import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from skill_learn.core import Core
from skill_learn.diagnostics import status


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def snapshot(self):
        return {str(path.relative_to(self.root)): (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def test_status_does_not_initialize_a_missing_home_or_invalid_database(self):
        home = self.root / "missing"
        command = subprocess.run([sys.executable, "-m", "skill_learn", "--home", str(home), "status"], capture_output=True, text=True)
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)["databases"], {"state": "missing", "runtime": "missing"})
        self.assertFalse(home.exists())
        home.mkdir()
        (home / "state.sqlite").write_bytes(b"not a database")
        before = self.snapshot()
        result = status(home)
        self.assertEqual(result["databases"]["state"], "unreadable")
        self.assertEqual(self.snapshot(), before)

    def test_status_reports_worker_queue_results_and_fallback_without_writes(self):
        home = self.root / "home"
        home.mkdir()
        (home / "settings.yaml").write_text("library:\n  root: skills\nnotifications:\n  enabled: false\n")
        core = Core(home)
        self.addCleanup(core.close)
        sequence = 0
        def request(operation, **payload):
            nonlocal sequence
            sequence += 1
            return core.request(str(sequence), operation, payload)["result"]
        request("state", owner="worker", hostID="host", hostPID=os.getpid())
        for name in ("success", "failure", "active", "queued"):
            queued = request("enqueue", harness="opencode", hostID="host", hostPID=os.getpid(), sessionID=name,
                             sessionName=name.title(), watermark="one", messages=[])
            if name == "queued":
                continue
            request("claim", owner="worker", hostID="host")
            if name in {"success", "failure"}:
                request("finish", reviewID=queued["reviewId"], text="Nothing to save." if name == "success" else "", error="Failure evidence" if name == "failure" else None)
        before = self.snapshot()
        result = status(home)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["workers"][0]["health"], "responsive")
        self.assertEqual(result["queue"], {"queued": 1, "running": 1})
        self.assertEqual(result["lastSuccess"]["session_name"], "Success")
        self.assertEqual(result["recentFailures"][0]["error"], "Failure evidence")
        self.assertEqual(result["fallbacks"][0]["fallback_reason"], "selected-model-variant-differs")
        self.assertFalse(result["active"][0]["leaseExpired"])
        self.assertEqual(result["settings"]["max_fork_input_tokens"], 250000)
        self.assertEqual(result["warnings"], [])
        marker = home / "workers" / f"{os.getpid()}.json"
        worker = json.loads(marker.read_text())
        worker["updatedAt"] = time.time() - 120
        marker.write_text(json.dumps(worker))
        core.store._connection.execute("UPDATE runtime.review_state SET lease_until=0")
        core.store._connection.commit()
        before = self.snapshot()
        result = status(home)
        self.assertEqual(result["workers"][0]["health"], "stale")
        self.assertTrue(result["active"][0]["leaseExpired"])
        self.assertEqual(self.snapshot(), before)

    def test_status_reports_unsupported_schema_without_migrating(self):
        with sqlite3.connect(self.root / "state.sqlite") as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
        before = self.snapshot()
        self.assertIn("Unsupported", status(self.root)["warnings"][0])
        self.assertEqual(self.snapshot(), before)
