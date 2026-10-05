import fcntl
import json
from contextlib import contextmanager

from .errors import SkillServiceError
from .library import Library, _NAME, _support_path, _compose_skill, _stamp_generated
from .native_store import NativeStore
from .notify import Notifier
from .planning import build_plan
from .settings import load_settings
from .usage import host_usage


class Core:
    def __init__(self, home, player=None):
        self.settings = load_settings(home)
        self.store = NativeStore(self.settings.home)
        self.library = Library(self.settings.library_root, self.store, self.settings.approval_generated)
        self.notifier = Notifier(self.settings, self.store, player)
        self.lock = (self.settings.home / "core.lock").open("a")
        self.operation_identity = None

    @contextmanager
    def serialized(self):
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(self.lock, fcntl.LOCK_UN)

    def close(self):
        self.notifier.flush()
        self.store.close()
        self.lock.close()

    def request(self, identity, operation, payload):
        with self.serialized():
            if operation in {"hello", "state", "pending", "show", "next-probe", "host-skills"}:
                return self.dispatch(operation, payload)
            previous = self.store.begin_operation(identity, operation, payload)
            if previous is not None:
                return previous
            try:
                self.operation_identity = identity
                response = self.dispatch(operation, payload)
                envelope = {"ok": True, "result": response}
            except Exception as error:
                envelope = {"ok": False, "error": {"code": "operation_failed", "message": str(error)}}
            self.store.end_operation(identity, envelope)
            self.operation_identity = None
            return envelope

    def refresh(self):
        if not self.settings.reports_enabled:
            return None
        try:
            from .reporting import generate
            return str(generate(self.store, self.settings))
        except Exception as error:
            # Output failure must never rewrite a learning outcome.
            import sys
            print(f"skill-learn report: {error}", file=sys.stderr)
            return None

    def dispatch(self, operation, payload):
        if operation == "hello":
            return {"ok": True, "result": {"version": 1, "python": self.settings.python, "triggers": self.settings.triggers,
                    "libraryRoot": str(self.settings.library_root), "home": str(self.settings.home),
                    "leaseSeconds": self.settings.lease_seconds, "selection": self.settings.selection,
                    "model": self.settings.model, "variant": self.settings.variant}}
        if operation == "state":
            return {"ok": True, "result": {"reviewers": self.store.reviewers(), "active": next((row for row in self.store.list_reviews() if row["status"] == "running"), None)}}
        if operation == "host-skills":
            import yaml
            skills = []
            for path in sorted(self.settings.library_root.glob("*/SKILL.md")):
                text = path.read_text(encoding="utf-8")
                header = {}
                if text.startswith("---\n"):
                    frontmatter, separator, body = text[4:].partition("\n---")
                    if separator:
                        header = yaml.safe_load(frontmatter) or {}
                        text = body.lstrip("\r\n")
                if not isinstance(header, dict):
                    raise SkillServiceError("Skill frontmatter must be a mapping: " + str(path))
                skills.append({"id": path.parent.name, "name": header.get("name", path.parent.name),
                               "description": header.get("description", ""), "path": str(path.resolve()), "content": text})
            return {"ok": True, "result": skills}
        if operation == "request-probe":
            if not isinstance(payload.get("parentID"), str) or payload.get("mode") not in {"fork", "digest"}:
                raise SkillServiceError("Probe requires parentID and fork/digest mode")
            identity = self.store.request_probe(payload["parentID"], payload["mode"])
            return {"probeID": identity, "status": "requested", "maxCalls": 2, "maxAssessedInputTokens": 8192, "publication": False}
        if operation == "next-probe":
            return {"ok": True, "result": self.store.next_probe()}
        if operation == "probe-failed":
            probe = self.store.probe(payload["probeID"])
            if probe and probe["status"] == "requested":
                self.store.update_probe(payload["probeID"], "failed", error=payload["error"])
            return {"failed": True}
        if operation == "prepare-probe":
            probe = self.store.probe(payload["probeID"])
            if not probe or probe["status"] != "requested":
                raise SkillServiceError("Probe was already claimed")
            body = payload["submission"]
            profile = body.get("profile") or {}
            model = profile.get("model")
            instruction = "Native cache diagnostic. Reply exactly: Nothing to save. Do not call any tools."
            assessed = None
            if all(type(body.get(key)) is int and body[key] >= 0 for key in ("parentInputTokens", "parentOutputTokens")):
                assessed = body["parentInputTokens"] + body["parentOutputTokens"] + len(instruction.encode()) + 4096
            error = None
            if not model or not model.get("providerID") or not model.get("modelID"):
                error = "Probe parent model is unavailable"
            elif assessed is None or assessed > 8192:
                error = "Probe input is unassessable or exceeds 8192 assessed tokens"
            elif probe["mode"] == "fork" and not body.get("compatibility", {}).get("compatible"):
                error = "Probe fork profile is incompatible: " + body.get("compatibility", {}).get("reason", "unavailable")
            if error:
                self.store.update_probe(probe["id"], "failed", error=error)
                raise SkillServiceError(error)
            if probe["mode"] == "digest":
                from .planning import project_messages, digest_history
                instruction = digest_history(project_messages(body["messages"])) + "\n\n" + instruction
                assessed = max(assessed, len(instruction.encode()) + 4096)
                if assessed > 8192:
                    self.store.update_probe(probe["id"], "failed", error="Digest probe exceeds 8192 assessed tokens")
                    raise SkillServiceError("Digest probe exceeds 8192 assessed tokens")
            job = self.store.start_probe(model["providerID"] + "/" + model["modelID"], profile.get("variant"), body["messages"])
            self.store.set_session_name("cache-probe", job["session_id"], "Cache probe — " + (body.get("sessionName") or "Untitled session"))
            plan = {"reviewID": job["id"], "parentID": body["sessionID"], "watermark": body["watermark"], "mode": probe["mode"],
                    "model": {**model, "variant": profile.get("variant")}, "agent": profile.get("agent", "build"), "profile": profile,
                    "permission": body.get("permission", []), "instruction": instruction, "steps": 2, "budget": None,
                    "diagnostic": True, "probeID": probe["id"], "decision": {"reason": "explicit-native-cache-probe", "fidelity": "host_requested", "forkInputTokens": assessed, "method": "host-prefix-plus-suffix-reserve", "cacheAcceptance": "unresolved-until-measured", "publication": False}}
            self.store.assign_owner(job["id"], payload["owner"], body["hostID"], body.get("hostPID"), self.settings.lease_seconds)
            self.store.save_plan(job["id"], plan)
            self.store.update_probe(probe["id"], "running", job["id"])
            return {"plan": plan}
        if operation == "enqueue":
            required = ("harness", "sessionID", "watermark", "hostID")
            if any(not isinstance(payload.get(key), str) or not payload[key] for key in required) or not isinstance(payload.get("messages"), list):
                raise SkillServiceError("Submission needs harness/sessionID/watermark/hostID/messages")
            if any(row["reviewer_id"] == payload["sessionID"] for row in self.store.reviewers()):
                return {"disposition": "internal-reviewer"}
            depth = payload.get("delegateDepth", 0)
            if type(depth) is not int or depth < 0:
                raise SkillServiceError("delegateDepth must be a non-negative integer")
            capture = {key: payload.get(key) for key in ("profile", "compatibility", "permission", "hostID", "hostPID", "hostScope", "directory", "parentInputTokens", "parentOutputTokens")}
            fields = {"harness": payload["harness"], "session_id": payload["sessionID"], "watermark": payload["watermark"],
                      "messages": payload["messages"], "delegate_depth": depth, "session_name": payload.get("sessionName"),
                      "trigger_name": payload.get("trigger"), "capture": capture}
            answer = self.store.enqueue(fields)
            if answer.get("reviewId"):
                self.notifier.play("initiated", answer["reviewId"])
            for review_id in answer.get("cancelled", []):
                self.notifier.play("cancelled", review_id)
            self.refresh()
            return answer
        if operation == "claim":
            answer = self.store.claim(payload["owner"], payload["hostID"], self.settings.lease_seconds, payload.get("hostScope"), payload.get("hostPID"))
            if answer.get("job"):
                plan = build_plan(self.settings, answer["job"], self.library)
                self.store.save_plan(plan["reviewID"], plan)
                return {"plan": plan}
            return answer
        if operation == "bind":
            self.store.bind(payload["reviewID"], payload["reviewerID"], payload.get("inheritedIDs", []), payload["owner"])
            return {"bound": True}
        if operation == "heartbeat":
            return {"active": self.store.heartbeat(payload["reviewID"], payload["owner"], self.settings.lease_seconds), "cancelRequested": self.store.cancel_requested(payload["reviewID"])}
        if operation == "admit":
            row = self.store.review_row(payload["reviewID"])
            if not row or row["status"] != "running" or row["cancel_requested"] or row["reviewer_id"] != payload["reviewerID"] or row["owner"] != payload["owner"]:
                return {"allowed": False, "reason": "review-not-active"}
            plan = json.loads(row["plan_json"])
            calls = self.store.model_calls_for_review(row["id"])
            if not calls and not plan.get("diagnostic") and self.settings.max_input_tokens is None:
                window = payload.get("contextWindow")
                if type(window) is int and window > 0:
                    plan["budget"] = self.settings.input_budget(window)
                    self.store.save_plan(row["id"], plan)
            previous = (calls[-1].get("response") or {}).get("usage", {}).get("input_tokens") if calls else None
            reason = "steps" if len(calls) >= plan["steps"] else "input-budget" if plan["budget"] is not None and previous is not None and previous >= plan["budget"] else None
            if plan.get("diagnostic") and calls:
                usage = (calls[-1].get("response") or {}).get("usage", {})
                output = usage.get("output_tokens")
                reasoning = usage.get("reasoning_tokens")
                if calls[-1]["call_status"] != "completed" or previous is None or output is None or reasoning is None:
                    reason = "diagnostic-retry-or-size-unknown"
                elif previous + output + reasoning + 4096 + len("Reply exactly: Nothing to save.".encode()) > 8192:
                    reason = "diagnostic-input-limit"
            if reason:
                self.store.add_evidence(row["id"], "admission-stop", {"reason": reason, "previousInput": previous, "fidelity": "host_normalized"})
                return {"allowed": False, "reason": reason}
            call_id = self.store.add_model_call(row["id"], row["harness"], row["reviewer_id"], len(calls) + 1, row["called_model"],
                {"fidelity": "host_requested", "profile": payload.get("profile"), "assistantID": payload.get("assistantID"), "wire_body": None, "response_id": None}, None,
                evidence_kind="host_requested", call_status="attempted")
            if not calls:
                self.notifier.play("started", row["id"])
            return {"allowed": True, "callID": call_id}
        if operation == "record":
            row = self.store.review_row(payload["reviewID"])
            if not row:
                return {"recorded": False}
            event = payload["event"]
            message_id = event.get("messageID") or (event.get("part") or {}).get("messageID")
            if message_id in json.loads(row["inherited_json"] or "[]"):
                return {"recorded": False, "reason": "inherited"}
            if not self.store.event_once(row["id"], payload["eventID"], event):
                return {"recorded": False, "reason": "duplicate"}
            self.store.add_evidence(row["id"], "host_observed", event)
            if event.get("kind") == "host_call_started" and payload.get("callID"):
                self.store.bind_call_assistant(row["id"], payload["callID"], message_id)
            part = event.get("part", {})
            if part.get("type") == "step-finish" and payload.get("callID"):
                calls = self.store.model_calls_for_review(row["id"])
                call = next((call for call in calls if call["id"] == payload["callID"]), None)
                if call and call["call_status"] == "attempted":
                    message = event.get("message")
                    parts = (message or {}).get("parts", [])
                    self.store.finish_model_call(call["id"], {"usage": host_usage(part.get("tokens")), "host_step": part,
                        "host_message": message, "content": "\n".join(fragment.get("text", "") for fragment in parts if fragment.get("type") == "text"),
                        "tool_calls": [fragment for fragment in parts if fragment.get("type") == "tool"],
                        "reasoning": [fragment for fragment in parts if fragment.get("type") == "reasoning"], "fidelity": "host_observed"}, payload.get("durationSeconds"))
                    self.store.refresh_usage(row["id"])
            return {"recorded": True}
        if operation == "skill-loaded":
            self.store.add_evidence(payload["reviewID"], "skill-load", {"name": payload["name"], "location": payload.get("location"), "fidelity": "host_instance_snapshot", "body": payload.get("body"), "currentDiskMayDiffer": True, "discoveryCompatible": payload.get("discoveryCompatible")})
            return {"recorded": True}
        if operation == "finish":
            return self.finish(payload)
        if operation == "cancel":
            row = self.store.review_row(payload["reviewID"])
            if row and row["status"] == "running":
                if row["reviewer_id"] and not payload.get("hostSettled"):
                    self.store.cancel_open(row["harness"], row["session_id"])
                    return {"needsAbort": row["reviewer_id"]}
                self.store.interrupt_attempts(row["id"])
                self.store.finish_native(row["id"], "cancelled")
                plan = json.loads(row["plan_json"] or "{}")
                if plan.get("probeID"):
                    self.store.update_probe(plan["probeID"], "cancelled", row["id"])
                self.notifier.play("cancelled", row["id"])
                self.refresh()
            return {"cancelled": True}
        if operation == "reconcile":
            row = self.store.review_row(payload["reviewID"])
            if not row or row["status"] != "running":
                return {"settled": True}
            state = payload["state"]
            if state == "finished":
                return self.finish(payload)
            if state == "active":
                if payload.get("owner"):
                    self.store.adopt_owner(row["id"], payload["owner"])
                    row = self.store.review_row(row["id"])
                self.store.heartbeat(row["id"], row["owner"], self.settings.lease_seconds)
                return {"active": True}
            if state == "ambiguous":
                self.store.set_recovery(row["id"], "recovery-needed")
                return {"needsOperator": True}
            if state not in {"missing", "abandoned"}:
                raise SkillServiceError("Unknown reconciliation state")
            self.store.interrupt_attempts(row["id"])
            self.store.set_recovery(row["id"], "interrupted")
            self.store.finish_native(row["id"], "cancelled" if row["cancel_requested"] else "failed", "Native session interrupted; no automatic paid replay")
            plan = json.loads(row["plan_json"] or "{}")
            if plan.get("probeID"):
                self.store.update_probe(plan["probeID"], "cancelled" if row["cancel_requested"] else "failed", row["id"], "Native session interrupted")
            self.notifier.play("cancelled" if row["cancel_requested"] else "failed", row["id"])
            self.refresh()
            return {"settled": True, "replayed": False}
        if operation == "report":
            return {"index": self.refresh()}
        if operation == "delete-session":
            rows = self.store.reviews_for_session(payload["harness"], payload["sessionID"])
            if any(row["status"] == "running" for row in rows):
                self.store.cancel_open(payload["harness"], payload["sessionID"])
                self.refresh()
                raise SkillServiceError("Active native reviewer must be aborted/reconciled before deletion")
            count = self.store.delete_session(payload["harness"], payload["sessionID"])
            self.refresh()
            return {"deletedReviews": count}
        if operation == "pending":
            return {"ok": True, "result": self.store.pending_proposals()}
        if operation == "show":
            answer = self.store.proposal(payload["id"]) or self.store.review_row(payload["id"]) or self.store.probe(payload["id"])
            if answer is None:
                raise SkillServiceError(f"Unknown record {payload['id']}")
            if payload["id"].startswith("rv_"):
                answer = {**answer, "evidence": self.store.evidence(payload["id"]), "calls": self.store.model_calls_for_review(payload["id"]), "context": self.store.review_context(payload["id"])}
            return {"ok": True, "result": answer}
        if operation in {"approve", "reject", "adopt", "pin", "unpin"}:
            answer = self.library.pin(payload["id"], operation == "pin") if operation in {"pin", "unpin"} else getattr(self.library, operation)(payload["id"])
            self.refresh()
            return answer
        raise SkillServiceError(f"Unknown operation {operation}")

    def finish(self, payload):
        row = self.store.review_row(payload["reviewID"])
        if not row:
            return {"outcome": "deleted"}
        if row["status"] != "running":
            return {"outcome": row["outcome"]}
        if self.store.unfinished_publication(row["id"], self.operation_identity):
            self.store.set_recovery(row["id"], "publication-recovery-needed")
            raise SkillServiceError("Interrupted publication must be reconciled, not replayed")
        if row["cancel_requested"]:
            self.store.interrupt_attempts(row["id"])
            self.store.finish_native(row["id"], "cancelled")
            plan = json.loads(row["plan_json"] or "{}")
            if plan.get("probeID"):
                self.store.update_probe(plan["probeID"], "cancelled", row["id"])
            self.notifier.play("cancelled", row["id"])
            self.refresh()
            return {"outcome": "cancelled"}
        text = payload.get("text", "").strip()
        plan = json.loads(row["plan_json"] or "{}")
        outcome, error = "unchanged", None
        try:
            if payload.get("error"):
                raise SkillServiceError(payload["error"])
            if plan.get("diagnostic"):
                self.store.add_evidence(row["id"], "cache-diagnostic", {"probeID": plan["probeID"], "publication": False, "calls": len(self.store.model_calls_for_review(row["id"])), "acceptance": "unresolved; host counts do not establish full parent-history reuse", "text": text})
            elif text != "Nothing to save.":
                feedback = json.loads(text)
                changes = feedback.get("changes") if isinstance(feedback, dict) else None
                if not isinstance(changes, list):
                    raise SkillServiceError("Review must return JSON changes or Nothing to save.")
                self.validate_feedback(changes)
                absorbers, answers = {}, []
                for change in sorted(changes, key=lambda change: change["action"] == "remove_file"):
                    answer = self.library.apply_feedback(row["id"], change, absorbers.get(change["name"]))
                    answers.append(answer)
                    if change["action"] in {"edit", "patch"} and answer.get("success"):
                        absorbers[change["name"]] = answer.get("proposalId")
                if any(answer.get("applied") for answer in answers):
                    outcome = "applied"
                elif any(answer.get("staged") for answer in answers):
                    outcome = "staged"
                elif any(not answer.get("success") for answer in answers):
                    outcome = "failed"
            self.store.add_evidence(row["id"], "native-completion", {"text": text, "transcript": payload.get("messages"), "fidelity": "host_observed", "wire_body": None})
        except Exception as exception:
            outcome, error = "failed", str(exception)
        self.store.interrupt_attempts(row["id"])
        self.store.finish_native(row["id"], outcome, error)
        if plan.get("probeID"):
            self.store.update_probe(plan["probeID"], "failed" if outcome == "failed" else "finished", row["id"], error)
        self.notifier.play("unchanged" if outcome == "unchanged" else "finished" if outcome in {"applied", "staged"} else "failed", row["id"])
        self.refresh()
        return {"outcome": outcome, "error": error}

    def validate_feedback(self, changes):
        for change in changes:
            if not isinstance(change, dict) or not isinstance(change.get("name"), str) or not _NAME.fullmatch(change["name"]):
                raise SkillServiceError("Invalid feedback skill name")
            action = change.get("action")
            if action not in {"create", "edit", "patch", "write_file", "remove_file"}:
                raise SkillServiceError("Invalid feedback action")
            if action in {"create", "edit", "write_file"} and not isinstance(change.get("content"), str):
                raise SkillServiceError("Feedback content must be text")
            if action in {"create", "edit"} and _stamp_generated(change["name"], _compose_skill(change["name"], change)) is None:
                raise SkillServiceError("Skill content requires a meaningful description")
            if action == "patch" and (not isinstance(change.get("old_string"), str) or not change["old_string"] or not isinstance(change.get("new_string"), str)):
                raise SkillServiceError("Invalid patch text")
            path = change.get("file_path") or change.get("path") or "SKILL.md"
            if self.library._target(change["name"], path) is None:
                raise SkillServiceError("Invalid support path")
            if action in {"write_file", "remove_file"} and not _support_path(path):
                raise SkillServiceError("Support operation requires a support path")
            if action == "create":
                files = change.get("files", [])
                if not isinstance(files, list):
                    raise SkillServiceError("Support files must be a list")
                paths = set()
                for extra in files:
                    if not isinstance(extra, dict) or not isinstance(extra.get("path"), str) or not _support_path(extra["path"]) or self.library._target(change["name"], extra["path"]) is None or not isinstance(extra.get("content"), str):
                        raise SkillServiceError("Invalid support file")
                    target = self.library._target(change["name"], extra["path"])
                    if any(target == previous or target in previous.parents or previous in target.parents for previous in paths):
                        raise SkillServiceError("Duplicate or overlapping support paths")
                    paths.add(target)
