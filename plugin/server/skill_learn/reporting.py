import fcntl
import hashlib
import json
import os
import re
import shutil
import uuid
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from . import report_view
from .library import Library
from .store import _model_call, _proposal

MANIFEST = ".skill-learn-report.json"
FORMAT = "skill-learn-static-v1"


class ReportSnapshot:
    """Read inside generate's transaction; decode large records only when needed."""
    def __init__(self, store, settings, days):
        self.home = settings.home
        self.store = store
        self.days = days
        self.since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days is not None else None
        self.reviews = [review for review in store.list_reviews() if self.since is None or review["received_at"] >= self.since]
        condition = "" if self.since is None else """WHERE review_id IN (
            SELECT r.id FROM reviews r JOIN submissions s ON s.id = r.submission_id WHERE s.received_at >= ?)"""
        parameters = () if self.since is None else (self.since,)
        self.proposals = [_proposal(row) for row in store._connection.execute(
            f"SELECT * FROM proposals {condition} ORDER BY created_at", parameters)]
        # Sort row references, not full request/response bodies in SQLite's temp B-tree.
        self.call_rows = sorted(store._connection.execute(f"SELECT * FROM model_calls {condition} ORDER BY id", parameters),
                                key=lambda row: (row["session_id"], row["id"]))
        self.calls_by_review = {}
        for row in self.call_rows:
            self.calls_by_review.setdefault(row["review_id"], []).append(row)
        self.call_usage = store.model_call_usage(since=self.since)
        self.skills = Library(settings.library_root, store, settings.approval_generated).list_skills()
        self.contexts = {}
        self.evidence_by_review = {}
        for row in store._connection.execute(f"SELECT review_id, kind, payload_json FROM evidence {condition} ORDER BY id", parameters):
            self.evidence_by_review.setdefault(row["review_id"], []).append(row)
        for review in self.reviews:
            review["publication_json"] = next((row["payload_json"] for row in reversed(self.evidence_by_review.get(review["id"], [])) if row["kind"] == "publication"), None)
        self.proposals_by_review = {}
        for proposal in self.proposals:
            self.proposals_by_review.setdefault(proposal["review_id"], []).append(proposal)
        self.delegates = []
        self.delegate_evidence = {}
        for submission in store.list_submissions(delegates_only=True, since=self.since):
            self.delegates.append({"id": submission["id"], "harness": submission["harness"], "session_id": submission["session_id"], "session_name": submission["session_name"], "received_at": submission["received_at"], "status": "not-reviewed", "context_mode": None})
            self.delegate_evidence[submission["id"]] = submission

    def review_fingerprint(self, review):
        submission = self.store._connection.execute("SELECT * FROM submissions WHERE id=?", (review["submission_id"],)).fetchone()
        return _fingerprint([review, submission, *self.calls_by_review.get(review["id"], []),
                             *self.evidence_by_review.get(review["id"], []), *self.proposals_for_review(review["id"])])

    def list_reviews(self):
        return self.reviews + self.delegates

    def list_proposals(self, *, include_payload=True):
        return self.proposals if include_payload else [{**proposal, "payload": None} for proposal in self.proposals]

    def list_model_calls(self):
        return [_model_call(row) for row in self.call_rows]

    def model_call_usage(self):
        return self.call_usage

    def review_context(self, review_id):
        if review_id not in self.contexts:
            self.contexts = {review_id: self.store.review_context(review_id)}
        return self.contexts[review_id]

    def model_calls_for_review(self, review_id):
        return [_model_call(row) for row in self.calls_by_review.get(review_id, [])]

    def proposals_for_review(self, review_id):
        return self.proposals_by_review.get(review_id, [])

    def evidence(self, review_id):
        return [{"kind": row["kind"], "payload": json.loads(row["payload_json"])}
                for row in self.evidence_by_review.get(review_id, [])]


def _fingerprint(records):
    digest = hashlib.sha256()
    for record in records:
        digest.update(len(record.keys()).to_bytes(8, "big"))
        for key in record.keys():
            for value in (key, record[key]):
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                data = text.encode("utf-8")
                digest.update(b"s" if isinstance(value, str) else b"j")
                digest.update(len(data).to_bytes(8, "big"))
                digest.update(data)
    return digest.hexdigest()


