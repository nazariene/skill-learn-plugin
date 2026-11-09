import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from skill_learn.core import Core


class ProjectSkillTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        (self.home / "settings.yaml").write_text("library:\n  root: skills\nnotifications:\n  enabled: false\n")
        self.project = self.root / "project"
        self.skills = self.project / ".opencode" / "skills"
        self.skills.mkdir(parents=True)
        self.core = Core(self.home)
        self.addCleanup(lambda: self.core.close())
        self.sequence = 0

    def request(self, operation, **payload):
        self.sequence += 1
        answer = self.core.request(str(self.sequence), operation, payload)
        self.assertTrue(answer["ok"], answer)
        return answer["result"]

    def skill(self, root, name="guide", generated=True):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        text = f"---\nname: {name}\ndescription: Use for project work.\n"
        if generated:
            text += "metadata:\n  origin: generated\n"
        document = directory / "SKILL.md"
        document.write_text(text + "---\nOriginal guide.\n")
        return document

    def job(self):
        queued = self.request("enqueue", harness="opencode", sessionID="parent", hostID="host", directory=str(self.project),
                              projectDirectory=str(self.project), messages=[], watermark=str(self.sequence))
        plan = self.request("claim", hostID="host", owner="owner")["plan"]
        self.assertEqual(plan["reviewID"], queued["reviewId"])
        documents = {path.parent.name: path for path in self.core.library.root.glob("*/SKILL.md")}
        documents.update({path.parent.name: path for path in self.skills.glob("*/SKILL.md")})
        for name, document in documents.items():
            self.request("skill-loaded", reviewID=plan["reviewID"], name=name, location=str(document.parent), body=document.read_text())
        return plan

    def finish(self, plan, changes):
        return self.request("finish", reviewID=plan["reviewID"], text=json.dumps({"changes": changes}))

    def patch(self, **extra):
        return {"name": "guide", "action": "patch", "old_string": "Original guide.", "new_string": "Updated guide.", **extra}

    def test_project_shadows_global_and_records_its_publication_destination(self):
        global_document = self.skill(self.core.library.root)
        self.core.store.save_skill("guide", "generated", pinned=True)
        document = self.skill(self.skills)
        plan = self.job()
        self.assertIn("Skill index (project)", plan["instruction"])
        load = self.request("skill-loaded", reviewID=plan["reviewID"], name="guide", location=str(document.parent), body=document.read_text())
        self.assertTrue(load["discoveryCompatible"])
        self.assertEqual(self.finish(plan, [self.patch()])["outcome"], "applied")
        self.assertIn("Updated guide.", document.read_text())
        self.assertIn("Original guide.", global_document.read_text())
        self.assertTrue(self.core.store.skill("guide")["pinned"])
        proposal = self.core.store.proposals_for_review(plan["reviewID"])[0]
        self.assertEqual(proposal["payload"]["projectDirectory"], str(self.project))
        self.assertEqual(proposal["payload"]["libraryRoot"], str(self.skills))

    def test_explicit_global_scope_does_not_edit_the_project_copy(self):
        global_document = self.skill(self.core.library.root)
        document = self.skill(self.skills)
        plan = self.job()
        self.request("skill-loaded", reviewID=plan["reviewID"], name="guide", location=str(global_document.parent), body=global_document.read_text())
        self.assertEqual(self.finish(plan, [self.patch(scope="global")])["outcome"], "applied")
        self.assertIn("Updated guide.", global_document.read_text())
        self.assertIn("Original guide.", document.read_text())

    def test_project_creates_and_support_files_use_scoped_registry(self):
        plan = self.job()
        changes = [{"name": "guide", "action": "create", "scope": "project", "description": "Use for project work.", "content": "Original guide."}]
        self.assertEqual(self.finish(plan, changes)["outcome"], "applied")
        self.assertTrue((self.skills / "guide/SKILL.md").is_file())
        self.assertFalse((self.core.library.root / "guide").exists())
        library = self.core.project_library(str(self.project))
        self.assertEqual(self.core.store.skill(library.registry_key("guide"))["origin"], "generated")
        self.assertIsNone(self.core.store.skill("guide"))
        self.assertEqual(self.finish(self.job(), [{"name": "guide", "action": "write_file", "file_path": "references/example.md", "content": "Example."}])["outcome"], "applied")
        self.assertEqual((self.skills / "guide/references/example.md").read_text(), "Example.")

    def test_manual_proposal_routes_after_restart_and_rechecks_pin(self):
        self.core.settings = replace(self.core.settings, approval_generated="manual")
        document = self.skill(self.skills)
        plan = self.job()
        self.assertEqual(self.finish(plan, [self.patch()])["outcome"], "staged")
        proposal = self.core.store.proposals_for_review(plan["reviewID"])[0]
        self.core.close()
        self.core = Core(self.home)
        self.request("pin", id="guide", projectDirectory=str(self.project))
        self.assertFalse(self.request("approve", id=proposal["id"])["ok"])
        self.assertIn("Original guide.", document.read_text())
        self.assertIsNone(self.core.store.skill("guide"))
        self.request("unpin", id="guide", projectDirectory=str(self.project))
        self.assertTrue(self.request("approve", id=proposal["id"])["ok"])
        self.assertIn("Updated guide.", document.read_text())

    def test_project_user_ownership_and_protection_are_not_bypassed(self):
        global_document = self.skill(self.core.library.root)
        document = self.skill(self.skills, generated=False)
        answer = self.finish(self.job(), [self.patch()])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("user-owned", answer["error"])
        self.assertIn("Original guide.", global_document.read_text())
        self.request("adopt", id="guide", projectDirectory=str(self.project))
        library = self.core.project_library(str(self.project))
        self.core.store.save_skill(library.registry_key("guide"), "generated", protection="external")
        answer = self.finish(self.job(), [self.patch()])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("external", answer["error"])
        self.assertIn("Original guide.", document.read_text())

    def test_external_load_cannot_be_used_to_patch_same_named_global_skill(self):
        global_document = self.skill(self.core.library.root)
        other = self.skill(self.root / "other/.opencode/skills")
        plan = self.job()
        load = self.request("skill-loaded", reviewID=plan["reviewID"], name="guide", location=str(other.parent), body=other.read_text())
        self.assertFalse(load["discoveryCompatible"])
        answer = self.finish(plan, [self.patch()])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("does not match", answer["error"])
        self.assertIn("Original guide.", global_document.read_text())
        self.assertIn("Original guide.", other.read_text())

    def test_project_root_and_package_symlink_escapes_are_rejected(self):
        other = self.root / "other"
        outside = self.skill(other)
        (self.skills / "guide").symlink_to(outside.parent, target_is_directory=True)
        answer = self.finish(self.job(), [self.patch(scope="project")])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("Original guide.", outside.read_text())
        self.skills.rename(self.project / ".opencode/saved-skills")
        self.skills.symlink_to(other, target_is_directory=True)
        self.assertIsNone(self.core.project_library(str(self.project)))
        answer = self.finish(self.job(), [self.patch(scope="project")])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("Original guide.", outside.read_text())

    def test_pending_proposal_rejects_retargeted_project_library(self):
        self.core.settings = replace(self.core.settings, approval_generated="manual")
        self.skill(self.skills)
        plan = self.job()
        self.finish(plan, [self.patch()])
        proposal = self.core.store.proposals_for_review(plan["reviewID"])[0]
        replacement = self.project / "replacement"
        outside = self.skill(replacement)
        self.skills.rename(self.project / ".opencode/saved-skills")
        self.skills.symlink_to(replacement, target_is_directory=True)
        answer = self.request("approve", id=proposal["id"])
        self.assertFalse(answer["ok"])
        self.assertIn("moved", answer["error"])
        self.assertIn("Original guide.", outside.read_text())

    def test_disappeared_project_target_never_falls_back_to_global(self):
        global_document = self.skill(self.core.library.root)
        document = self.skill(self.skills)
        plan = self.job()
        document.rename(document.with_name("saved.md"))
        answer = self.finish(plan, [self.patch()])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("does not exist", answer["error"])
        self.assertIn("Original guide.", global_document.read_text())

    def test_project_alias_cannot_bypass_the_real_skill_pin(self):
        document = self.skill(self.skills, name="protected")
        self.request("pin", id="protected", projectDirectory=str(self.project))
        (self.skills / "guide").symlink_to(document.parent, target_is_directory=True)
        answer = self.finish(self.job(), [self.patch(scope="project")])
        self.assertEqual(answer["outcome"], "failed")
        self.assertIn("Original guide.", document.read_text())

    def test_missing_project_library_cannot_be_created_implicitly(self):
        self.skills.rmdir()
        plan = self.job()
        answer = self.finish(plan, [{"name": "guide", "action": "create", "scope": "project", "content": "Use for project work."}])
        self.assertEqual(answer["outcome"], "failed")
        self.assertFalse(self.skills.exists())


if __name__ == "__main__":
    unittest.main()
