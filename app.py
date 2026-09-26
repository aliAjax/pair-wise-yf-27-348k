"""博物馆藏品来源与返还审查系统。"""
from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import threading
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "provenance.db"
CLAIM_TRANSITIONS = {
    "submitted": {"under_review"},
    "under_review": {"negotiating", "resolved_return", "rejected"},
    "negotiating": {"resolved_return", "rejected"},
    "resolved_return": set(),
    "rejected": set(),
}
OPEN_CLAIM_STATUSES = ("submitted", "under_review", "negotiating")
LOAN_STATUSES = ("pending", "approved", "invalidated", "returned")
LOAN_OBJECT_FIELDS = ("title", "object_type", "current_holder", "public_summary")


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        super().__init__(message)
        self.message, self.status, self.code, self.details = message, status, code, details


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ProvenanceStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('staff','reviewer','claimant','public'))
                );
                CREATE TABLE IF NOT EXISTS sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    source_type TEXT NOT NULL, reference TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(name,reference)
                );
                CREATE TABLE IF NOT EXISTS objects(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, inventory_no TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL, object_type TEXT NOT NULL, current_holder TEXT NOT NULL,
                    public_summary TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_type TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT,
                    place TEXT NOT NULL, description TEXT NOT NULL,
                    source_id INTEGER REFERENCES sources(id),
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    created_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    event_id INTEGER REFERENCES events(id), filename TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('public','internal')),
                    uploaded_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    claimant_id TEXT NOT NULL REFERENCES users(id),
                    claimed_by TEXT NOT NULL, desired_outcome TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'submitted'
                        CHECK(status IN ('submitted','under_review','negotiating','resolved_return','rejected')),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claim_reviews(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL REFERENCES claims(id),
                    reviewer_id TEXT NOT NULL REFERENCES users(id),
                    old_status TEXT NOT NULL, new_status TEXT NOT NULL,
                    note TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    version INTEGER NOT NULL, snapshot TEXT NOT NULL,
                    changed_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(object_id,version)
                );
                CREATE TABLE IF NOT EXISTS loan_approvals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    object_id INTEGER NOT NULL REFERENCES objects(id),
                    requested_by TEXT NOT NULL REFERENCES users(id),
                    loan_start TEXT NOT NULL, loan_end TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','invalidated','returned')),
                    reviewed_by TEXT REFERENCES users(id), reviewed_at TEXT,
                    approved_at TEXT,
                    baseline TEXT,
                    conflict_summary TEXT,
                    returned_at TEXT, returned_by TEXT REFERENCES users(id),
                    archive TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, object_id INTEGER REFERENCES objects(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role) VALUES(?,?,?)",
                [
                    ("staff", "藏品研究员", "staff"),
                    ("reviewer1", "返还审查员", "reviewer"),
                    ("claimant1", "权利主张人", "claimant"),
                    ("public", "公众访客", "public"),
                ],
            )

    def _user(self, conn, user_id, roles=None):
        if not user_id:
            raise BusinessError("缺少 X-User-Id", 401, "authentication_required")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            raise BusinessError("用户不存在", 401, "unknown_user")
        if roles and user["role"] not in roles:
            raise BusinessError("当前角色无权执行此操作", 403, "forbidden")
        return user

    def _object(self, conn, object_id):
        row = conn.execute("SELECT * FROM objects WHERE id=?", (object_id,)).fetchone()
        if not row:
            raise BusinessError("藏品不存在", 404, "not_found")
        return row

    def _audit(self, conn, object_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(object_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (object_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )

    def _snapshot(self, conn, object_id, actor):
        row = self._object(conn, object_id)
        snapshot = {
            "object": dict(row),
            "events": [dict(x) for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
            "claims": [dict(x) for x in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
        }
        conn.execute(
            "INSERT INTO object_versions(object_id,version,snapshot,changed_by,created_at) VALUES(?,?,?,?,?)",
            (object_id, row["version"], json.dumps(snapshot, ensure_ascii=False, sort_keys=True), actor, now()),
        )

    def create_object(self, user_id, inventory_no, title, object_type, holder, public_summary):
        inventory_no, title = inventory_no.strip(), title.strip()
        if not inventory_no or len(title) < 2:
            raise BusinessError("库存号和标题不能为空", 422, "invalid_object")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            try:
                cur = conn.execute(
                    """INSERT INTO objects(inventory_no,title,object_type,current_holder,public_summary,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (inventory_no, title, object_type.strip() or "未分类", holder.strip() or "馆藏", public_summary.strip(), user_id, now(), now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("库存号已存在", 409, "inventory_exists")
            object_id = cur.lastrowid
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.create", {"inventory_no": inventory_no})
            return {"id": object_id, "inventory_no": inventory_no, "version": 1}

    def update_object(self, user_id, object_id, changes):
        allowed = {"title", "object_type", "current_holder", "public_summary"}
        clean = {k: str(v).strip() for k, v in changes.items() if k in allowed and str(v).strip()}
        if not clean:
            raise BusinessError("没有可更新字段", 422, "empty_update")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            new_version = row["version"] + 1
            assignments = ",".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE objects SET {assignments},version=?,updated_at=? WHERE id=?",
                (*clean.values(), new_version, now(), object_id),
            )
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "object.update", {"version": new_version, "changes": clean})
            self._invalidate_approved_loans(conn, object_id, user_id, "object_updated", {"changes": list(clean.keys())})
            return {"id": object_id, "version": new_version, "changes": clean}

    def add_source(self, user_id, name, source_type, reference):
        if not name.strip() or not reference.strip():
            raise BusinessError("来源名称和引用不能为空", 422, "invalid_source")
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                cur = conn.execute(
                    "INSERT INTO sources(name,source_type,reference,created_by,created_at) VALUES(?,?,?,?,?)",
                    (name.strip(), source_type.strip() or "archive", reference.strip(), user_id, now()),
                )
            except sqlite3.IntegrityError:
                raise BusinessError("来源记录已存在", 409, "source_exists")
            return {"id": cur.lastrowid, "name": name.strip(), "reference": reference.strip()}

    def add_event(self, user_id, object_id, event_type, date_start, date_end, place, description, source_id=None, visibility="internal"):
        if not event_type.strip() or not description.strip() or not place.strip():
            raise BusinessError("事件类型、地点和说明不能为空", 422, "invalid_event")
        try:
            start = date.fromisoformat(date_start)
            end = date.fromisoformat(date_end) if date_end else start
        except ValueError:
            raise BusinessError("事件日期必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("事件结束日期不能早于开始日期", 422, "invalid_date_range")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff"})
            row = self._object(conn, object_id)
            if source_id and not conn.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone():
                raise BusinessError("来源不存在", 404, "source_not_found")
            cur = conn.execute(
                """INSERT INTO events(object_id,event_type,date_start,date_end,place,description,source_id,visibility,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (object_id, event_type.strip(), date_start, date_end or None, place.strip(), description.strip(), source_id, visibility, user_id, now()),
            )
            new_version = row["version"] + 1
            conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (new_version, now(), object_id))
            self._snapshot(conn, object_id, user_id)
            self._audit(conn, object_id, user_id, "event.add", {"event_id": cur.lastrowid, "version": new_version, "visibility": visibility})
            self._invalidate_approved_loans(conn, object_id, user_id, "event_added", {"event_id": cur.lastrowid, "visibility": visibility})
            return {"id": cur.lastrowid, "object_id": object_id, "object_version": new_version}

    def upload_evidence(self, user_id, object_id, filename, content_b64, visibility, event_id=None):
        if not filename.strip():
            raise BusinessError("文件名不能为空", 422, "invalid_filename")
        if visibility not in {"public", "internal"}:
            raise BusinessError("visibility 必须是 public 或 internal", 422, "invalid_visibility")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError):
            raise BusinessError("content_b64 不是合法 Base64", 422, "invalid_base64")
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as conn:
            actor = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            if event_id and not conn.execute("SELECT 1 FROM events WHERE id=? AND object_id=?", (event_id, object_id)).fetchone():
                raise BusinessError("证据关联的事件不存在", 404, "event_not_found")
            cur = conn.execute(
                """INSERT INTO evidence(object_id,event_id,filename,sha256,size,content,visibility,uploaded_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (object_id, event_id, filename.strip(), digest, len(content), content, visibility, user_id, now()),
            )
            self._audit(conn, object_id, user_id, "evidence.upload", {"evidence_id": cur.lastrowid, "sha256": digest, "visibility": visibility})
            self._invalidate_approved_loans(conn, object_id, user_id, "evidence_added", {"evidence_id": cur.lastrowid, "visibility": visibility})
            return {"id": cur.lastrowid, "filename": filename.strip(), "sha256": digest, "size": len(content)}

    def create_claim(self, user_id, object_id, claimed_by, desired_outcome):
        if not claimed_by.strip() or not desired_outcome.strip():
            raise BusinessError("主张人和期望结果不能为空", 422, "invalid_claim")
        with self.connect() as conn:
            claimant = self._user(conn, user_id, {"claimant"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO claims(object_id,claimant_id,claimed_by,desired_outcome,created_at,updated_at)
                   VALUES(?,?,?,?,?,?)""",
                (object_id, user_id, claimed_by.strip(), desired_outcome.strip(), now(), now()),
            )
            self._audit(conn, object_id, user_id, "claim.create", {"claim_id": cur.lastrowid})
            self._invalidate_approved_loans(conn, object_id, user_id, "claim_added", {"claim_id": cur.lastrowid})
            return {"id": cur.lastrowid, "object_id": object_id, "status": "submitted"}

    def transition_claim(self, user_id, claim_id, new_status, note):
        if len(note.strip()) < 5:
            raise BusinessError("阶段审查说明至少 5 字", 422, "review_note_required")
        with self.connect() as conn:
            reviewer = self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                claim = conn.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not claim:
                    raise BusinessError("权利主张不存在", 404, "not_found")
                allowed = CLAIM_TRANSITIONS.get(claim["status"], set())
                if new_status not in allowed:
                    raise BusinessError(f"不能从 {claim['status']} 直接变更为 {new_status}", 409, "invalid_transition")
                conn.execute("UPDATE claims SET status=?,updated_at=? WHERE id=?", (new_status, now(), claim_id))
                conn.execute(
                    "INSERT INTO claim_reviews(claim_id,reviewer_id,old_status,new_status,note,created_at) VALUES(?,?,?,?,?,?)",
                    (claim_id, user_id, claim["status"], new_status, note.strip(), now()),
                )
                new_version = claim["object_id"]
                obj = self._object(conn, claim["object_id"])
                next_version = obj["version"] + 1
                conn.execute("UPDATE objects SET version=?,updated_at=? WHERE id=?", (next_version, now(), claim["object_id"]))
                self._snapshot(conn, claim["object_id"], user_id)
                self._audit(conn, claim["object_id"], user_id, "claim.transition", {"claim_id": claim_id, "from": claim["status"], "to": new_status})
                return {"claim_id": claim_id, "old_status": claim["status"], "status": new_status, "object_version": next_version}
            except Exception:
                conn.rollback()
                raise

    def _parse_loan_dates(self, loan_start, loan_end):
        try:
            start, end = date.fromisoformat(loan_start), date.fromisoformat(loan_end)
        except (ValueError, TypeError):
            raise BusinessError("借展时段必须是 YYYY-MM-DD", 422, "invalid_date")
        if end < start:
            raise BusinessError("外借结束日期不能早于开始日期", 422, "invalid_date_range")
        return start.isoformat(), end.isoformat()

    def _loan_blocks(self, conn, row):
        """计算外借申请当前的阻塞项：未结主张、公开事件缺来源、内部证据缺失、时段重叠。"""
        object_id = row["object_id"]
        blocks = []
        placeholders = ",".join("?" * len(OPEN_CLAIM_STATUSES))
        open_claims = conn.execute(
            f"SELECT id,claimed_by,status FROM claims WHERE object_id=? AND status IN ({placeholders}) ORDER BY id",
            (object_id, *OPEN_CLAIM_STATUSES),
        ).fetchall()
        for c in open_claims:
            blocks.append({"code": "open_claim", "claim_id": c["id"], "claimed_by": c["claimed_by"], "status": c["status"],
                           "message": f"存在未结权利主张 #{c['id']}（{c['claimed_by']}，状态 {c['status']}）"})
        for e in conn.execute(
            "SELECT id,event_type,source_id FROM events WHERE object_id=? AND visibility='public' ORDER BY id", (object_id,)
        ).fetchall():
            source_ok = e["source_id"] and conn.execute("SELECT 1 FROM sources WHERE id=?", (e["source_id"],)).fetchone()
            if not source_ok:
                blocks.append({"code": "public_event_without_source", "event_id": e["id"], "event_type": e["event_type"],
                               "message": f"公开流转事件 #{e['id']}（{e['event_type']}）缺少来源"})
        internal_count = conn.execute(
            "SELECT COUNT(*) AS n FROM evidence WHERE object_id=? AND visibility='internal'", (object_id,)
        ).fetchone()["n"]
        if internal_count < 1:
            blocks.append({"code": "missing_internal_evidence", "message": "至少需要一条内部证据"})
        for o in conn.execute(
            """SELECT id FROM loan_approvals
               WHERE object_id=? AND id<>? AND status='approved'
                 AND loan_start<=? AND loan_end>=? ORDER BY id""",
            (object_id, row["id"], row["loan_end"], row["loan_start"]),
        ).fetchall():
            blocks.append({"code": "overlapping_loan", "loan_id": o["id"],
                           "message": f"与已批准外借 #{o['id']} 的借展时段重叠，不能重复批准"})
        return blocks

    def _loan_conflicts(self, conn, row):
        """批准基线与当前藏品资料之间的冲突项（失效后展示）。"""
        if not row["baseline"]:
            return []
        baseline = json.loads(row["baseline"])
        object_id = row["object_id"]
        conflicts = []
        for c in conn.execute("SELECT id,claimed_by,status FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall():
            if c["id"] not in baseline["claim_ids"]:
                conflicts.append({"type": "new_claim", "claim_id": c["id"], "claimed_by": c["claimed_by"], "status": c["status"],
                                  "message": f"新增权利主张 #{c['id']}（{c['claimed_by']}，状态 {c['status']}）"})
        for e in conn.execute("SELECT id,event_type,visibility FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall():
            if e["id"] not in baseline["event_ids"]:
                scope = "公开" if e["visibility"] == "public" else "内部"
                conflicts.append({"type": "new_event", "event_id": e["id"], "event_type": e["event_type"], "visibility": e["visibility"],
                                  "message": f"新增{scope}流转事件 #{e['id']}（{e['event_type']}）"})
        for ev in conn.execute("SELECT id,filename,visibility FROM evidence WHERE object_id=? ORDER BY id", (object_id,)).fetchall():
            if ev["id"] not in baseline["evidence_ids"]:
                scope = "内部" if ev["visibility"] == "internal" else "公开"
                conflicts.append({"type": "new_evidence", "evidence_id": ev["id"], "filename": ev["filename"], "visibility": ev["visibility"],
                                  "message": f"新增{scope}证据 {ev['filename']}"})
        obj = self._object(conn, object_id)
        changed_fields = [f for f in LOAN_OBJECT_FIELDS if obj[f] != baseline["object"].get(f)]
        if changed_fields:
            conflicts.append({"type": "object_modified", "fields": changed_fields,
                              "message": "藏品资料已修改：" + "、".join(changed_fields)})
        return conflicts

    def _invalidate_approved_loans(self, conn, object_id, actor, trigger, trigger_item):
        """藏品发生批准后变化时，将该藏品所有已批准外借置为失效并记录冲突项。"""
        invalidated = []
        for loan in conn.execute(
            "SELECT * FROM loan_approvals WHERE object_id=? AND status='approved' ORDER BY id", (object_id,)
        ).fetchall():
            conflicts = self._loan_conflicts(conn, loan)
            conn.execute(
                "UPDATE loan_approvals SET status='invalidated',conflict_summary=?,updated_at=? WHERE id=?",
                (json.dumps(conflicts, ensure_ascii=False, sort_keys=True), now(), loan["id"]),
            )
            self._audit(conn, object_id, actor, "loan.invalidated",
                        {"loan_id": loan["id"], "trigger": trigger, "trigger_item": trigger_item})
            invalidated.append(loan["id"])
        return invalidated

    def _loan_payload(self, conn, row):
        payload = {k: row[k] for k in row.keys()}
        obj = conn.execute("SELECT inventory_no,title FROM objects WHERE id=?", (row["object_id"],)).fetchone()
        payload["object_inventory_no"] = obj["inventory_no"] if obj else None
        payload["object_title"] = obj["title"] if obj else None
        if payload.get("baseline"):
            payload["baseline"] = json.loads(payload["baseline"])
        if payload.get("archive"):
            payload["archive"] = json.loads(payload["archive"])
        if payload.get("conflict_summary"):
            payload["conflicts_at_invalidation"] = json.loads(payload["conflict_summary"])
        payload["blocks"] = self._loan_blocks(conn, row) if row["status"] == "pending" else []
        payload["conflicts"] = self._loan_conflicts(conn, row) if row["status"] in ("approved", "invalidated") and row["baseline"] else []
        return payload

    def create_loan(self, user_id, object_id, loan_start, loan_end, purpose):
        start, end = self._parse_loan_dates(loan_start, loan_end)
        if not purpose or not purpose.strip():
            raise BusinessError("借展用途不能为空", 422, "invalid_purpose")
        purpose = purpose.strip()
        with self.connect() as conn:
            self._user(conn, user_id, {"staff"})
            self._object(conn, object_id)
            cur = conn.execute(
                """INSERT INTO loan_approvals(object_id,requested_by,loan_start,loan_end,purpose,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (object_id, user_id, start, end, purpose, now(), now()),
            )
            loan_id = cur.lastrowid
            self._audit(conn, object_id, user_id, "loan.request",
                        {"loan_id": loan_id, "loan_start": start, "loan_end": end, "purpose": purpose})
            return self._loan_payload(conn, conn.execute("SELECT * FROM loan_approvals WHERE id=?", (loan_id,)).fetchone())

    def decide_loan(self, user_id, loan_id, decision):
        with self.connect() as conn:
            self._user(conn, user_id, {"reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM loan_approvals WHERE id=?", (loan_id,)).fetchone()
                if not row:
                    raise BusinessError("外借申请不存在", 404, "not_found")
                if row["status"] != "pending":
                    raise BusinessError(f"申请当前状态为 {row['status']}，无法再次审批", 409, "loan_not_pending")
                if decision == "reject":
                    conn.execute("DELETE FROM loan_approvals WHERE id=?", (loan_id,))
                    self._audit(conn, row["object_id"], user_id, "loan.reject", {"loan_id": loan_id})
                    return {"loan_id": loan_id, "status": "rejected"}
                if decision != "approve":
                    raise BusinessError("decision 必须是 approve 或 reject", 422, "invalid_decision")
                blocks = self._loan_blocks(conn, row)
                if blocks:
                    raise BusinessError("外借审批未通过，请先解除阻塞项", 409, "loan_blocked", details={"blocks": blocks})
                obj = self._object(conn, row["object_id"])
                baseline = {
                    "object": {f: obj[f] for f in LOAN_OBJECT_FIELDS},
                    "event_ids": [r["id"] for r in conn.execute("SELECT id FROM events WHERE object_id=? ORDER BY id", (row["object_id"],)).fetchall()],
                    "evidence_ids": [r["id"] for r in conn.execute("SELECT id FROM evidence WHERE object_id=? ORDER BY id", (row["object_id"],)).fetchall()],
                    "claim_ids": [r["id"] for r in conn.execute("SELECT id FROM claims WHERE object_id=? ORDER BY id", (row["object_id"],)).fetchall()],
                }
                ts = now()
                conn.execute(
                    """UPDATE loan_approvals SET status='approved',reviewed_by=?,reviewed_at=?,approved_at=?,
                       baseline=?,updated_at=? WHERE id=?""",
                    (user_id, ts, ts, json.dumps(baseline, ensure_ascii=False, sort_keys=True), ts, loan_id),
                )
                self._audit(conn, row["object_id"], user_id, "loan.approve", {"loan_id": loan_id})
                return self._loan_payload(conn, conn.execute("SELECT * FROM loan_approvals WHERE id=?", (loan_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise

    def return_loan(self, user_id, loan_id, note):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM loan_approvals WHERE id=?", (loan_id,)).fetchone()
                if not row:
                    raise BusinessError("外借申请不存在", 404, "not_found")
                if row["status"] != "approved":
                    raise BusinessError(f"申请当前状态为 {row['status']}，只有已批准且未失效的外借可以归还", 409, "loan_not_returnable")
                ts = now()
                archive = {
                    "loan_start": row["loan_start"], "loan_end": row["loan_end"], "purpose": row["purpose"],
                    "requested_by": row["requested_by"], "reviewed_by": row["reviewed_by"],
                    "approved_at": row["approved_at"], "returned_at": ts, "returned_by": user_id, "note": note.strip(),
                }
                conn.execute(
                    "UPDATE loan_approvals SET status='returned',returned_at=?,returned_by=?,archive=?,updated_at=? WHERE id=?",
                    (ts, user_id, json.dumps(archive, ensure_ascii=False, sort_keys=True), ts, loan_id),
                )
                self._audit(conn, row["object_id"], user_id, "loan.return", {"loan_id": loan_id, "note": note.strip()})
                return self._loan_payload(conn, conn.execute("SELECT * FROM loan_approvals WHERE id=?", (loan_id,)).fetchone())
            except Exception:
                conn.rollback()
                raise

    def list_loans(self, user_id, object_id=None):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            if object_id is not None:
                self._object(conn, object_id)
                rows = conn.execute("SELECT * FROM loan_approvals WHERE object_id=? ORDER BY id", (object_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM loan_approvals ORDER BY id").fetchall()
            return [self._loan_payload(conn, r) for r in rows]

    def get_object(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            obj = self._object(conn, object_id)
            if user["role"] == "public":
                events = conn.execute(
                    "SELECT id,event_type,date_start,date_end,place,description,visibility,created_at FROM events WHERE object_id=? AND visibility='public' ORDER BY id",
                    (object_id,),
                ).fetchall()
                claims = conn.execute(
                    "SELECT id,claimed_by,desired_outcome,status,created_at FROM claims WHERE object_id=? ORDER BY id", (object_id,)
                ).fetchall()
                return {
                    "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                    "object_type": obj["object_type"], "public_summary": obj["public_summary"], "version": obj["version"],
                    "events": [dict(e) for e in events], "claims": [dict(c) for c in claims],
                }
            result = {
                "id": obj["id"], "inventory_no": obj["inventory_no"], "title": obj["title"],
                "object_type": obj["object_type"], "current_holder": obj["current_holder"],
                "public_summary": obj["public_summary"], "version": obj["version"],
                "events": [dict(x) | {"source": dict(conn.execute("SELECT id,name,source_type,reference FROM sources WHERE id=?", (x["source_id"],)).fetchone()) if x["source_id"] else None,
                                     "evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE event_id=? ORDER BY id", (x["id"],)).fetchall()]}
                            for x in conn.execute("SELECT * FROM events WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "claims": [dict(c) | {"reviews": [dict(r) for r in conn.execute("SELECT * FROM claim_reviews WHERE claim_id=? ORDER BY id", (c["id"],)).fetchall()]}
                           for c in conn.execute("SELECT * FROM claims WHERE object_id=? ORDER BY id", (object_id,)).fetchall()],
                "unlinked_evidence": [dict(e) for e in conn.execute("SELECT id,filename,sha256,size,visibility FROM evidence WHERE object_id=? AND event_id IS NULL ORDER BY id", (object_id,)).fetchall()],
            }
            if user["role"] == "claimant":
                # 主张人只看到公开来源事件和自己的主张，不能浏览内部调查材料。
                result["events"] = [e for e in result["events"] if e["visibility"] == "public"]
                result["unlinked_evidence"] = []
                result["claims"] = [c for c in result["claims"] if c["claimant_id"] == user_id]
                for c in result["claims"]:
                    c.pop("claimant_id", None)
            return result

    def list_objects(self, user_id):
        with self.connect() as conn:
            user = self._user(conn, user_id)
            if user["role"] == "public":
                rows = conn.execute("SELECT id,inventory_no,title,object_type,public_summary,version FROM objects ORDER BY id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM objects ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def object_history(self, user_id, object_id):
        with self.connect() as conn:
            user = self._user(conn, user_id, {"staff", "reviewer"})
            self._object(conn, object_id)
            rows = conn.execute("SELECT id,version,changed_by,created_at FROM object_versions WHERE object_id=? ORDER BY version", (object_id,)).fetchall()
            return [dict(r) for r in rows]

    def history_detail(self, user_id, object_id, version):
        with self.connect() as conn:
            self._user(conn, user_id, {"staff", "reviewer"})
            row = conn.execute("SELECT * FROM object_versions WHERE object_id=? AND version=?", (object_id, version)).fetchone()
            if not row:
                raise BusinessError("历史版本不存在", 404, "not_found")
            return dict(row) | {"snapshot": json.loads(row["snapshot"])}


class Handler(BaseHTTPRequestHandler):
    server_version = "Provenance/1.0"

    def _store(self): return self.server.store  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health": return self._send(200, {"ok": True})
        store = self._store()
        if parts == ["api", "objects"] and method == "GET": return self._send(200, {"items": store.list_objects(user)})
        if parts == ["api", "objects"] and method == "POST":
            d = self._body(); return self._send(201, store.create_object(user, d.get("inventory_no", ""), d.get("title", ""), d.get("object_type", ""), d.get("current_holder", ""), d.get("public_summary", "")))
        if parts == ["api", "sources"] and method == "POST":
            d = self._body(); return self._send(201, store.add_source(user, d.get("name", ""), d.get("source_type", ""), d.get("reference", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "objects"]:
            object_id = int(parts[2])
            if len(parts) == 3 and method == "GET": return self._send(200, store.get_object(user, object_id))
            if len(parts) == 4 and parts[3] == "update" and method == "POST": return self._send(200, store.update_object(user, object_id, self._body().get("changes", {})))
            if len(parts) == 4 and parts[3] == "events" and method == "POST":
                d = self._body(); return self._send(201, store.add_event(user, object_id, d.get("event_type", ""), d.get("date_start", ""), d.get("date_end", ""), d.get("place", ""), d.get("description", ""), d.get("source_id"), d.get("visibility", "internal")))
            if len(parts) == 4 and parts[3] == "evidence" and method == "POST":
                d = self._body(); return self._send(201, store.upload_evidence(user, object_id, d.get("filename", ""), d.get("content_b64", ""), d.get("visibility", "internal"), d.get("event_id")))
            if len(parts) == 4 and parts[3] == "claims" and method == "POST":
                d = self._body(); return self._send(201, store.create_claim(user, object_id, d.get("claimed_by", ""), d.get("desired_outcome", "")))
            if len(parts) == 4 and parts[3] == "history" and method == "GET": return self._send(200, {"items": store.object_history(user, object_id)})
            if len(parts) == 5 and parts[3] == "history" and method == "GET": return self._send(200, store.history_detail(user, object_id, int(parts[4])))
            if len(parts) == 4 and parts[3] == "loans" and method == "GET": return self._send(200, {"items": store.list_loans(user, object_id)})
            if len(parts) == 4 and parts[3] == "loans" and method == "POST":
                d = self._body(); return self._send(201, store.create_loan(user, object_id, d.get("loan_start", ""), d.get("loan_end", ""), d.get("purpose", "")))
        if parts == ["api", "loans"] and method == "GET": return self._send(200, {"items": store.list_loans(user)})
        if len(parts) == 4 and parts[:2] == ["api", "loans"]:
            loan_id = int(parts[2])
            if parts[3] == "approve" and method == "POST": return self._send(200, store.decide_loan(user, loan_id, "approve"))
            if parts[3] == "reject" and method == "POST": return self._send(200, store.decide_loan(user, loan_id, "reject"))
            if parts[3] == "return" and method == "POST":
                d = self._body(); return self._send(200, store.return_loan(user, loan_id, d.get("note", "")))
        if len(parts) == 4 and parts[:2] == ["api", "claims"] and parts[3] == "transition" and method == "POST":
            d = self._body(); return self._send(200, store.transition_claim(user, int(parts[2]), d.get("status", ""), d.get("note", "")))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try: self._dispatch(method)
        except BusinessError as exc:
            payload = {"error": {"code": exc.code, "message": exc.message}}
            if exc.details is not None:
                payload["error"]["details"] = exc.details
            self._send(exc.status, payload)
        except (ValueError, TypeError): self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc: self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")
    def log_message(self, fmt, *args): print(f"{self.address_string()} - {fmt % args}")


class ProvenanceServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, store): self.store = store; super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="博物馆藏品来源与返还审查")
    parser.add_argument("--db", default=str(DEFAULT_DB)); parser.add_argument("--port", type=int, default=8103)
    parser.add_argument("--init", action="store_true"); parser.add_argument("--seed", action="store_true")
    args = parser.parse_args(); store = ProvenanceStore(args.db); store.init_schema()
    if args.seed: store.seed()
    if args.init or args.seed: print(f"数据库已初始化: {args.db}"); return
    server = ProvenanceServer(("127.0.0.1", args.port), store)
    print(f"来源审查系统运行于 http://127.0.0.1:{args.port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == "__main__": main()
