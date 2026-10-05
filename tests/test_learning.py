import json
import multiprocessing
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from skill_learn.core import Core
from skill_learn.errors import SkillServiceError
from skill_learn.notify import Notifier
from skill_learn.planning import build_plan, digest_history, project_messages
from skill_learn.settings import load_settings
from skill_learn.usage import sum_usage


def competing_claim(home, barrier, output, owner):
    core = Core(home)
    try:
        barrier.wait()
        output.put(core.request(owner, "claim", {"owner": owner, "hostID": "host"})["result"])
    finally:
        core.close()


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "settings.yaml").write_text("library:\n  root: skills\nreports:\n  enabled: false\nnotifications:\n  enabled: false\n")
        self.core = Core(self.home)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.core.close)
        self.index = 0

    def request(self, op, **payload):
        self.index += 1
        response = self.core.request("test_" + str(self.index), op, payload)
        self.assertTrue(response["ok"], response)
        return response["result"]

    def job(self, parent="p", watermark="w"):
        self.request("enqueue", harness="opencode", sessionID=parent, hostID="host", messages=[], watermark=watermark)
        plan = self.request("claim", owner="o", hostID="host")["plan"]
        self.request("bind", reviewID=plan["reviewID"], reviewerID="native_" + plan["reviewID"], inheritedIDs=["inherited"], owner="o")
        return plan["reviewID"]

    def admit(self, review_id, assistant="assistant"):
        return self.request("admit", reviewID=review_id, reviewerID="native_" + review_id, owner="o", assistantID=assistant)

    def record(self, review_id, call_id, tokens, identity="step1"):
        return self.request("record", reviewID=review_id, callID=call_id, eventID=identity, event={"messageID": "assistant", "part": {"id": identity, "type": "step-finish", "tokens": tokens}})

    def test_processes_share_a_single_paid_worker_claim(self):
        self.request("enqueue", harness="opencode", sessionID="p", hostID="host", messages=[], watermark="w")
        context = multiprocessing.get_context("fork")
        barrier, output = context.Barrier(2), context.Queue()
        workers = [context.Process(target=competing_claim, args=(str(self.home), barrier, output, f"owner{i}")) for i in range(2)]
        for worker in workers:
            worker.start()
        answers = [output.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sum("plan" in answer for answer in answers), 1)
        self.assertEqual(sum("active" in answer for answer in answers), 1)

    def test_step_and_previous_input_admission_edges(self):
        for previous, allowed in ((99, True), (100, False), (101, False), (None, True)):
            with self.subTest(previous=previous):
                review = self.job(parent=str(previous), watermark=str(previous))
                plan = json.loads(self.core.store.review_row(review)["plan_json"])
                plan["budget"], plan["steps"] = 100, 2
                self.core.store.save_plan(review, plan)
                first = self.admit(review)
                self.assertTrue(first["allowed"])
                tokens = {"input": previous, "output": 3, "reasoning": 1, "cache": {"read": 0, "write": 0}} if previous is not None else {}
                self.record(review, first["callID"], tokens)
                self.assertEqual(self.admit(review, "next")["allowed"], allowed)
                if allowed:
                    self.assertEqual(self.admit(review, "third")["reason"], "steps")
                self.request("finish", reviewID=review, text="Nothing to save.")

    def test_disabled_budget_inherited_exclusion_and_duplicate_steps(self):
        review = self.job()
        plan = json.loads(self.core.store.review_row(review)["plan_json"])
        plan["budget"] = None
        self.core.store.save_plan(review, plan)
        first = self.admit(review)
        excluded = self.request("record", reviewID=review, callID=first["callID"], eventID="old", event={"messageID": "inherited", "part": {"type": "step-finish", "tokens": {"input": 999999}}})
        self.assertEqual(excluded["reason"], "inherited")
        tokens = {"input": 2000, "output": 3, "reasoning": 1, "cache": {"read": 8000, "write": 0}}
        self.record(review, first["callID"], tokens)
        self.assertFalse(self.record(review, first["callID"], tokens)["recorded"])
        second = self.admit(review, "second")
        self.assertTrue(second["allowed"])
        self.record(review, second["callID"], {}, "step2")
        totals = sum_usage(self.core.store.model_calls_for_review(review))
        self.assertEqual(totals["input"], 10000)
        self.assertTrue(totals["partial"]["input"])
        self.assertTrue(totals["partial"]["hit"])

    def test_finished_active_missing_and_ambiguous_recovery(self):
        review = self.job()
        call = self.admit(review)
        self.assertTrue(self.request("reconcile", reviewID=review, state="active")["active"])
        self.assertTrue(self.request("reconcile", reviewID=review, state="ambiguous")["needsOperator"])
        self.assertEqual(self.core.store.review_row(review)["status"], "running")
        self.assertFalse(self.request("reconcile", reviewID=review, state="missing")["replayed"])
        self.assertEqual(self.core.store.model_calls_for_review(review)[0]["call_status"], "interrupted")
        next_review = self.job("next", "new")
        finished = self.request("reconcile", reviewID=next_review, state="finished", text="Nothing to save.")
        self.assertEqual(finished["outcome"], "unchanged")
        self.assertEqual(self.request("reconcile", reviewID=next_review, state="finished", text="Nothing to save.")["settled"], True)
        self.assertEqual(len(self.core.store.model_calls_for_review(review)), 1)

    def test_result_publication_once_and_internal_tombstone_after_delete(self):
        review = self.job()
        feedback = json.dumps({"changes": [{"name": "procedure", "action": "create", "content": "Use for repeated work."}]})
        self.assertEqual(self.request("finish", reviewID=review, text=feedback)["outcome"], "applied")
        before = (self.home / "skills/procedure/SKILL.md").read_bytes()
        self.assertEqual(self.request("finish", reviewID=review, text=feedback)["outcome"], "applied")
        self.assertEqual(len(self.core.store.proposals_for_review(review)), 1)
        self.request("delete-session", harness="opencode", sessionID="p")
        self.assertEqual((self.home / "skills/procedure/SKILL.md").read_bytes(), before)
        self.assertIsNone(self.core.store.review_row(review))
        self.assertEqual(self.request("enqueue", harness="opencode", hostID="new-host", sessionID="native_" + review, watermark="new", messages=[])["disposition"], "internal-reviewer")

    def test_absorbing_manual_edit_support_removal_and_stale_new_file_approval(self):
        review = self.job()
        library = self.core.library
        library.apply_feedback(review, {"name": "procedure", "action": "create", "content": "Use for work.\n\nOld guide.", "files": [{"path": "references/old.md", "content": "old guide"}]})
        library.approval_generated = "manual"
        staged = library.apply_feedback(review, {"name": "procedure", "action": "patch", "old_string": "Old guide.", "new_string": "Absorbed guide."})
        removal = library.apply_feedback(review, {"name": "procedure", "action": "remove_file", "file_path": "references/old.md"}, staged["proposalId"])
        path = library.root / "procedure/references/old.md"
        self.assertFalse(library.approve(removal["proposalId"])["ok"])
        self.assertTrue(path.exists())
        self.assertTrue(library.approve(staged["proposalId"])["ok"])
        self.assertTrue(library.approve(removal["proposalId"])["ok"])
        self.assertFalse(path.exists())
        new_file = library.apply_feedback(review, {"name": "procedure", "action": "write_file", "file_path": "templates/new.md", "content": "staged"})
        target = library.root / "procedure/templates/new.md"
        target.parent.mkdir()
        target.write_text("operator file")
        self.assertFalse(library.approve(new_file["proposalId"])["ok"])
        self.assertEqual(target.read_text(), "operator file")

    def test_interrupted_publication_is_not_replayed_and_abandonment_releases_claim(self):
        review = self.job()
        self.core.store.begin_operation("interrupted-finish", "reconcile", {"reviewID": review, "state": "finished"})
        feedback = '{"changes":[{"name":"replayed","action":"create","content":"Use for work."}]}'
        answer = self.core.request("new-finish", "finish", {"reviewID": review, "text": feedback})
        self.assertFalse(answer["ok"])
        self.assertEqual(self.core.store.review_row(review)["recovery_state"], "publication-recovery-needed")
        self.assertFalse((self.home / "skills/replayed").exists())
        self.request("reconcile", reviewID=review, state="abandoned")
        self.assertNotEqual(self.job("next", "next"), review)

    def test_cancelled_before_admission_has_no_started_notification(self):
        cues = []
        self.core.notifier.play = lambda cue, review_id=None: cues.append(cue)
        review = self.job()
        self.request("enqueue", harness="opencode", hostID="host", sessionID="p", messages=[], watermark="new")
        self.assertFalse(self.admit(review)["allowed"])
        self.request("finish", reviewID=review, text="Nothing to save.")
        self.assertNotIn("started", cues)
        self.assertIn("cancelled", cues)

    def test_ownership_unique_patch_paths_and_atomic_failure(self):
        review = self.job()
        library = self.core.library
        directory = library.root / "user-owned"
        directory.mkdir()
        (directory / "SKILL.md").write_text("---\nname: user-owned\ndescription: Use for work.\n---\nBody.")
        self.core.store.save_skill("user-owned", "generated")
        self.assertFalse(library.apply_feedback(review, {"name": "user-owned", "action": "edit", "content": "Use for edited work."})["success"])
        self.assertTrue(library.adopt("user-owned")["ok"])
        self.assertTrue(library.apply_feedback(review, {"name": "user-owned", "action": "patch", "old_string": "Body.", "new_string": "Body. Body."})["applied"])
        self.assertFalse(library.apply_feedback(review, {"name": "user-owned", "action": "patch", "old_string": "Body.", "new_string": "new"})["success"])
        for protection in ("bundled", "hub", "external"):
            self.core.store.save_skill("user-owned", "generated", protection=protection)
            self.assertFalse(library.apply_feedback(review, {"name": "user-owned", "action": "patch", "old_string": "Body. Body.", "new_string": "new"})["success"])
        self.core.store.save_skill("user-owned", "generated")
        self.assertFalse(library.apply_feedback(review, {"name": "user-owned", "action": "write_file", "file_path": "../outside", "content": "bad"})["success"])
        document = directory / "SKILL.md"
        before = document.read_bytes()
        real_replace = os.replace
        def fail_new_package(source, target):
            if Path(source).name == "package":
                raise OSError("publication failed")
            return real_replace(source, target)
        with patch("skill_learn.library.os.replace", fail_new_package), self.assertRaises(OSError):
            library.apply_feedback(review, {"name": "user-owned", "action": "patch", "old_string": "Body. Body.", "new_string": "new"})
        self.assertEqual(document.read_bytes(), before)

    def test_digest_compaction_tail_boundary_clips_and_fork_size(self):
        raw = [
            {"info": {"id": "old", "role": "user"}, "parts": [{"type": "text", "text": "old"}]},
            {"info": {"id": "tail", "role": "user"}, "parts": [{"type": "text", "text": "tail"}]},
            {"info": {"id": "compact", "role": "user"}, "parts": [{"type": "compaction", "tail_start_id": "tail"}]},
            {"info": {"id": "summary", "role": "assistant", "summary": True, "parentID": "compact", "finish": "stop"}, "parts": [{"type": "text", "text": "summary"}]},
        ]
        projected = project_messages(raw)
        self.assertEqual([entry["id"] for entry in projected], ["compact", "summary", "tail"])
        messages = [{"role": "user", "content": "u" * 400}, {"role": "assistant", "content": "a" * 300, "tool_calls": [{"name": "bash", "input": "secret-old-call"}]}]
        messages += [{"role": "user", "content": str(i)} for i in range(5)]
        messages += [{"role": "assistant", "content": "call", "tool_calls": [{"name": "read"}]}, {"role": "tool", "content": "result"}]
        messages += [{"role": "user", "content": str(i)} for i in range(23)]
        digest = digest_history(messages)
        self.assertIn("u" * 300, digest)
        self.assertNotIn("u" * 301, digest)
        self.assertIn("a" * 200, digest)
        self.assertNotIn("secret-old-call", digest)
        self.assertIn("[historical tool call]", digest)
        profile = {"model": {"providerID": "openai", "modelID": "gpt-5.5"}, "variant": "medium", "agent": "build"}
        base = {"id": "rv", "session_id": "p", "watermark": "w", "messages": raw, "capture": {"profile": profile, "compatibility": {"compatible": True}, "parentInputTokens": 500, "parentOutputTokens": 100}}
        self.assertEqual(build_plan(self.core.settings, base, self.core.library)["mode"], "fork")
        base["capture"]["parentInputTokens"] = 200000
        self.assertEqual(build_plan(self.core.settings, base, self.core.library)["decision"]["reason"], "fork-input-limit")
        base["capture"]["parentInputTokens"] = None
        self.assertEqual(build_plan(self.core.settings, base, self.core.library)["mode"], "digest")
        unlimited = replace(self.core.settings, max_fork_input_tokens=None)
        self.assertEqual(build_plan(unlimited, base, self.core.library)["mode"], "fork")
        base["capture"]["compatibility"] = {"compatible": False, "reason": "unpreservable-scope"}
        self.assertEqual(build_plan(unlimited, base, self.core.library)["mode"], "digest")
        configured = replace(self.core.settings, selection="configured", model="other/different", context_window=None)
        profile["contextWindow"] = 400000
        plan = build_plan(configured, base, self.core.library)
        self.assertEqual(plan["decision"]["reason"], "selected-model-variant-differs")
        self.assertEqual(plan["budget"], 120000)
        self.assertEqual(build_plan(replace(self.core.settings, context_mode="digest"), base, self.core.library)["decision"]["reason"], "forced-digest")

    def test_notifications_are_off_path_distinct_muted_and_fail_isolated(self):
        calls = []
        settings_path = self.home / "settings.yaml"
        settings_path.write_text("reports:\n  enabled: false\nnotifications:\n  enabled: true\n")
        with patch.dict(os.environ, {"SKILL_LEARNING_NOTIFICATIONS": "1"}):
            notifier = Notifier(load_settings(self.home), self.core.store, lambda cue, phrase, volume: calls.append((cue, phrase)))
        for cue in ("initiated", "started", "finished", "unchanged", "failed", "cancelled"):
            notifier.play(cue)
        notifier.flush()
        self.assertEqual(len(set(calls)), 6)
        settings_path.write_text("notifications:\n  volume: 0\n")
        silent = Notifier(load_settings(self.home), self.core.store, lambda *args: self.fail("muted player invoked"))
        silent.play("started")
        review = self.job()
        failing = Notifier(notifier.settings, self.core.store, lambda *args: (_ for _ in ()).throw(RuntimeError("no player")))
        failing.play("failed", review)
        failing.flush()
        self.assertEqual(self.core.store.review_row(review)["status"], "running")
        self.assertTrue(any(entry["kind"] == "notification_failure" for entry in self.core.store.evidence(review)))

    def test_probe_bounds_no_publication_and_no_paid_retry(self):
        probe = self.request("request-probe", parentID="p", mode="fork")
        profile = {"model": {"providerID": "openai", "modelID": "gpt-5.5"}, "variant": "medium", "agent": "build"}
        body = {"sessionID": "p", "hostID": "host", "hostPID": os.getpid(), "messages": [], "watermark": "w", "profile": profile, "compatibility": {"compatible": True}, "parentInputTokens": 500, "parentOutputTokens": 100}
        plan = self.request("prepare-probe", probeID=probe["probeID"], owner="o", submission=body)["plan"]
        review = plan["reviewID"]
        self.request("bind", reviewID=review, reviewerID="native_" + review, inheritedIDs=[], owner="o")
        first = self.admit(review)
        self.assertTrue(first["allowed"])
        self.assertEqual(self.admit(review)["reason"], "diagnostic-retry-or-size-unknown")
        self.record(review, first["callID"], {"input": 500, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}})
        self.assertTrue(self.admit(review)["allowed"])
        self.assertFalse(self.admit(review)["allowed"])
        malicious = '{"changes":[{"name":"bad","action":"create","content":"Use for bad."}]}'
        self.request("finish", reviewID=review, text=malicious)
        self.assertFalse((self.home / "skills/bad").exists())
        self.assertEqual(len(self.core.store.model_calls_for_review(review)), 2)
        oversized = self.request("request-probe", parentID="p", mode="fork")
        body["parentInputTokens"] = 8192
        response = self.core.request("over", "prepare-probe", {"probeID": oversized["probeID"], "owner": "o", "submission": body})
        self.assertFalse(response["ok"])
        self.assertEqual(self.core.store.review_count(), 1)
