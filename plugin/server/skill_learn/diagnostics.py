import json
import os
import sqlite3
import time
import fcntl
import shutil
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path

from .settings import load_settings


def process_alive(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return None


def status(home):
    settings = load_settings(home)
    result = {"home": str(settings.home), "settings": asdict(settings), "workers": [],
              "databases": {}, "queue": {}, "active": [], "recent": [], "recentFailures": [], "lastSuccess": None, "fallbacks": [], "warnings": []}
    for path in sorted((settings.home / "workers").glob("*.json")):
        try:
            worker = json.loads(path.read_text())
            worker["alive"] = process_alive(worker.get("pid"))
            worker["heartbeatAgeSeconds"] = max(0, time.time() - worker["updatedAt"])
            worker["health"] = "stopped" if worker["alive"] is False else "responsive" if worker["alive"] and worker["heartbeatAgeSeconds"] <= settings.lease_seconds else "stale"
            result["workers"].append(worker)
        except (OSError, ValueError, KeyError, TypeError) as error:
            result["warnings"].append(f"Worker {path.name}: {error}")
    connections = {}
    resources = ExitStack()
    try:
        # Copy both databases and WALs under the writer's lock. SQLite may write
        # SHM bookkeeping even with mode=ro; keep that in a private snapshot.
        snapshot = Path(resources.enter_context(tempfile.TemporaryDirectory(prefix="skill-learn-status-")))
        with ExitStack() as locks:
            lock_path = settings.home / "core.lock"
            if lock_path.is_file():
                lock = locks.enter_context(lock_path.open("rb"))
                fcntl.flock(lock, fcntl.LOCK_SH)
            for name in ("state", "runtime"):
                for suffix in (".sqlite", ".sqlite-wal"):
                    source = settings.home / (name + suffix)
                    if source.is_file():
                        shutil.copyfile(source, snapshot / source.name)
        for name in ("state", "runtime"):
            path = snapshot / f"{name}.sqlite"
            result["databases"][name] = "missing"
            if not path.is_file():
                continue
            try:
                connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                connections[name] = connection
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                result["databases"][name] = "readable"
            except sqlite3.Error as error:
                result["databases"][name] = "unreadable"
                result["warnings"].append(f"{name}: {error}")
        state = connections.get("state")
        if state and result["databases"]["state"] == "readable":
            try:
                result["queue"] = dict(state.execute("SELECT status,count(*) FROM reviews WHERE status IN ('queued','running') GROUP BY status"))
                select = """SELECT r.id,r.session_id,s.session_name,r.status,r.outcome,r.error,r.started_at,r.finished_at,
                    r.context_mode,r.called_model,json_extract(r.decision_json,'$.reason') AS fallback_reason,
                    (SELECT payload_json FROM evidence e WHERE e.review_id=r.id AND e.kind='publication' ORDER BY e.id DESC LIMIT 1) AS publication_json
                    FROM reviews r JOIN submissions s ON s.id=r.submission_id"""
                result["recent"] = [dict(row) for row in state.execute(select + " ORDER BY r.rowid DESC LIMIT 10")]
                success = state.execute(select + " WHERE r.outcome IN ('applied','staged','unchanged') ORDER BY r.rowid DESC LIMIT 1").fetchone()
                result["lastSuccess"] = dict(success) if success else None
                result["fallbacks"] = [dict(row) for row in state.execute(select + " WHERE r.context_mode='digest' ORDER BY r.rowid DESC LIMIT 10")]
                for row in state.execute(select + " WHERE r.status='running'"):
                    active = dict(row)
                    runtime = connections.get("runtime")
                    lease = runtime.execute("SELECT owner,host_id,host_pid,reviewer_id,lease_until,recovery_state FROM review_state WHERE id=?", (row["id"],)).fetchone() if runtime else None
                    if lease:
                        active.update(dict(lease))
                    active["leaseExpired"] = (active.get("lease_until") or 0) <= time.time()
                    active["hostAlive"] = process_alive(active.get("host_pid"))
                    result["active"].append(active)
                result["recentFailures"] = [dict(row) for row in state.execute(select + " WHERE r.error IS NOT NULL ORDER BY r.rowid DESC LIMIT 10")]
            except sqlite3.Error as error:
                result["warnings"].append(f"Unsupported or incomplete database schema: {error}")
    finally:
        for connection in connections.values():
            connection.close()
        resources.close()
    return result
