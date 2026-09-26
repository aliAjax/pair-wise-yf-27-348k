import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


def _b64(payload):
    return base64.b64encode(payload).decode()


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def make_ready_object(self, inventory_no="M-1999-7", title="青铜器"):
        """登记一个满足外借全部前置条件的藏品（公开事件有来源 + 内部证据）。"""
        source = self.store.add_source("staff", f"档案-{inventory_no}", "archive", f"ACC-{inventory_no}")
        obj = self.store.create_object("staff", inventory_no, title, "礼器", "市博物馆", "1999年入藏。")
        self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        self.store.upload_evidence("staff", obj["id"], "purchase.pdf", _b64(b"purchase record"), "internal", None)
        return obj

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", _b64(b"purchase record"), "internal", event["id"])
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

    def test_loan_apply_approve_return_archive_flow(self):
        obj = self.make_ready_object("L-1", "外借青铜器")
        loan = self.store.create_loan("staff", obj["id"], "2027-03-01", "2027-05-31", "赴外馆特展三个月")
        self.assertEqual(loan["status"], "pending")
        self.assertEqual(loan["blocks"], [])
        approved = self.store.decide_loan("reviewer1", loan["id"], "approve")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewed_by"], "reviewer1")
        self.assertIn("object", approved["baseline"])
        self.assertEqual(approved["conflicts"], [])
        returned = self.store.return_loan("staff", loan["id"], "藏品完好归还并核对入藏。")
        self.assertEqual(returned["status"], "returned")
        self.assertIsNotNone(returned["returned_at"])
        self.assertEqual(returned["archive"]["returned_by"], "staff")
        self.assertEqual(returned["archive"]["purpose"], "赴外馆特展三个月")
        # 归还后不能重复操作
        with self.assertRaises(BusinessError) as ctx:
            self.store.return_loan("staff", loan["id"], "再次归还")
        self.assertEqual(ctx.exception.code, "loan_not_returnable")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("reviewer1", loan["id"], "approve")
        self.assertEqual(ctx.exception.code, "loan_not_pending")

    def test_loan_blocks_show_all_blocking_reasons(self):
        # 无来源公开事件 + 无内部证据
        obj = self.store.create_object("staff", "L-2", "陶罐", "陶器", "本馆", "公开简介。")
        self.store.add_event("staff", obj["id"], "exhibition", "2005-01-01", "2005-02-01", "本市", "公开展出记录", None, "public")
        loan = self.store.create_loan("staff", obj["id"], "2027-03-01", "2027-04-01", "巡展")
        codes = {b["code"] for b in loan["blocks"]}
        self.assertIn("public_event_without_source", codes)
        self.assertIn("missing_internal_evidence", codes)
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("reviewer1", loan["id"], "approve")
        self.assertEqual(ctx.exception.code, "loan_blocked")
        self.assertEqual({b["code"] for b in ctx.exception.details["blocks"]},
                         {"public_event_without_source", "missing_internal_evidence"})
        # 补上来源与内部证据后，未结主张仍然阻塞
        source = self.store.add_source("reviewer1", "展陈记录", "archive", "EX-2005-3")
        # 来源无法事后回填到事件，改为新增一条带来源的公开事件，并把旧缺来源事件移出公开层
        self.store.add_event("staff", obj["id"], "catalog", "2010-06-01", "", "本市", "编目记录", source["id"], "public")
        self.store.upload_evidence("reviewer1", obj["id"], "note.pdf", _b64(b"internal note"), "internal")
        claim = self.store.create_claim("claimant1", obj["id"], "某家族", "要求返还")
        blocks = {b["code"]: b for b in self.store.list_loans("staff", obj["id"])[0]["blocks"]}
        # 旧公开事件仍缺来源，同时出现未结主张
        self.assertIn("open_claim", blocks)
        self.assertEqual(blocks["open_claim"]["claim_id"], claim["id"])
        self.assertIn("public_event_without_source", blocks)
        # 主张进入终态（rejected）后不再算未结，但缺来源公开事件仍阻塞
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "进入审查阶段。")
        self.store.transition_claim("reviewer1", claim["id"], "rejected", "主张缺乏依据。")
        remaining = {b["code"] for b in self.store.list_loans("staff", obj["id"])[0]["blocks"]}
        self.assertNotIn("open_claim", remaining)
        self.assertIn("public_event_without_source", remaining)

    def test_internal_events_do_not_require_source_but_internal_evidence_required(self):
        obj = self.store.create_object("staff", "L-3", "织物", "丝织品", "库房", "公开简介。")
        # 只有内部事件（不需要来源），但没有任何证据
        self.store.add_event("staff", obj["id"], "inspection", "2019-09-01", "", "库房", "内部品相检查", None, "internal")
        loan = self.store.create_loan("staff", obj["id"], "2027-03-01", "2027-04-01", "学术借展")
        self.assertEqual({b["code"] for b in loan["blocks"]}, {"missing_internal_evidence"})
        # 公开证据不能满足“内部证据”要求
        self.store.upload_evidence("staff", obj["id"], "public.jpg", _b64(b"public"), "public")
        loan = self.store.list_loans("staff", obj["id"])[0]
        self.assertEqual({b["code"] for b in loan["blocks"]}, {"missing_internal_evidence"})
        # 补内部证据后放行
        self.store.upload_evidence("staff", obj["id"], "internal.jpg", _b64(b"internal"), "internal")
        loan = self.store.list_loans("staff", obj["id"])[0]
        self.assertEqual(loan["blocks"], [])
        approved = self.store.decide_loan("reviewer1", loan["id"], "approve")
        self.assertEqual(approved["status"], "approved")

    def test_loan_invalidated_by_claim_event_evidence_or_object_change(self):
        # 新增主张 -> 失效
        obj_claim = self.make_ready_object("L-4", "佛像")
        loan = self.store.decide_loan("reviewer1", self.store.create_loan("staff", obj_claim["id"], "2027-03-01", "2027-04-01", "借展一")["id"], "approve")
        new_claim = self.store.create_claim("claimant1", obj_claim["id"], "李氏后人", "追索")
        stale = self.store.list_loans("staff", obj_claim["id"])[0]
        self.assertEqual(stale["status"], "invalidated")
        claim_conflict = [c for c in stale["conflicts"] if c["type"] == "new_claim"]
        self.assertEqual(claim_conflict[0]["claim_id"], new_claim["id"])
        self.assertEqual(claim_conflict[0]["status"], "submitted")
        with self.assertRaises(BusinessError) as ctx:
            self.store.return_loan("staff", loan["id"], "失效后不能归还")
        self.assertEqual(ctx.exception.code, "loan_not_returnable")

        # 新增事件 -> 失效并显示事件冲突
        obj_event = self.make_ready_object("L-5", "瓷器")
        self.store.decide_loan("reviewer1", self.store.create_loan("staff", obj_event["id"], "2027-03-01", "2027-04-01", "借展二")["id"], "approve")
        new_event = self.store.add_event("staff", obj_event["id"], "note", "2026-01-01", "", "库房", "新增内部登记", None, "internal")
        stale = self.store.list_loans("staff", obj_event["id"])[0]
        self.assertEqual(stale["status"], "invalidated")
        event_conflict = [c for c in stale["conflicts"] if c["type"] == "new_event"]
        self.assertEqual(event_conflict[0]["event_id"], new_event["id"])

        # 新增证据 -> 失效
        obj_evidence = self.make_ready_object("L-6", "玉器")
        self.store.decide_loan("reviewer1", self.store.create_loan("staff", obj_evidence["id"], "2027-03-01", "2027-04-01", "借展三")["id"], "approve")
        new_evidence = self.store.upload_evidence("reviewer1", obj_evidence["id"], "new.pdf", _b64(b"new evidence"), "internal")
        stale = self.store.list_loans("staff", obj_evidence["id"])[0]
        self.assertEqual(stale["status"], "invalidated")
        evidence_conflict = [c for c in stale["conflicts"] if c["type"] == "new_evidence"]
        self.assertEqual(evidence_conflict[0]["evidence_id"], new_evidence["id"])

        # 修改藏品资料 -> 失效并显示被改字段
        obj_update = self.make_ready_object("L-7", "漆器")
        self.store.decide_loan("reviewer1", self.store.create_loan("staff", obj_update["id"], "2027-03-01", "2027-04-01", "借展四")["id"], "approve")
        self.store.update_object("staff", obj_update["id"], {"title": "漆器（修订名）", "current_holder": "修复室"})
        stale = self.store.list_loans("staff", obj_update["id"])[0]
        self.assertEqual(stale["status"], "invalidated")
        modified = [c for c in stale["conflicts"] if c["type"] == "object_modified"][0]
        self.assertEqual(set(modified["fields"]), {"title", "current_holder"})

    def test_overlapping_loan_cannot_be_approved_twice(self):
        obj = self.make_ready_object("L-8", "金器")
        first = self.store.create_loan("staff", obj["id"], "2027-03-01", "2027-05-31", "春季外借")
        self.store.decide_loan("reviewer1", first["id"], "approve")
        # 重叠时段：可以提交，但审批被阻塞
        overlap = self.store.create_loan("staff", obj["id"], "2027-05-15", "2027-06-15", "衔接展")
        self.assertEqual({b["code"] for b in overlap["blocks"]}, {"overlapping_loan"})
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("reviewer1", overlap["id"], "approve")
        self.assertEqual(ctx.exception.details["blocks"][0]["loan_id"], first["id"])
        # 不重叠的外借可以批准（首尾相接不算重叠）
        adjacent = self.store.create_loan("staff", obj["id"], "2027-06-01", "2027-07-01", "夏季外借")
        self.assertEqual({b["code"] for b in adjacent["blocks"]}, set())
        self.store.decide_loan("reviewer1", adjacent["id"], "approve")
        # 第一条归还后，重叠申请仍需重新走审查；归还留档不再占时段，新的重叠申请可批
        self.store.return_loan("staff", first["id"], "按期归还。")
        after_return = self.store.create_loan("staff", obj["id"], "2027-04-01", "2027-04-10", "补展")
        self.assertEqual(after_return["blocks"], [])
        self.store.decide_loan("reviewer1", after_return["id"], "approve")

    def test_loan_permissions(self):
        obj = self.make_ready_object("L-9", "银器")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_loan("reviewer1", obj["id"], "2027-03-01", "2027-04-01", "审查员不能发起")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_loan("claimant1", obj["id"], "2027-03-01", "2027-04-01", "主张人不能发起")
        self.assertEqual(ctx.exception.status, 403)
        loan = self.store.create_loan("staff", obj["id"], "2027-03-01", "2027-04-01", "正式借展")
        with self.assertRaises(BusinessError) as ctx:
            self.store.decide_loan("staff", loan["id"], "approve")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_loans("public")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_loans("claimant1")
        self.assertEqual(ctx.exception.status, 403)

    def test_loan_date_validation(self):
        obj = self.make_ready_object("L-10", "铜印")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_loan("staff", obj["id"], "2027-05-01", "2027-04-01", "反向时段")
        self.assertEqual(ctx.exception.code, "invalid_date_range")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_loan("staff", obj["id"], "05/01/2027", "2027-05-10", "坏日期")
        self.assertEqual(ctx.exception.code, "invalid_date")
        with self.assertRaises(BusinessError) as ctx:
            self.store.create_loan("staff", obj["id"], "2027-05-01", "2027-05-10", "  ")
        self.assertEqual(ctx.exception.code, "invalid_purpose")


if __name__ == "__main__":
    unittest.main()
