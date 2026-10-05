import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .usage import sum_usage
from .errors import SkillServiceError


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "state.sqlite"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        try:
            self._create()
        except BaseException:
            self._connection.close()
            raise

    def _create(self):
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY,
                harness TEXT NOT NULL,
                session_id TEXT NOT NULL,
                watermark TEXT NOT NULL,
                trigger_name TEXT,
                delegate_depth INTEGER NOT NULL,
                model TEXT,
                reasoning TEXT,
                system_prompt TEXT,
                tools_json TEXT,
                messages_json TEXT NOT NULL,
                received_at TEXT NOT NULL,
                context_window INTEGER,
                capture_json TEXT,
                temperature REAL,
                session_name TEXT,
                UNIQUE(harness, session_id, watermark)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                submission_id TEXT NOT NULL,
                harness TEXT NOT NULL,
                session_id TEXT NOT NULL,
                status TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                called_model TEXT,
                outcome TEXT,
                error TEXT,
                started_at TEXT,
                finished_at TEXT,
                context_mode TEXT,
                tools_extended INTEGER,
                cache_read_tokens INTEGER,
                input_tokens INTEGER,
                decision_json TEXT,
                usage_totals_json TEXT
            );
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,
                review_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                action TEXT NOT NULL,
                gist TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                base_hash TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE TABLE IF NOT EXISTS skill_registry (
                name TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                pinned INTEGER NOT NULL DEFAULT 0,
                protection TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id TEXT NOT NULL,
                harness TEXT,
                session_id TEXT NOT NULL,
                call_index INTEGER NOT NULL,
                model TEXT,
                payload_json TEXT NOT NULL,
                response_json TEXT,
                created_at TEXT NOT NULL,
                evidence_kind TEXT NOT NULL DEFAULT 'legacy_pre_serialization',
                call_status TEXT,
                wire_body TEXT,
                endpoint TEXT,
                account_scope TEXT,
                request_headers_json TEXT,
                response_headers_json TEXT,
                http_status INTEGER,
                raw_response TEXT,
                response_id TEXT,
                duration_seconds REAL,
                finished_at TEXT
            );
        """)
        self._connection.commit()

    def _review_exists(self, review_id):
        row = self._connection.execute("SELECT 1 FROM reviews WHERE id = ?", (review_id,)).fetchone()
        return row is not None

    def close(self):
        with self._lock:
            self._connection.close()

    def insert_submission(self, fields):
        submission_id = new_id("sub")
        with self._lock:
            session_name = fields.get("session_name")
            if not session_name:
                previous = self._connection.execute(
                    "SELECT session_name FROM submissions WHERE harness = ? AND session_id = ? AND session_name IS NOT NULL ORDER BY rowid DESC LIMIT 1",
                    (fields["harness"], fields["session_id"]),
                ).fetchone()
                session_name = previous["session_name"] if previous else None
            inserted = True
            try:
                self._connection.execute(
                    """INSERT INTO submissions (
                        id, harness, session_id, watermark, trigger_name, delegate_depth,
                        model, reasoning, system_prompt, tools_json, messages_json, received_at, context_window,
                         capture_json, temperature, session_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        submission_id, fields["harness"], fields["session_id"], fields["watermark"],
                        fields.get("trigger_name"), fields["delegate_depth"], fields.get("model"),
                        fields.get("reasoning"), fields.get("system_prompt"),
                        json.dumps(fields.get("tools") or []), json.dumps(fields["messages"]), now(),
                        fields.get("context_window"),
                        json.dumps(fields["capture"]) if fields.get("capture") is not None else None,
                        fields.get("temperature"),
                        session_name,
                    ),
                )
            except sqlite3.IntegrityError:
                inserted = False
            if session_name:
                self._connection.execute(
                    "UPDATE submissions SET session_name = ? WHERE harness = ? AND session_id = ?",
                    (session_name, fields["harness"], fields["session_id"]),
                )
            self._connection.commit()
        return submission_id if inserted else None

    def set_session_name(self, harness, session_id, name):
        if not isinstance(name, str) or not name.strip():
            return 0
        with self._lock:
            updated = self._connection.execute(
                "UPDATE submissions SET session_name = ? WHERE harness = ? AND session_id = ?",
                (name.strip(), harness, session_id),
            )
            self._connection.commit()
            return updated.rowcount

    def add_review(self, submission_id, harness, session_id, called_model):
        review_id = new_id("rv")
        with self._lock:
            self._connection.execute(
                """INSERT INTO reviews (
                    id, submission_id, harness, session_id, status, called_model
                ) VALUES (?, ?, ?, ?, 'queued', ?)""",
                (review_id, submission_id, harness, session_id, called_model),
            )
            self._connection.commit()
        return review_id

    def cancel_open(self, harness, session_id):
        with self._lock:
            queued = self._connection.execute(
                """SELECT id FROM reviews
                   WHERE harness = ? AND session_id = ? AND status = 'queued'""",
                (harness, session_id),
            ).fetchall()
            cancelled = [row["id"] for row in queued]
            if cancelled:
                self._connection.execute(
                    """UPDATE reviews
                       SET status = 'cancelled', outcome = 'cancelled', finished_at = ?, cancel_requested = 1
                       WHERE harness = ? AND session_id = ? AND status = 'queued'""",
                    (now(), harness, session_id),
                )
            self._connection.execute(
                """UPDATE reviews SET cancel_requested = 1
                   WHERE harness = ? AND session_id = ? AND status = 'running'""",
                (harness, session_id),
            )
            self._connection.commit()
        return cancelled

    def claim_next(self):
        with self._lock:
            running = self._connection.execute(
                "SELECT id FROM reviews WHERE status = 'running' LIMIT 1"
            ).fetchone()
            if running:
                return None
            self._connection.execute(
                """UPDATE reviews
                   SET status = 'cancelled', outcome = 'cancelled', finished_at = ?
                   WHERE status = 'queued' AND cancel_requested = 1""",
                (now(),),
            )
            row = self._connection.execute(
                """SELECT id FROM reviews
                   WHERE status = 'queued' AND cancel_requested = 0
                   ORDER BY rowid LIMIT 1"""
            ).fetchone()
            if row is None:
                self._connection.commit()
                return None
            self._connection.execute(
                "UPDATE reviews SET status = 'running', started_at = ? WHERE id = ?",
                (now(), row["id"]),
            )
            self._connection.commit()
            review_id = row["id"]
        return self.review_context(review_id)

    def start_probe(self, model, reasoning, messages):
        """Reserve the worker slot for an explicit diagnostic, never a skill review."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if self._connection.execute("SELECT 1 FROM reviews WHERE status IN ('queued', 'running') LIMIT 1").fetchone():
                    raise SkillServiceError("Cache probe requires an idle review queue")
                submission_id, review_id, session_id = new_id("sub"), new_id("rv"), new_id("probe")
                self._connection.execute(
                    """INSERT INTO submissions
                       (id,harness,session_id,watermark,trigger_name,delegate_depth,model,reasoning,messages_json,received_at)
                       VALUES (?,'cache-probe',?,?,'diagnostic',0,?,?,?,?)""",
                    (submission_id, session_id, session_id, model, reasoning, json.dumps(messages), now()),
                )
                self._connection.execute(
                    """INSERT INTO reviews
                       (id,submission_id,harness,session_id,status,called_model,started_at)
                       VALUES (?,?,'cache-probe',?,'running',?,?)""",
                    (review_id, submission_id, session_id, model, now()),
                )
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return self.review_context(review_id)

    def review_context(self, review_id):
        with self._lock:
            review = self._connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            if review is None:
                return None
            submission = self._connection.execute(
                "SELECT * FROM submissions WHERE id = ?", (review["submission_id"],)
            ).fetchone()
        return {
            "id": review["id"],
            "status": review["status"],
            "called_model": review["called_model"],
            "cancel_requested": bool(review["cancel_requested"]),
            "messages": json.loads(submission["messages_json"]),
            "model": submission["model"],
            "reasoning": submission["reasoning"],
            "system_prompt": submission["system_prompt"],
            "context_window": submission["context_window"] if "context_window" in submission.keys() else None,
            "tools": json.loads(submission["tools_json"] or "[]"),
            "harness": submission["harness"],
            "session_id": submission["session_id"],
            "session_name": submission["session_name"],
            "trigger_name": submission["trigger_name"],
            "capture": json.loads(submission["capture_json"]) if submission["capture_json"] else None,
            "watermark": submission["watermark"],
            "temperature": submission["temperature"],
        }

    def cancel_requested(self, review_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT cancel_requested FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
        if row is None:
            return True
        return bool(row["cancel_requested"])

    def delete_session(self, harness, session_id):
        with self._lock:
            reviews = self._connection.execute(
                "SELECT id FROM reviews WHERE harness = ? AND session_id = ?",
                (harness, session_id),
            ).fetchall()
            review_ids = [row["id"] for row in reviews]
            if review_ids:
                marks = ",".join("?" for _ in review_ids)
                self._connection.execute(
                    f"UPDATE reviews SET cancel_requested = 1 WHERE id IN ({marks})",
                    review_ids,
                )
                for table in ("evidence", "proposals", "model_calls"):
                    self._connection.execute(
                        f"DELETE FROM {table} WHERE review_id IN ({marks})",
                        review_ids,
                    )
                self._connection.execute(f"DELETE FROM reviews WHERE id IN ({marks})", review_ids)
            self._connection.execute(
                "DELETE FROM model_calls WHERE harness = ? AND session_id = ?",
                (harness, session_id),
            )
            self._connection.execute(
                "DELETE FROM submissions WHERE harness = ? AND session_id = ?",
                (harness, session_id),
            )
            self._connection.commit()
        return len(review_ids)

    def finish(self, review_id, outcome, error=None):
        with self._lock:
            current = self._connection.execute(
                "SELECT status FROM reviews WHERE id = ?", (review_id,)
            ).fetchone()
            if current is None or current["status"] != "running":
                return
            self._connection.execute(
                """UPDATE reviews
                   SET status = ?, outcome = ?, error = ?, finished_at = ?
                   WHERE id = ?""",
                (outcome, outcome, error, now(), review_id),
            )
            self._connection.commit()

    def add_evidence(self, review_id, kind, payload):
        with self._lock:
            if not self._review_exists(review_id):
                return
            self._connection.execute(
                "INSERT INTO evidence (review_id, kind, payload_json, created_at) VALUES (?, ?, ?, ?)",
                (review_id, kind, json.dumps(payload), now()),
            )
            self._connection.commit()

    def evidence(self, review_id):
        with self._lock:
            rows = self._connection.execute(
                "SELECT kind, payload_json FROM evidence WHERE review_id = ? ORDER BY id",
                (review_id,),
            ).fetchall()
        return [{"kind": row["kind"], "payload": json.loads(row["payload_json"])} for row in rows]

    def add_model_call(self, review_id, harness, session_id, call_index, model, payload, response,
                       *, evidence_kind="legacy_pre_serialization", call_status=None):
        with self._lock:
            if not self._review_exists(review_id):
                return
            cursor = self._connection.execute(
                """INSERT INTO model_calls (
                    review_id, harness, session_id, call_index, model, payload_json, response_json, created_at,
                    evidence_kind, call_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id, harness, session_id, call_index, model,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(response, ensure_ascii=False) if response is not None else None,
                    now(), evidence_kind, call_status,
                ),
            )
            self._connection.commit()
            return cursor.lastrowid

    def start_model_call(self, review_id, harness, session_id, call_index, model, payload):
        return self.add_model_call(
            review_id, harness, session_id, call_index, model, payload, None,
            evidence_kind="prepared", call_status="attempted",
        )

    def save_wire_request(self, call_id, endpoint, headers, body, account_scope=None):
        with self._lock:
            self._connection.execute(
                """UPDATE model_calls SET evidence_kind = 'wire', wire_body = ?, endpoint = ?,
                   request_headers_json = ?, account_scope = ? WHERE id = ?""",
                (body, endpoint, json.dumps(headers), account_scope, call_id),
            )
            self._connection.commit()

    def save_wire_response(self, call_id, status, body, headers):
        with self._lock:
            self._connection.execute(
                """UPDATE model_calls SET http_status = ?, raw_response = ?, response_headers_json = ?
                   WHERE id = ?""",
                (status, body, json.dumps(headers), call_id),
            )
            self._connection.commit()

    def finish_model_call(self, call_id, response, duration):
        provider_response = response.get("provider_response") or {}
        with self._lock:
            self._connection.execute(
                """UPDATE model_calls SET response_json = ?, response_id = ?, call_status = ?,
                   duration_seconds = ?, finished_at = ? WHERE id = ?""",
                (json.dumps(response, ensure_ascii=False), provider_response.get("id"),
                 "failed" if "error" in response else "completed", duration, now(), call_id),
            )
            self._connection.commit()

    def refresh_usage(self, review_id):
        with self._lock:
            rows = self._connection.execute(
                "SELECT response_json FROM model_calls WHERE review_id = ? ORDER BY id", (review_id,),
            ).fetchall()
            totals = sum_usage([{"response": json.loads(row["response_json"]) if row["response_json"] else None} for row in rows])
            input_tokens = totals["input"] if totals["known"]["input"] and not totals["partial"]["input"] else None
            cache_tokens = totals["hit"] if totals["known"]["hit"] and not totals["partial"]["hit"] else None
            self._connection.execute(
                "UPDATE reviews SET input_tokens = ?, cache_read_tokens = ?, usage_totals_json = ? WHERE id = ?",
                (input_tokens, cache_tokens, json.dumps(totals), review_id),
            )
            self._connection.commit()

    def list_model_calls(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM model_calls ORDER BY session_id, id"
            ).fetchall()
        return [_model_call(row) for row in rows]

    def model_call_usage(self):
        """Dashboard counters without request bodies or attribution trees."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT id, review_id, call_index, call_status, duration_seconds,
                          CASE WHEN json_type(response_json, '$.usage') = 'object'
                               THEN json_remove(json_extract(response_json, '$.usage'), '$.attribution', '$.host_counters')
                          END AS usage_json
                   FROM model_calls ORDER BY id"""
            ).fetchall()
        return [
            {"id": row["id"], "review_id": row["review_id"], "call_index": row["call_index"],
             "call_status": row["call_status"], "duration_seconds": row["duration_seconds"],
             "response": {"usage": json.loads(row["usage_json"]) if row["usage_json"] is not None else None}}
            for row in rows
        ]

    def model_calls_for_review(self, review_id):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM model_calls WHERE review_id = ? ORDER BY call_index, id",
                (review_id,),
            ).fetchall()
        return [_model_call(row) for row in rows]

    def add_proposal(self, review_id, skill_name, action, gist, payload, base_hash):
        proposal_id = new_id("sp")
        with self._lock:
            if not self._review_exists(review_id):
                return None
            self._connection.execute(
                """INSERT INTO proposals (
                    id, review_id, skill_name, action, gist, payload_json, base_hash, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (proposal_id, review_id, skill_name, action, gist, json.dumps(payload), base_hash, now()),
            )
            self._connection.commit()
        return proposal_id

    def proposal(self, proposal_id):
        with self._lock:
            row = self._connection.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        return _proposal(row)

    def pending_proposals(self):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM proposals WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        return [_proposal(row) for row in rows]

    def pending_for_review(self, review_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT id FROM proposals WHERE review_id = ? AND status = 'pending' LIMIT 1",
                (review_id,),
            ).fetchone()
        return row is not None

    def proposals_for_review(self, review_id):
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM proposals WHERE review_id = ? ORDER BY created_at",
                (review_id,),
            ).fetchall()
        return [_proposal(row) for row in rows]

    def mark_proposal(self, proposal_id, status):
        with self._lock:
            self._connection.execute(
                "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ?",
                (status, now(), proposal_id),
            )
            self._connection.commit()

    def skill(self, name):
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM skill_registry WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            return None
        return {
            "name": row["name"],
            "origin": row["origin"],
            "pinned": bool(row["pinned"]),
            "protection": row["protection"],
        }

    def save_skill(self, name, origin, pinned=False, protection=None):
        with self._lock:
            existing = self._connection.execute(
                "SELECT created_at FROM skill_registry WHERE name = ?", (name,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now()
            self._connection.execute(
                """INSERT INTO skill_registry (name, origin, pinned, protection, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     origin = excluded.origin,
                     pinned = excluded.pinned,
                     protection = excluded.protection""",
                (name, origin, int(pinned), protection, created_at),
            )
            self._connection.commit()

    def set_pinned(self, name, pinned):
        with self._lock:
            row = self._connection.execute(
                "SELECT origin, protection FROM skill_registry WHERE name = ?", (name,)
            ).fetchone()
            if row is None:
                return False
            self._connection.execute(
                "UPDATE skill_registry SET pinned = ? WHERE name = ?",
                (int(pinned), name),
            )
            self._connection.commit()
        return True

    def review_row(self, review_id):
        with self._lock:
            row = self._connection.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def save_context(self, review_id, mode, tools_extended, called_model, decision=None):
        with self._lock:
            self._connection.execute(
                """UPDATE reviews
                   SET context_mode = ?, tools_extended = ?, called_model = ?, decision_json = ?
                   WHERE id = ?""",
                (mode, int(tools_extended), called_model, json.dumps(decision) if decision is not None else None, review_id),
            )
            self._connection.commit()

    def save_usage(self, review_id, cache_read_tokens, input_tokens):
        with self._lock:
            self._connection.execute(
                "UPDATE reviews SET cache_read_tokens = ?, input_tokens = ? WHERE id = ?",
                (cache_read_tokens, input_tokens, review_id),
            )
            self._connection.commit()

    def list_reviews(self):
        with self._lock:
            rows = self._connection.execute(
                """SELECT r.*, s.watermark, s.trigger_name, s.delegate_depth, s.session_name, s.received_at
                   FROM reviews r JOIN submissions s ON s.id = r.submission_id
                   ORDER BY r.rowid"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_submissions(self):
        with self._lock:
            rows = self._connection.execute(
                """SELECT s.*, r.id AS review_id, r.status AS review_status, r.outcome, r.context_mode, r.called_model
                   FROM submissions s LEFT JOIN reviews r ON r.submission_id = s.id
                   ORDER BY s.received_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def list_proposals(self, *, include_payload=True):
        columns = "*" if include_payload else "id, review_id, skill_name, action, gist, base_hash, status, created_at, decided_at"
        with self._lock:
            rows = self._connection.execute(f"SELECT {columns} FROM proposals ORDER BY created_at").fetchall()
        return [_proposal(row) for row in rows]

    def reviews_for_session(self, harness, session_id):
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM reviews WHERE harness = ? AND session_id = ? ORDER BY rowid""",
                (harness, session_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def submission_count(self):
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) AS count FROM submissions").fetchone()["count"]

    def review_count(self):
        with self._lock:
            return self._connection.execute("SELECT COUNT(*) AS count FROM reviews").fetchone()["count"]


def _proposal(row):
    return {
        "id": row["id"],
        "review_id": row["review_id"],
        "skill_name": row["skill_name"],
        "action": row["action"],
        "gist": row["gist"],
        "payload": json.loads(row["payload_json"]) if "payload_json" in row.keys() else None,
        "base_hash": row["base_hash"],
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


def _model_call(row):
    return {
        "id": row["id"],
        "review_id": row["review_id"],
        "harness": row["harness"],
        "session_id": row["session_id"],
        "call_index": row["call_index"],
        "model": row["model"],
        "payload": json.loads(row["payload_json"]),
        "response": json.loads(row["response_json"]) if row["response_json"] else None,
        "created_at": row["created_at"],
        "evidence_kind": row["evidence_kind"],
        "call_status": row["call_status"],
        "wire_body": row["wire_body"],
        "endpoint": row["endpoint"],
        "account_scope": row["account_scope"],
        "request_headers": json.loads(row["request_headers_json"]) if row["request_headers_json"] else None,
        "response_headers": json.loads(row["response_headers_json"]) if row["response_headers_json"] else None,
        "http_status": row["http_status"],
        "raw_response": row["raw_response"],
        "response_id": row["response_id"],
        "duration_seconds": row["duration_seconds"],
        "finished_at": row["finished_at"],
    }
