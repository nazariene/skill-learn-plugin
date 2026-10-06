import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
import venv
import zipfile
from pathlib import Path
from unittest.mock import patch

from skill_learn.core import Core
from skill_learn.errors import SkillServiceError
from skill_learn.library import Library
from skill_learn.native_store import NativeStore
from skill_learn.protocol import run
from skill_learn.settings import load_settings
from skill_learn.usage import host_usage


class NativeCoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        config = patch.dict(os.environ, {"OPENCODE_CONFIG_DIR": str(self.home / "opencode")})
        config.start()
        self.addCleanup(config.stop)
        (self.home / "settings.yaml").write_text("library:\n  root: skills\nnotifications:\n  enabled: false\n")

    def tearDown(self):
        self.temporary.cleanup()

    def settings(self, text):
        (self.home / "settings.yaml").write_text(text)
        return load_settings(self.home)

    def test_defaults_paths_and_budgets(self):
        (self.home / "settings.yaml").unlink()
        settings = load_settings(self.home)
        self.assertEqual(settings.triggers["idle"]["seconds"], 120)
        self.assertEqual(settings.triggers["turns"]["count"], 25)
        self.assertEqual(settings.max_fork_input_tokens, 250000)
        self.assertEqual(settings.steps, 16)
        self.assertEqual(settings.library_root, self.home / "opencode/skills")
        self.assertEqual(settings.reports_root, self.home / "reports")
        self.assertEqual(settings.input_budget(400000), 300000)
        self.assertEqual(settings.input_budget(), 120000)
        self.assertEqual(self.settings("review:\n  maxInputTokens: 42\n").input_budget(400000), 42)
        self.assertIsNone(self.settings("review:\n  maxInputTokens: 0\n").input_budget())
        self.assertIsNone(self.settings("review:\n  maxForkInputTokens: null\n").max_fork_input_tokens)
        with patch.dict(os.environ, {"SKILL_LEARNING_NOTIFICATIONS": "0"}):
            self.assertFalse(load_settings(self.home).notifications_enabled)

    def test_shipped_defaults_and_explicit_trigger_and_fork_overrides(self):
        example = Path(__file__).resolve().parents[1] / "docs/settings.example.yaml"
        settings = self.settings(example.read_text())
        self.assertEqual(settings.triggers["idle"]["seconds"], 120)
        self.assertEqual(settings.max_fork_input_tokens, 250000)
        self.assertIsNone(settings.max_input_tokens)
        explicit = self.settings("triggers:\n  idle:\n    seconds: 15\nreview:\n  maxForkInputTokens: 120000\n  maxInputTokens: 42\n")
        self.assertEqual(explicit.triggers["idle"]["seconds"], 15)
        self.assertEqual(explicit.max_fork_input_tokens, 120000)
        self.assertEqual(explicit.input_budget(400000), 42)

    def test_invalid_removed_and_duplicate_settings(self):
        for text in ("server: {}", "llm:\n  apiKey: secret", "llm:\n  authFile: auth.json", "llm:\n  selection: service", "llm:\n  selection: []", "approval:\n  generated: []", "reports:\n  enabled: true", "reports:\n  enabled: false", "review:\n  maxForkInputTokens: 0", "runtime:\n  leaseSeconds: true", "notifications:\n  volume: .nan", "reports:\n  root: reports\n  root: other-reports", "triggers:\n  idle:\n    seconds: -1", "library:\n  root: 12", "library:\n  root: ''", "library:\n  root: false", "library:\n  root: []"):
            with self.subTest(text=text), self.assertRaises(SkillServiceError):
                self.settings(text)

    def test_optional_library_uses_opencode_config_precedence_and_resolves_skill_symlinks(self):
        for environment, expected in (
            ({}, self.home / ".config/opencode/skills"),
            ({"XDG_CONFIG_HOME": str(self.home / "xdg")}, self.home / "xdg/opencode/skills"),
            ({"XDG_CONFIG_HOME": str(self.home / "xdg"), "OPENCODE_CONFIG_DIR": str(self.home / "custom-config")}, self.home / "custom-config/skills"),
        ):
            with self.subTest(environment=environment), patch.dict(os.environ, environment, clear=True), patch("skill_learn.settings.Path.home", return_value=self.home):
                for text in ("{}", "library: {}", "library:\n  root: null"):
                    self.assertEqual(self.settings(text).library_root, expected)
                self.assertEqual(self.settings("library:\n  root: skills").library_root, self.home / "skills")
                self.assertEqual(self.settings("library:\n  root: custom-library").library_root, self.home / "custom-library")
        config = self.home / "opencode"
        config.mkdir()
        library = self.home / "shared-skills"
        library.mkdir()
        (config / "skills").symlink_to(library, target_is_directory=True)
        (config / "plugins").symlink_to(self.home / "other-plugin-installation", target_is_directory=True)
        self.assertEqual(self.settings("library:\n  root: null").library_root, library)

    def test_default_library_reads_global_skills_and_publishes_only_generated_changes(self):
        self.settings("notifications:\n  enabled: false\n")
        root = self.home / "opencode/skills"
        user_skill = root / "user-procedure/SKILL.md"
        user_skill.parent.mkdir(parents=True)
        original = "---\nname: user-procedure\ndescription: Use for user work.\n---\nUser instructions."
        user_skill.write_text(original)
        core = Core(self.home)
        self.addCleanup(core.close)
        self.assertEqual(core.library.root, root)
        self.assertEqual([skill["id"] for skill in core.request("catalogue", "host-skills", {})["result"]], ["user-procedure"])
        review = self.enqueue(core, "enqueue")["reviewId"]
        core.request("claim", "claim", {"owner": "owner", "hostID": "host"})
        feedback = {"changes": [{"name": "generated-procedure", "action": "create", "content": "Use for repeatable work."}]}
        self.assertEqual(core.request("finish", "finish", {"reviewID": review, "text": json.dumps(feedback)})["result"]["outcome"], "applied")
        self.assertIn("origin: generated", (root / "generated-procedure/SKILL.md").read_text())
        self.assertFalse(core.library.apply_feedback(review, {"name": "user-procedure", "action": "edit", "content": "Replace user instructions."})["success"])
        self.assertEqual(user_skill.read_text(), original)
        self.assertFalse((self.home / "skills").exists())

    def test_absolute_expanded_paths_and_muted_notifications(self):
        settings = self.settings("library:\n  root: ~/skills\nreports:\n  root: /absolute/reports\nnotifications:\n  enabled: false\nreview:\n  maxInputTokens: -1\n")
        self.assertEqual(settings.library_root, Path.home() / "skills")
        self.assertEqual(settings.reports_root, Path("/absolute/reports"))
        self.assertIsNone(settings.input_budget())
        self.settings("notifications:\n  enabled: false\n")
        core = Core(self.home)
        self.addCleanup(core.close)
        self.assertFalse((self.home / "reports").exists())
        self.assertFalse(core.settings.notifications_enabled)

    def test_diagnostic_stdout_is_redirected_and_operation_identity_conflicts(self):
        original = Core.dispatch
        def noisy_dispatch(core, operation, payload):
            print("diagnostic noise")
            return original(core, operation, payload)
        output, diagnostics = io.StringIO(), io.StringIO()
        with patch.object(Core, "dispatch", noisy_dispatch), patch("sys.stderr", diagnostics):
            run(self.home, io.StringIO('{"version":1,"id":"hello","op":"hello","payload":{}}\n'), output)
        self.assertEqual(json.loads(output.getvalue())["id"], "hello")
        self.assertIn("diagnostic noise", diagnostics.getvalue())
        core = Core(self.home)
        self.addCleanup(core.close)
        self.enqueue(core, "same")
        with self.assertRaises(SkillServiceError):
            self.enqueue(core, "same", watermark="different")

    def test_management_entry_without_harness_or_credentials(self):
        response = subprocess.run([sys.executable, "-m", "skill_learn", "--home", str(self.home), "pending"], capture_output=True, text=True)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(json.loads(response.stdout), [])
        import skill_learn
        self.assertNotIn("server.client", sys.modules)
        self.assertNotIn("server.oauth", sys.modules)
        self.assertTrue(Path(skill_learn.__file__).is_file())

    def test_package_wheel_and_console_are_isolated(self):
        checkout = Path(__file__).resolve().parents[1] / "plugin/server"
        source, wheels, environment = self.home / "source", self.home / "wheels", self.home / "venv"
        source.mkdir()
        wheels.mkdir()
        shutil.copy2(checkout / "pyproject.toml", source)
        shutil.copytree(checkout / "skill_learn", source / "skill_learn", ignore=shutil.ignore_patterns("__pycache__"))
        env = {**os.environ, "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        env.pop("PYTHONPATH", None)
        build = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), "."], cwd=source, env=env, capture_output=True, text=True)
        self.assertEqual(build.returncode, 0, build.stderr)
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
        python = environment / "bin/python"
        wheel = next(wheels.glob("*.whl"))
        with zipfile.ZipFile(wheel) as archive:
            for cue in ("initiated", "started", "finished", "unchanged", "failed", "cancelled"):
                self.assertEqual(archive.read(f"skill_learn/sounds/{cue}.wav")[:4], b"RIFF")
        installed = subprocess.run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], cwd=self.home, env=env, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        smoke = subprocess.run([str(environment / "bin/skill-learn"), "--home", str(self.home / "fresh"), "pending"], cwd=self.home, env=env, capture_output=True, text=True)
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        self.assertEqual(json.loads(smoke.stdout), [])
        location = subprocess.run([str(python), "-c", "import skill_learn; print(skill_learn.__file__)"], cwd=self.home, env=env, capture_output=True, text=True)
        self.assertIn(str(environment), location.stdout)

    def test_correlated_protocol_unknown_version_and_large_stdin(self):
        text = "evidence" * 150000
        frames = [
            {"version": 2, "id": "bad", "op": "hello", "payload": {}},
            {"version": 1, "id": "hello", "op": "hello", "payload": {}},
            {"version": 1, "id": "large", "op": "enqueue", "payload": {"harness": "opencode", "sessionID": "parent", "watermark": "w", "hostID": "host", "messages": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": text}]}]}},
        ]
        output = io.StringIO()
        run(self.home, io.StringIO("\n".join(json.dumps(frame) for frame in frames) + "\n"), output)
        answers = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([answer["id"] for answer in answers], ["bad", "hello", "large"])
        self.assertFalse(answers[0]["ok"])
        self.assertTrue(answers[1]["ok"])
        self.assertEqual(answers[2]["result"]["disposition"], "queued")
        store = NativeStore(self.home)
        self.addCleanup(store.close)
        review_id = answers[2]["result"]["reviewId"]
        self.assertEqual(store.review_context(review_id)["messages"][0]["parts"][0]["text"], text)

    def test_config_failure_is_a_structured_learning_error(self):
        (self.home / "settings.yaml").write_text("llm:\n  apiKey: removed")
        output = io.StringIO()
        run(self.home, io.StringIO('{"version":1,"id":"hello","op":"hello","payload":{}}\n'), output)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def enqueue(self, core, identity, session="parent", watermark="w", **extra):
        return core.request(identity, "enqueue", {"harness": "opencode", "hostID": "host", "sessionID": session,
            "watermark": watermark, "messages": [{"info": {"id": "u1", "role": "user"}, "parts": [{"type": "text", "text": "learn"}]}], **extra})["result"]

    def test_single_claim_dedup_rename_supersession_and_completion(self):
        core, second = Core(self.home), Core(self.home)
        self.addCleanup(core.close)
        self.addCleanup(second.close)
        queued = self.enqueue(core, "enqueue1", sessionName="First name")
        duplicate = self.enqueue(second, "enqueue2", sessionName="Renamed")
        self.assertEqual(duplicate["disposition"], "duplicate")
        self.assertEqual(core.store.submission_count(), 1)
        self.assertEqual(core.store.review_context(queued["reviewId"])["session_name"], "Renamed")
        claimed = core.request("claim1", "claim", {"owner": "owner", "hostID": "host"})["result"]["plan"]
        self.assertIn("active", second.request("claim2", "claim", {"owner": "other", "hostID": "host"})["result"])
        self.assertEqual(self.enqueue(core, "delegate", session="child", delegateDepth=1)["disposition"], "not-reviewed")
        core.request("bind", "bind", {"reviewID": claimed["reviewID"], "reviewerID": "reviewer", "inheritedIDs": ["old"], "owner": "owner"})
        replacement = self.enqueue(second, "new", watermark="w2")
        self.assertEqual(replacement["abort"][0]["reviewerID"], "reviewer")
        completed = core.request("finish", "finish", {"reviewID": claimed["reviewID"], "text": '{"changes":[{"name":"new-skill","action":"create","content":"Use for a reusable procedure."}]}'})
        self.assertEqual(completed["result"]["outcome"], "cancelled")
        self.assertFalse((self.home / "skills" / "new-skill").exists())
        self.assertEqual(core.request("finish", "finish", {"reviewID": claimed["reviewID"], "text": '{"changes":[{"name":"new-skill","action":"create","content":"Use for a reusable procedure."}]}' }), completed)
        self.assertFalse((self.home / "reports").exists())

    def test_host_usage_retains_unknown_and_normalized_zero(self):
        self.assertIsNone(host_usage({})["input_tokens"])
        zero = host_usage({"input": 0, "cache": {"read": 0, "write": 0}})
        self.assertEqual(zero["input_tokens"], 0)
        self.assertIsNone(zero["raw_provider_usage"])
        self.assertEqual(zero["provider_counter_availability"], "unavailable")
        self.assertEqual(host_usage({"input": 2000, "cache": {"read": 8000, "write": 0}})["input_tokens"], 10000)

    def test_generated_ownership_atomic_package_and_stale_proposal(self):
        store = NativeStore(self.home)
        self.addCleanup(store.close)
        sub = store.insert_submission({"harness": "test", "session_id": "p", "watermark": "w", "messages": [], "delegate_depth": 0})
        review = store.add_review(sub, "test", "p", "model")
        library = Library(self.home / "skills", store, "auto")
        answer = library.apply_feedback(review, {"name": "procedure", "action": "create", "content": "Use for repeated work.\n\nOld text.", "files": [{"path": "references/guide.md", "content": "guide"}]})
        self.assertTrue(answer["applied"])
        body = library.read_skill("procedure")
        self.assertIn("origin: generated", body)
        self.assertEqual((library.root / "procedure/references/guide.md").read_text(), "guide")
        library.pin("procedure", True)
        self.assertFalse(library.apply_feedback(review, {"name": "procedure", "action": "patch", "old_string": "Old text.", "new_string": "New text."})["success"])
        library.pin("procedure", False)
        library.approval_generated = "manual"
        staged = library.apply_feedback(review, {"name": "procedure", "action": "patch", "old_string": "Old text.", "new_string": "New text."})
        document = library.root / "procedure/SKILL.md"
        document.write_text(body + "\nConcurrent edit.")
        self.assertFalse(library.approve(staged["proposalId"])["ok"])
        self.assertEqual(store.proposal(staged["proposalId"])["status"], "pending")
        self.assertTrue(library.reject(staged["proposalId"])["ok"])
        self.assertIn("Concurrent edit.", document.read_text())
        self.assertFalse(library.apply_feedback(review, {"name": "procedure", "action": "remove_file", "file_path": "references/guide.md"})["success"])

    def test_invalid_feedback_has_no_partial_publication(self):
        core = Core(self.home)
        self.addCleanup(core.close)
        self.enqueue(core, "enqueue")
        plan = core.request("claim", "claim", {"owner": "o", "hostID": "host"})["result"]["plan"]
        feedback = {"changes": [{"name": "first", "action": "create", "content": "Useful procedure."}, {"name": "second", "action": "delete"}]}
        answer = core.request("finish", "finish", {"reviewID": plan["reviewID"], "text": json.dumps(feedback)})
        self.assertEqual(answer["result"]["outcome"], "failed")
        self.assertFalse((core.library.root / "first").exists())
        for index, paths in enumerate((("references/a", "references/a"), ("references/a", "references/a/b"))):
            self.enqueue(core, f"overlap-enqueue-{index}", session=f"overlap-{index}")
            plan = core.request(f"overlap-claim-{index}", "claim", {"owner": "o", "hostID": "host"})["result"]["plan"]
            feedback["changes"][1] = {"name": "second", "action": "create", "content": "Useful procedure.", "files": [{"path": path, "content": "support"} for path in paths]}
            answer = core.request(f"overlap-finish-{index}", "finish", {"reviewID": plan["reviewID"], "text": json.dumps(feedback)})
            self.assertEqual(answer["result"]["outcome"], "failed")
            self.assertFalse((core.library.root / "first").exists())


if __name__ == "__main__":
    unittest.main()
