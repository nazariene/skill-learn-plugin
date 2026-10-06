import fcntl
import json
import os
import re
import shutil
import uuid
import sys
from html import escape
from pathlib import Path

from . import report_view
from .library import Library

MANIFEST = ".skill-learn-report.json"
FORMAT = "skill-learn-static-v1"


class ReportSnapshot:
    """Stable read transaction shared by the index and all detail pages."""
    def __init__(self, store, settings):
        self.home = settings.home
        with store._lock:
            store._connection.execute("BEGIN")
            try:
                self.reviews = store.list_reviews()
                self.proposals = store.list_proposals()
                self.calls = store.list_model_calls()
                self.call_usage = store.model_call_usage()
                self.skills = Library(settings.library_root, store, settings.approval_generated).list_skills()
                self.contexts = {review["id"]: store.review_context(review["id"]) for review in self.reviews}
                self.evidence_by_review = {review["id"]: store.evidence(review["id"]) for review in self.reviews}
                self.delegates = []
                self.delegate_evidence = {}
                for submission in store.list_submissions(delegates_only=True):
                    self.delegates.append({"id": submission["id"], "harness": submission["harness"], "session_id": submission["session_id"], "session_name": submission["session_name"], "received_at": submission["received_at"], "status": "not-reviewed", "context_mode": None})
                    self.delegate_evidence[submission["id"]] = submission
            finally:
                store._connection.rollback()

    def list_reviews(self):
        return self.reviews + self.delegates

    def list_proposals(self, *, include_payload=True):
        return self.proposals if include_payload else [{**proposal, "payload": None} for proposal in self.proposals]

    def list_model_calls(self):
        return self.calls

    def model_call_usage(self):
        return self.call_usage

    def review_context(self, review_id):
        return self.contexts.get(review_id)

    def model_calls_for_review(self, review_id):
        return [call for call in self.calls if call["review_id"] == review_id]

    def proposals_for_review(self, review_id):
        return [proposal for proposal in self.proposals if proposal["review_id"] == review_id]

    def evidence(self, review_id):
        return self.evidence_by_review.get(review_id, [])


def generate(store, settings):
    root = settings.reports_root
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".skill-learn-report.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish(root, ReportSnapshot(store, settings), settings)


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
    generation = "g_" + uuid.uuid4().hex
    directory = generations / generation
    directory.mkdir()
    index_temporary = root / (".index-" + generation)
    manifest_temporary = root / (".manifest-" + generation)
    published = False
    try:
        files = []
        for call in snapshot.calls:
            if type(call["id"]) is not int or call["id"] < 1:
                raise ValueError("Unsafe model-call evidence identity")
            name = f'call-{call["id"]}.js'
            payload = json.dumps({"id": f'call-{call["id"]}', "html": report_view._model_call_evidence(call)})
            (directory / name).write_text('document.dispatchEvent(new CustomEvent("skill-learn-call-evidence", {detail: ' + payload + '}));\n', encoding="utf-8")
            files.append(name)
        for review in snapshot.reviews:
            identity = review["id"]
            if not re.fullmatch(r"rv_[a-zA-Z0-9]+", identity):
                raise ValueError("Unsafe review page identity")
            name = f"context-{identity}.js"
            payload = json.dumps({"id": identity, "context": snapshot.review_context(identity)}, ensure_ascii=False, separators=(",", ":"))
            (directory / name).write_text('document.dispatchEvent(new CustomEvent("skill-learn-context-evidence", {detail: JSON.parse(' + json.dumps(payload, ensure_ascii=False) + ')}));\n', encoding="utf-8")
            files.append(name)
            page = report_view.render_review(snapshot, identity, lazy_context=True)
            (directory / f"review-{identity}.html").write_text(page, encoding="utf-8")
            files.append(f"review-{identity}.html")
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
        (directory / MANIFEST).write_text(json.dumps({"format": FORMAT, "files": files}), encoding="utf-8")
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
