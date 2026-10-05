import sqlite3
import tempfile
import unittest
from pathlib import Path

from skill_learn.native_store import NativeStore
from skill_learn.store import Store
from skill_learn.errors import SkillServiceError


def schema(connection):
    tables = {}
    for name, in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        columns = sorted(tuple(row)[1:] for row in connection.execute(f"PRAGMA table_info({name})"))
        indexes = sorted((row[2], row[3], row[4], tuple(column[2] for column in connection.execute(f"PRAGMA index_info({row[1]})")))
                         for row in connection.execute(f"PRAGMA index_list({name})"))
        tables[name] = (columns, indexes)
    return tables


class SchemaParityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.reference = sqlite3.connect(":memory:")
        self.addCleanup(self.reference.close)
        self.reference.executescript((Path(__file__).parent / "fixtures/standalone-state.sql").read_text())

    def test_learning_schema_matches_standalone_and_native_bookkeeping_is_separate(self):
        store = NativeStore(self.home)
        self.addCleanup(store.close)
        self.assertEqual(schema(store._connection), schema(self.reference))
        with sqlite3.connect(self.home / "runtime.sqlite") as runtime:
            self.assertEqual(set(schema(runtime)), {"review_state", "operations", "native_events", "internal_sessions", "probes"})

    def current_store(self):
        store = NativeStore(self.home)
        submission = store.insert_submission({"harness": "opencode", "session_id": "parent", "watermark": "one", "delegate_depth": 0,
                                              "messages": [{"role": "user", "content": "Preserve history"}], "session_name": "Existing session",
                                              "capture": {"hostID": "host"}})
        review = store.add_review(submission, "opencode", "parent", "openai/test")
        store.add_evidence(review, "host_observed", {"usage": 42})
        call = store.add_model_call(review, "opencode", "reviewer", 1, "openai/test", {"request": "original"}, {"usage": {"input_tokens": 42}}, evidence_kind="host_requested", call_status="completed")
        proposal = store.add_proposal(review, "existing", "patch", "Preserve proposal", {"new_string": "kept"}, "base")
        store.save_skill("existing", "generated", pinned=True)
        store.close()
        return review, call, proposal

    def test_current_store_reopens_without_losing_learning_records_or_native_identities(self):
        review, call, proposal = self.current_store()
        store = NativeStore(self.home)
        self.assertEqual(store.claim("worker", "host", 60)["job"]["id"], review)
        store.save_plan(review, {"mode": "fork", "reviewID": review, "decision": {}, "model": {"providerID": "openai", "modelID": "test"}})
        store.bind(review, "reviewer", ["inherited"], "worker")
        store.begin_operation("unfinished", "finish", {"reviewID": review})
        store.event_once(review, "step", {})
        before = {table: [dict(row) for row in store._connection.execute(f"SELECT * FROM main.{table}")]
                  for table in schema(self.reference) if table != "sqlite_sequence"}
        store.close()
        for _ in range(2):
            store = NativeStore(self.home)
            try:
                self.assertEqual(schema(store._connection), schema(self.reference))
                for table, rows in before.items():
                    self.assertEqual([dict(row) for row in store._connection.execute(f"SELECT * FROM main.{table}")], rows)
                row = store.review_row(review)
                self.assertEqual((row["owner"], row["reviewer_id"]), ("worker", "reviewer"))
                self.assertEqual(store.reviewers()[0]["reviewer_id"], "reviewer")
                self.assertTrue(store.unfinished_publication(review, None))
                self.assertFalse(store.event_once(review, "step", {"duplicate": True}))
                self.assertEqual(store.model_calls_for_review(review)[0]["id"], call)
                self.assertEqual(store.proposal(proposal)["payload"], {"new_string": "kept"})
                self.assertTrue(store.skill("existing")["pinned"])
                self.assertTrue(store.heartbeat(review, "worker", 60))
            finally:
                store.close()

    def test_unsupported_combined_schema_is_rejected_without_conversion(self):
        store = Store(self.home)
        store._connection.execute("ALTER TABLE reviews ADD COLUMN owner TEXT")
        store._connection.commit()
        store.close()
        with self.assertRaisesRegex(SkillServiceError, "Unsupported database schema"):
            NativeStore(self.home)
        with sqlite3.connect(self.home / "state.sqlite") as connection:
            self.assertIn("owner", {row[1] for row in connection.execute("PRAGMA table_info(reviews)")})
        self.assertFalse((self.home / "runtime.sqlite").exists())

    def test_current_queued_review_can_be_claimed_and_heartbeated(self):
        review, _, _ = self.current_store()
        store = NativeStore(self.home)
        self.addCleanup(store.close)
        self.assertEqual(store.claim("worker", "host", 60)["job"]["id"], review)
        self.assertTrue(store.heartbeat(review, "worker", 60))
        self.assertEqual(schema(store._connection), schema(self.reference))
