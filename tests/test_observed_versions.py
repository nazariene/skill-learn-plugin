import json
import tempfile
import unittest
from pathlib import Path

from skill_learn.core import Core


class ObservedVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        (self.home / "settings.yaml").write_text("library:\n  root: skills\nnotifications:\n  enabled: false\n")
        self.core = Core(self.home)
        self.addCleanup(self.core.close)
        self.sequence = 0
        self.directory = self.core.library.root / "guide"
        (self.directory / "references").mkdir(parents=True)
        self.document = self.directory / "SKILL.md"
        self.document.write_text("---\nname: guide\ndescription: Use for work.\nmetadata:\n  origin: generated\n---\nOriginal guide.\n")
        self.support = self.directory / "references/example.md"
        self.support.write_text("Original example.")

    def request(self, operation, **payload):
        self.sequence += 1
        result = self.core.request(str(self.sequence), operation, payload)
        self.assertTrue(result["ok"], result)
        return result["result"]

    def review(self, load=True):
        self.request("enqueue", harness="opencode", sessionID="parent", hostID="host", watermark=str(self.sequence), messages=[])
        review = self.request("claim", owner="owner", hostID="host")["plan"]["reviewID"]
        if load:
            snapshot = self.request("skill-loaded", reviewID=review, name="guide", location=str(self.directory), body="Old host snapshot")
            self.assertEqual(snapshot["snapshot"]["files"][0]["content"], self.document.read_text())
        return review

    def finish(self, review, *changes):
        return self.request("finish", reviewID=review, text=json.dumps({"changes": [{"name": "guide", **change} for change in changes]}))

    def test_unobserved_and_stale_rewrites_patches_and_support_files_are_refused(self):
        changes = [
            {"action": "edit", "content": "Use for work.\nReplacement."},
            {"action": "patch", "old_string": "Original guide.", "new_string": "Replacement."},
            {"action": "write_file", "file_path": "references/example.md", "content": "Replacement."},
            {"action": "patch", "file_path": "references/example.md", "old_string": "Original example.", "new_string": "Replacement."},
        ]
        for change in changes:
            with self.subTest(change=change):
                review = self.review(load=False)
                self.assertEqual(self.finish(review, change)["publication"]["refused"], 1)
                review = self.review()
                target = self.support if change.get("file_path") else self.document
                target.write_text(target.read_text() + "\nConcurrent work.")
                before = target.read_bytes()
                result = self.finish(review, change)
                self.assertEqual(result["outcome"], "failed")
                self.assertIn("stale change refused", result["error"])
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(self.core.store.proposals_for_review(review), [])

    def test_same_review_advances_only_its_successful_changes(self):
        review = self.review()
        result = self.finish(review,
            {"action": "patch", "old_string": "Original guide.", "new_string": "First revision."},
            {"action": "patch", "old_string": "First revision.", "new_string": "Second revision."},
            {"action": "write_file", "file_path": "references/new.md", "content": "New support."},
            {"action": "patch", "file_path": "references/new.md", "old_string": "New support.", "new_string": "Updated support."})
        self.assertEqual(result["publication"], {"applied": 4, "staged": 0, "refused": 0, "warnings": []})
        self.assertIn("Second revision.", self.document.read_text())
        self.assertEqual((self.directory / "references/new.md").read_text(), "Updated support.")

    def test_new_file_collision_and_disappeared_support_are_stale(self):
        review = self.review()
        target = self.directory / "references/new.md"
        target.write_text("Other writer.")
        self.support.unlink()
        result = self.finish(review,
            {"action": "write_file", "file_path": "references/new.md", "content": "Replacement."},
            {"action": "write_file", "file_path": "references/example.md", "content": "Replacement."})
        self.assertEqual(result["publication"]["refused"], 2)
        self.assertEqual(target.read_text(), "Other writer.")
        self.assertFalse(self.support.exists())

    def test_approval_rechecks_observed_baseline_and_stale_removal(self):
        self.core.library.approval_generated = "manual"
        review = self.review()
        self.support.write_text("Concurrent support change.")
        result = self.finish(review,
            {"action": "patch", "old_string": "Original guide.", "new_string": "Absorbed example."},
            {"action": "remove_file", "file_path": "references/example.md"})
        self.assertEqual(result["publication"]["staged"], 1)
        self.assertEqual(result["publication"]["refused"], 1)
        proposal = self.core.store.proposals_for_review(review)[0]
        observed = self.core.store.observed_files(review, str(self.directory))["SKILL.md"]
        self.assertEqual(proposal["base_hash"], observed)
        self.document.write_text(self.document.read_text() + "\nOperator edit.")
        self.assertFalse(self.core.library.approve(proposal["id"])["ok"])
        self.assertEqual(self.core.store.proposal(proposal["id"])["status"], "pending")
        self.assertEqual(self.support.read_text(), "Concurrent support change.")
