import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)


class LoanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _ready_object(self, inventory_no="M-2026-1"):
        """满足全部外借条件的藏品：公开事件有来源、有内部证据、无未结主张。"""
        source = self.store.add_source("staff", f"出库档案-{inventory_no}", "archive", f"REF-{inventory_no}")
        obj = self.store.create_object("staff", inventory_no, "瓷器", "器物", "库房", "简介。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "2000-01-01", "", "本市", "购藏入藏", source["id"], "public")
        self.store.upload_evidence("staff", obj["id"], "report.pdf", base64.b64encode(b"internal file").decode(), "internal", event["id"])
        return obj

    def test_loan_submit_approve_return_archived(self):
        obj = self._ready_object()
        loan = self.store.submit_loan("staff", obj["id"], "2026-10-01", "2026-12-01", "赴甲馆特展")
        self.assertEqual(loan["status"], "submitted")
        ok = self.store.review_loan("reviewer1", loan["id"], "approve", "条件齐备，同意外借。")
        self.assertEqual(ok["status"], "approved")
        done = self.store.return_loan("staff", loan["id"], "按期归还，状况良好。")
        self.assertEqual(done["status"], "returned")
        view = self.store.get_loan("reviewer1", loan["id"])
        self.assertEqual(view["status"], "returned")
        self.assertEqual(view["returned_by"], "staff")
        self.assertIsNotNone(view["returned_at"])

    def test_loan_blocked_reasons(self):
        obj = self.store.create_object("staff", "M-2026-2", "书画", "纸质", "库房", "简介。")
        self.store.add_event("staff", obj["id"], "exhibition", "2010-05-01", "", "本馆", "公开展出", None, "public")
        self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "返还书画")
        loan = self.store.submit_loan("staff", obj["id"], "2026-10-01", "2026-11-01", "巡展")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_loan("reviewer1", loan["id"], "approve", "尝试批准外借。")
        self.assertEqual(ctx.exception.code, "loan_blocked")
        codes = {b["code"] for b in ctx.exception.extra["blockers"]}
        self.assertEqual(codes, {"open_claim", "event_without_source", "no_internal_evidence"})
        # 列表视图同样给出阻塞原因
        view = self.store.get_loan("staff", loan["id"])
        self.assertEqual(len(view["blockers"]), 3)

    def test_approval_invalidated_by_new_materials(self):
        triggers = [
            ("claim", lambda oid: self.store.create_claim("claimant1", oid, "主张人", "返还藏品")),
            ("event", lambda oid: self.store.add_event("staff", oid, "note", "2026-01-01", "", "馆内", "内部核查记录", None, "internal")),
            ("evidence", lambda oid: self.store.upload_evidence("staff", oid, "x.pdf", base64.b64encode(b"x").decode(), "internal")),
            ("object_update", lambda oid: self.store.update_object("staff", oid, {"public_summary": "更新简介。"})),
        ]
        for i, (kind, trigger) in enumerate(triggers):
            obj = self._ready_object(f"M-2026-1{i}")
            loan = self.store.submit_loan("staff", obj["id"], "2026-10-01", "2026-12-01", "特展")
            self.store.review_loan("reviewer1", loan["id"], "approve", "条件齐备，同意。")
            trigger(obj["id"])
            view = self.store.get_loan("staff", loan["id"])
            self.assertEqual(view["status"], "invalidated", kind)
            self.assertEqual(view["conflicts"][0]["type"], kind)

    def test_overlapping_loan_cannot_be_approved_twice(self):
        obj = self._ready_object("M-2026-9")
        loan1 = self.store.submit_loan("staff", obj["id"], "2026-10-01", "2026-12-01", "甲馆特展")
        self.store.review_loan("reviewer1", loan1["id"], "approve", "同意甲馆特展。")
        loan2 = self.store.submit_loan("staff", obj["id"], "2026-11-15", "2027-01-15", "乙馆巡展")
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_loan("reviewer1", loan2["id"], "approve", "尝试批准乙馆。")
        self.assertIn("overlapping_loan", {b["code"] for b in ctx.exception.extra["blockers"]})
        loan3 = self.store.submit_loan("staff", obj["id"], "2027-02-01", "2027-04-01", "丙馆联展")
        self.assertEqual(self.store.review_loan("reviewer1", loan3["id"], "approve", "时段不重叠，同意。")["status"], "approved")
        # 归还留档后，原时段可以再次批准
        self.store.return_loan("staff", loan1["id"], "已归还入库。")
        loan4 = self.store.submit_loan("staff", obj["id"], "2026-11-01", "2026-12-15", "丁馆补展")
        self.assertEqual(self.store.review_loan("reviewer1", loan4["id"], "approve", "甲馆已归还，同意。")["status"], "approved")

    def test_loan_role_and_state_invariants(self):
        obj = self._ready_object("M-2026-8")
        with self.assertRaises(BusinessError) as ctx:
            self.store.submit_loan("reviewer1", obj["id"], "2026-10-01", "2026-11-01", "越权提交")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError):
            self.store.submit_loan("staff", obj["id"], "2026-11-01", "2026-10-01", "日期倒置")
        loan = self.store.submit_loan("staff", obj["id"], "2026-10-01", "2026-11-01", "特展")
        with self.assertRaises(BusinessError) as ctx:
            self.store.return_loan("staff", loan["id"], "未批准就归还")
        self.assertEqual(ctx.exception.code, "invalid_state")
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_loans("public")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