def _renderer_fingerprint():
    digest = hashlib.sha256()
    for name in ("reporting.py", "report_view.py", "store.py", "usage.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def generate(store, settings, *, days=7):
    if days is not None and (type(days) is not int or days < 1):
        raise ValueError("Report days must be a positive integer; use --all for all history")
    root = settings.reports_root
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".skill-learn-report.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with store._lock:
            store._connection.execute("BEGIN")
            try:
                return _publish(root, ReportSnapshot(store, settings, days), settings)
            finally:
                store._connection.rollback()


def _publish(root, snapshot, settings):
    manifest = root / MANIFEST
    previous = json.loads(manifest.read_text()) if manifest.is_file() else None
    if previous and previous.get("format") != FORMAT:
        raise ValueError("Report manifest belongs to another publisher")
    if (root / "index.html").is_file():
        marker = re.search(r'<meta name="skill-learn-generation" content="(g_[a-f0-9]{32})">', (root / "index.html").read_text(encoding="utf-8"))
        if marker:
            previous = {"format": FORMAT, "generation": marker.group(1)}
    if (root / "index.html").exists() and previous is None:
        raise ValueError("Existing index.html is not a managed learning report")
    generations = root / ".generations"
    if generations.is_symlink():
        raise ValueError("Report generations cannot be a symlink")
    generations.mkdir(exist_ok=True)
    renderer = _renderer_fingerprint()
    old = None
    owned = {}
    if previous and re.fullmatch(r"g_[a-f0-9]{32}", previous.get("generation", "")):
        candidate = generations / previous["generation"]
        ownership = candidate / MANIFEST
        if not candidate.is_symlink() and ownership.is_file() and not ownership.is_symlink():
            owned = json.loads(ownership.read_text())
            if owned.get("format") == FORMAT and owned.get("renderer") == renderer:
                old = candidate
    previous_files = set(owned.get("files", []))
    previous_fingerprints = owned.get("fingerprints", {})
    generation = "g_" + uuid.uuid4().hex
    directory = generations / generation
    directory.mkdir()
    index_temporary = root / (".index-" + generation)
    manifest_temporary = root / (".manifest-" + generation)
    published = False
    try:
        files = []
        fingerprints = {}

        def reuse(names, fingerprint):
            fingerprints[names[-1]] = fingerprint
            if old is None or previous_fingerprints.get(names[-1]) != fingerprint:
                return False
            if any(name not in previous_files or (old / name).is_symlink() or not (old / name).is_file() for name in names):
                return False
            for name in names:
                os.link(old / name, directory / name)
            return True

        for row in snapshot.call_rows:
            if type(row["id"]) is not int or row["id"] < 1:
                raise ValueError("Unsafe model-call evidence identity")
            name = f'call-{row["id"]}.js'
            if not reuse([name], _fingerprint([row])):
                call = _model_call(row)
                payload = json.dumps({"id": f'call-{call["id"]}', "html": report_view._model_call_evidence(call)})
                (directory / name).write_text('document.dispatchEvent(new CustomEvent("skill-learn-call-evidence", {detail: ' + payload + '}));\n', encoding="utf-8")
            files.append(name)
        for review in snapshot.reviews:
            identity = review["id"]
            if not re.fullmatch(r"rv_[a-zA-Z0-9]+", identity):
                raise ValueError("Unsafe review page identity")
            name = f"context-{identity}.js"
            page_name = f"review-{identity}.html"
            if reuse([name, page_name], snapshot.review_fingerprint(review)):
                files.extend([name, page_name])
                continue
            payload = json.dumps({"id": identity, "context": snapshot.review_context(identity)}, ensure_ascii=False, separators=(",", ":"))
            (directory / name).write_text('document.dispatchEvent(new CustomEvent("skill-learn-context-evidence", {detail: JSON.parse(' + json.dumps(payload, ensure_ascii=False) + ')}));\n', encoding="utf-8")
            files.append(name)
            page = report_view.render_review(snapshot, identity, lazy_context=True)
            (directory / page_name).write_text(page, encoding="utf-8")
            files.append(page_name)
        for delegate in snapshot.delegates:
            identity = delegate["id"]
            if not re.fullmatch(r"sub_[a-zA-Z0-9]+", identity):
                raise ValueError("Unsafe delegate page identity")
            body = report_view._navigation("sessions") + '<a href="sessions.html">Review sessions</a><h1>' + escape(delegate.get("session_name") or "Delegate session") + '</h1><p class="identifier">[' + escape(delegate["session_id"]) + ']</p><p>Delegate — not reviewed.</p>' + report_view._fold("Stored host submission", '<pre>' + escape(report_view._pretty(snapshot.delegate_evidence[identity])) + '</pre>')
            (directory / f"review-{identity}.html").write_text(report_view._page("Delegate session", body), encoding="utf-8")
            files.append(f"review-{identity}.html")
        for section in ("sessions", "skills"):
            name = section + ".html"
            (directory / name).write_text(report_view.render_home(snapshot, snapshot.skills, section), encoding="utf-8")
            files.append(name)
        index = report_view.render_home(snapshot, snapshot.skills)
        (directory / "index.html").write_text(index, encoding="utf-8")
        index = index.replace('<head>', '<head><meta name="skill-learn-generation" content="' + generation + '"><base href="./.generations/' + generation + '/">', 1)
        files.append("index.html")
        (directory / MANIFEST).write_text(json.dumps({"format": FORMAT, "files": files, "renderer": renderer, "fingerprints": fingerprints}), encoding="utf-8")
        index_temporary.write_text(index, encoding="utf-8")
        manifest_temporary.write_text(json.dumps({"format": FORMAT, "generation": generation}), encoding="utf-8")
        # All referenced pages are complete before this single entrypoint swap.
        os.replace(index_temporary, root / "index.html")
        published = True
        try:
            os.replace(manifest_temporary, manifest)
        except OSError as error:
            # The complete index contains its authoritative generation marker.
            # An advisory-manifest failure after publication is not a failed
            # generation and cannot invalidate the newly complete snapshot.
            print(f"skill-learn report manifest: {error}", file=sys.stderr)
        if previous and re.fullmatch(r"g_[a-f0-9]{32}", previous.get("generation", "")):
            old = generations / previous["generation"]
            if not old.is_symlink() and old.is_dir():
                ownership = old / MANIFEST
                if ownership.is_file() and not ownership.is_symlink():
                    owned = json.loads(ownership.read_text())
                    if owned.get("format") == FORMAT:
                        for name in owned.get("files", []):
                            if name in {"index.html", "sessions.html", "skills.html"} or re.fullmatch(r"review-(?:rv|sub)_[a-zA-Z0-9]+\.html|call-[0-9]+\.js|context-rv_[a-zA-Z0-9]+\.js", name):
                                (old / name).unlink(missing_ok=True)
                        ownership.unlink()
                        try:
                            old.rmdir()
                        except OSError:
                            pass
        return root / "index.html"
    except BaseException:
        if not published:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        index_temporary.unlink(missing_ok=True)
        manifest_temporary.unlink(missing_ok=True)
