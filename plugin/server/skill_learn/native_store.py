import hashlib
import fcntl
import json
import time
import os

from .errors import SkillServiceError
from .store import Store, new_id, now


class NativeStore(Store):
    def bind_call_assistant(self, review_id, call_id, assistant_id):
        with self._lock:
            row = self._connection.execute("SELECT payload_json FROM model_calls WHERE id=? AND review_id=?", (call_id, review_id)).fetchone()
            if row is None:
                raise SkillServiceError("Unknown native call")
            payload = json.loads(row["payload_json"])
            if payload.get("assistantID") not in {None, assistant_id}:
                raise SkillServiceError("Native call already belongs to another assistant")
            payload["assistantID"] = assistant_id
            self._connection.execute("UPDATE model_calls SET payload_json=? WHERE id=?", (json.dumps(payload), call_id))
            self._connection.commit()

    def _create(self):
        with (self.home / "core.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            columns = {row[1] for row in self._connection.execute("PRAGMA main.table_info(reviews)")}
            tables = {row[0] for row in self._connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
            if columns & {"owner", "host_id", "reviewer_id", "lease_until", "inherited_json", "plan_json", "recovery_state", "host_pid"} or tables & {"operations", "native_events", "internal_sessions", "probes"}:
                raise SkillServiceError("Unsupported database schema; current split state.sqlite/runtime.sqlite databases are required")
            super()._create()
            self._connection.execute("ATTACH DATABASE ? AS runtime", (str(self.home / "runtime.sqlite"),))
            self._create_runtime()
            self._connection.execute("""CREATE TEMP VIEW native_reviews AS
                SELECT r.*, n.owner, n.host_id, n.reviewer_id, n.lease_until,
                       n.inherited_json, n.plan_json, n.recovery_state, n.host_pid
                FROM main.reviews r LEFT JOIN runtime.review_state n ON n.id=r.id""")

    def _create_runtime(self):
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS runtime.review_state (
                id TEXT PRIMARY KEY, owner TEXT, host_id TEXT, reviewer_id TEXT,
                lease_until REAL, inherited_json TEXT, plan_json TEXT,
                recovery_state TEXT, host_pid INTEGER
            );
            CREATE TABLE IF NOT EXISTS runtime.operations (
                id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                response_json TEXT, created_at TEXT NOT NULL, operation TEXT, review_id TEXT
            );
            CREATE TABLE IF NOT EXISTS runtime.native_events (
                review_id TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(review_id,event_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS runtime.native_reviewer_identity
                ON review_state(host_id,reviewer_id) WHERE reviewer_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS runtime.internal_sessions (
                host_id TEXT NOT NULL, reviewer_id TEXT NOT NULL, review_id TEXT NOT NULL,
                plan_json TEXT, inherited_json TEXT, host_pid INTEGER, owner TEXT,
                PRIMARY KEY(host_id,reviewer_id)
            );
            CREATE TABLE IF NOT EXISTS runtime.probes (
                id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, mode TEXT NOT NULL,
                status TEXT NOT NULL, review_id TEXT, error TEXT, created_at TEXT NOT NULL
            );
        """)
        self._connection.commit()

    def review_row(self, review_id):
        with self._lock:
            row = self._connection.execute("SELECT * FROM native_reviews WHERE id=?", (review_id,)).fetchone()
        return dict(row) if row else None

    def list_reviews(self):
        with self._lock:
            rows = self._connection.execute("""SELECT r.*, s.watermark, s.trigger_name, s.delegate_depth, s.session_name, s.received_at
                FROM native_reviews r JOIN main.reviews m ON m.id=r.id
                JOIN submissions s ON s.id=r.submission_id ORDER BY m.rowid""").fetchall()
        return [dict(row) for row in rows]

    def reviews_for_session(self, harness, session_id):
        with self._lock:
            rows = self._connection.execute("SELECT r.* FROM native_reviews r JOIN main.reviews m ON m.id=r.id WHERE r.harness=? AND r.session_id=? ORDER BY m.rowid", (harness, session_id)).fetchall()
        return [dict(row) for row in rows]

    def assign_owner(self, review_id, owner, host_id, host_pid, seconds):
        with self._lock:
            self._connection.execute("""INSERT INTO runtime.review_state (id,owner,host_id,host_pid,lease_until) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,host_id=excluded.host_id,host_pid=excluded.host_pid,lease_until=excluded.lease_until""",
                (review_id, owner, host_id, host_pid, time.time() + seconds))
            self._connection.commit()

    def begin_operation(self, identity, operation, payload):
        fingerprint = hashlib.sha256(json.dumps([operation, payload], sort_keys=True).encode()).hexdigest()
        with self._lock:
            row = self._connection.execute("SELECT * FROM runtime.operations WHERE id=?", (identity,)).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise SkillServiceError("Operation identity reused with different arguments")
                if row["status"] != "done":
                    raise SkillServiceError("Interrupted operation requires reconciliation; it will not be replayed")
                return json.loads(row["response_json"])
            publication = "finish" if operation == "reconcile" and payload.get("state") == "finished" else operation
            self._connection.execute("INSERT INTO runtime.operations (id,fingerprint,status,response_json,created_at,operation,review_id) VALUES (?,?,'started',NULL,?,?,?)", (identity, fingerprint, now(), publication, payload.get("reviewID")))
            self._connection.commit()
        return None

    def end_operation(self, identity, response):
        result = response.get("result") or {}
        review_id = None
        if isinstance(result, dict):
            review_id = result.get("reviewId") or (result.get("plan") or {}).get("reviewID")
        with self._lock:
            self._connection.execute("UPDATE runtime.operations SET status='done', response_json=?,review_id=COALESCE(review_id,?) WHERE id=?", (json.dumps(response), review_id, identity))
            self._connection.commit()

    def enqueue(self, fields):
        # Caller holds the cross-process home lock across this state transition.
        inserted = self.insert_submission(fields)
        if inserted is None:
            return {"disposition": "duplicate", "cancelled": []}
        cancelled = self.cancel_open(fields["harness"], fields["session_id"])
        active = [row for row in self.reviews_for_session(fields["harness"], fields["session_id"]) if row["status"] == "running"]
        if fields["delegate_depth"]:
            return {"disposition": "not-reviewed", "cancelled": cancelled}
        review_id = self.add_review(inserted, fields["harness"], fields["session_id"], fields.get("model"))
        return {"disposition": "queued", "reviewId": review_id, "cancelled": cancelled,
                "abort": [{"reviewId": row["id"], "reviewerID": row["reviewer_id"], "hostID": row["host_id"]} for row in active]}

    def claim(self, owner, host_id, seconds, host_scope=None, host_pid=None):
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                active = self._connection.execute("SELECT * FROM native_reviews WHERE status='running' LIMIT 1").fetchone()
                if active:
                    self._connection.commit()
                    return {"active": dict(active), "needsReconcile": (active["lease_until"] or 0) <= time.time()}
                queued = self._connection.execute("SELECT r.*,s.capture_json FROM reviews r JOIN submissions s ON s.id=r.submission_id WHERE r.status='queued' AND r.cancel_requested=0 ORDER BY r.rowid").fetchall()
                row = None
                for candidate in queued:
                    capture = json.loads(candidate["capture_json"] or "{}")
                    if capture.get("hostID") == host_id:
                        row = candidate
                        break
                    if host_scope and capture.get("hostScope") == host_scope and type(capture.get("hostPID")) is int:
                        try:
                            os.kill(capture["hostPID"], 0)
                        except ProcessLookupError:
                            capture.update(hostID=host_id, hostPID=host_pid, compatibility={"compatible": False, "reason": "host-reconnected-profile"})
                            self._connection.execute("UPDATE submissions SET capture_json=? WHERE id=?", (json.dumps(capture), candidate["submission_id"]))
                            row = candidate
                            break
                        except PermissionError:
                            pass
                if row is None:
                    self._connection.commit()
                    return {}
                self._connection.execute("UPDATE reviews SET status='running',started_at=? WHERE id=?", (now(), row["id"]))
                self._connection.execute("""INSERT INTO runtime.review_state (id,owner,host_id,host_pid,lease_until) VALUES (?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,host_id=excluded.host_id,host_pid=excluded.host_pid,lease_until=excluded.lease_until""",
                                         (row["id"], owner, host_id, capture.get("hostPID"), time.time() + seconds))
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return {"job": self.review_context(row["id"])}

    def save_plan(self, review_id, plan):
        with self._lock:
            self._connection.execute("UPDATE runtime.review_state SET plan_json=? WHERE id=?", (json.dumps(plan), review_id))
            self._connection.execute("UPDATE reviews SET context_mode=?,decision_json=?,called_model=? WHERE id=?",
                                     (plan["mode"], json.dumps(plan["decision"]), plan["model"]["providerID"] + "/" + plan["model"]["modelID"], review_id))
            self._connection.commit()

    def bind(self, review_id, reviewer_id, inherited, owner):
        row = self.review_row(review_id)
        if not row or row["owner"] != owner:
            raise SkillServiceError("Review no longer belongs to this owner")
        if row["reviewer_id"] and row["reviewer_id"] != reviewer_id:
            raise SkillServiceError("Review already bound to a different native session")
        with self._lock:
            self._connection.execute("INSERT OR IGNORE INTO runtime.internal_sessions VALUES (?,?,?,?,?,?,?)", (row["host_id"], reviewer_id, review_id, row["plan_json"], json.dumps(inherited), row["host_pid"], owner))
            self._connection.execute("UPDATE runtime.review_state SET reviewer_id=?,inherited_json=? WHERE id=?", (reviewer_id, json.dumps(inherited), review_id))
            self._connection.commit()
        if row["status"] != "running" or row["cancel_requested"]:
            raise SkillServiceError("Review is cancelled; native identity retained for abort/reconciliation")

    def reviewers(self, host_id=None):
        with self._lock:
            rows = self._connection.execute("""SELECT i.review_id AS id,i.host_id,i.reviewer_id,i.plan_json,
                i.inherited_json,i.host_pid,i.owner,COALESCE(r.status,'deleted') AS status,
                r.cancel_requested,r.lease_until FROM runtime.internal_sessions i LEFT JOIN native_reviews r ON r.id=i.review_id
                WHERE ? IS NULL OR i.host_id=?""", (host_id, host_id)).fetchall()
        return [dict(row) for row in rows]

    def request_probe(self, parent_id, mode):
        with self._lock:
            if self._connection.execute("SELECT 1 FROM reviews WHERE status IN ('queued','running') LIMIT 1").fetchone() or self._connection.execute("SELECT 1 FROM runtime.probes WHERE status='requested' LIMIT 1").fetchone():
                raise SkillServiceError("Cache probe requires an idle learning queue")
            identity = new_id("probe")
            self._connection.execute("INSERT INTO runtime.probes VALUES (?,?,?,'requested',NULL,NULL,?)", (identity, parent_id, mode, now()))
            self._connection.commit()
            return identity

    def next_probe(self):
        with self._lock:
            row = self._connection.execute("SELECT * FROM runtime.probes WHERE status='requested' ORDER BY created_at LIMIT 1").fetchone()
        return dict(row) if row else None

    def probe(self, identity):
        with self._lock:
            row = self._connection.execute("SELECT * FROM runtime.probes WHERE id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def update_probe(self, identity, status, review_id=None, error=None):
        with self._lock:
            self._connection.execute("UPDATE runtime.probes SET status=?,review_id=COALESCE(?,review_id),error=? WHERE id=?", (status, review_id, error, identity))
            self._connection.commit()

    def unfinished_publication(self, review_id, except_identity):
        with self._lock:
            return self._connection.execute("SELECT 1 FROM runtime.operations WHERE review_id=? AND operation='finish' AND status='started' AND id!=? LIMIT 1", (review_id, except_identity or "")).fetchone() is not None

    def adopt_owner(self, review_id, owner):
        with self._lock:
            self._connection.execute("UPDATE runtime.review_state SET owner=? WHERE id=?", (owner, review_id))
            self._connection.execute("UPDATE runtime.internal_sessions SET owner=? WHERE review_id=?", (owner, review_id))
            self._connection.commit()

    def delete_session(self, harness, session_id):
        review_ids = [row["id"] for row in self.reviews_for_session(harness, session_id)]
        with self._lock:
            for review_id in review_ids:
                self._connection.execute("DELETE FROM runtime.native_events WHERE review_id=?", (review_id,))
                self._connection.execute("DELETE FROM runtime.operations WHERE review_id=?", (review_id,))
                self._connection.execute("DELETE FROM runtime.review_state WHERE id=?", (review_id,))
                self._connection.execute("UPDATE runtime.internal_sessions SET plan_json='{}',inherited_json='[]',owner=NULL WHERE review_id=?", (review_id,))
            self._connection.commit()
        return super().delete_session(harness, session_id)

    def heartbeat(self, review_id, owner, seconds):
        with self._lock:
            cursor = self._connection.execute("UPDATE runtime.review_state SET lease_until=? WHERE id=? AND owner=? AND EXISTS(SELECT 1 FROM main.reviews WHERE id=? AND status='running')", (time.time() + seconds, review_id, owner, review_id))
            self._connection.commit()
            return bool(cursor.rowcount)

    def event_once(self, review_id, event_id, payload):
        with self._lock:
            cursor = self._connection.execute("INSERT OR IGNORE INTO runtime.native_events VALUES (?,?,?)", (review_id, event_id, json.dumps(payload)))
            self._connection.commit()
            return bool(cursor.rowcount)

    def set_recovery(self, review_id, state):
        with self._lock:
            self._connection.execute("UPDATE runtime.review_state SET recovery_state=? WHERE id=?", (state, review_id))
            self._connection.commit()

    def interrupt_attempts(self, review_id):
        with self._lock:
            self._connection.execute("UPDATE model_calls SET call_status='interrupted',finished_at=? WHERE review_id=? AND call_status='attempted'", (now(), review_id))
            self._connection.commit()

    def finish_native(self, review_id, outcome, error=None):
        self.finish(review_id, outcome, error)
        with self._lock:
            self._connection.execute("UPDATE runtime.review_state SET lease_until=NULL WHERE id=? AND EXISTS(SELECT 1 FROM main.reviews WHERE id=? AND status!='running')", (review_id, review_id))
            self._connection.commit()
