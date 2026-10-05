import json

from .prompt import review_instruction
from .usage import token_count


def retained_history(raw):
    """Apply completed-compaction boundaries only to digest evidence."""
    for index in range(len(raw) - 1, -1, -1):
        native = raw[index].get("native", {})
        if native.get("type") == "compaction" and native.get("status") == "completed":
            return raw[index:]
    kept, completed, tail = [], set(), None
    for message in reversed(raw):
        kept.append(message)
        info = message.get("info", {})
        if tail:
            if info.get("id") == tail:
                break
            continue
        if info.get("role") == "assistant" and info.get("summary") and info.get("finish") and not info.get("error"):
            completed.add(info.get("parentID"))
        if info.get("role") == "user" and info.get("id") in completed:
            compaction = next((part for part in message.get("parts", []) if part.get("type") == "compaction"), None)
            if compaction is not None:
                tail = compaction.get("tail_start_id")
                if not tail or info.get("id") == tail:
                    break
    kept.reverse()
    for index in range(len(kept) - 1, -1, -1):
        compaction = next((part for part in kept[index].get("parts", []) if part.get("type") == "compaction" and part.get("tail_start_id")), None)
        if compaction:
            summary_index = next((position for position in range(index + 1, len(kept)) if kept[position].get("info", {}).get("summary") and kept[position]["info"].get("parentID") == kept[index].get("info", {}).get("id")), None)
            tail_index = next((position for position, message in enumerate(kept) if message.get("info", {}).get("id") == compaction["tail_start_id"]), None)
            if summary_index is not None and tail_index is not None and tail_index < index:
                return kept[index:summary_index + 1] + kept[tail_index:index] + kept[summary_index + 1:]
            break
    return kept


def project_messages(raw):
    """Projection for digest evidence only; native fork history remains host-owned."""
    messages = []
    for message in retained_history(raw):
        info = message.get("info", {})
        if info.get("role") not in {"user", "assistant"}:
            continue
        text = "\n".join(part.get("text", "") for part in message.get("parts", []) if part.get("type") == "text" and not part.get("ignored"))
        for part in message.get("parts", []):
            if part.get("type") == "compaction":
                text += "\nWhat did we do so far?"
            if part.get("type") == "file":
                text += "\n[Attachment: " + str(part.get("filename") or part.get("mime") or "file") + "]"
        entry = {"role": info["role"], "content": text, "id": info.get("id")}
        if info["role"] == "assistant":
            if info.get("error") or (info.get("time") and not info["time"].get("completed") and not info.get("finish")):
                continue
            calls = []
            results = []
            for part in message.get("parts", []):
                if part.get("type") != "tool":
                    continue
                state = part.get("state", {})
                calls.append({"name": part.get("tool"), "input": state.get("input"), "id": part.get("callID")})
                output = "[Old tool result content cleared]" if state.get("time", {}).get("compacted") else state.get("output", state.get("error", "[interrupted]"))
                results.append({"role": "tool", "content": output, "name": part.get("tool"), "id": info.get("id")})
            entry["tool_calls"] = calls
            messages.append(entry)
            messages.extend(results)
        else:
            messages.append(entry)
    return messages


def digest_history(messages, tail=24):
    boundary = max(0, len(messages) - tail)
    while boundary and messages[boundary]["role"] == "tool":
        boundary -= 1
    earlier = []
    for message in messages[:boundary]:
        role = message["role"]
        if role == "user":
            earlier.append("USER: " + str(message.get("content", ""))[:300])
        elif role == "assistant":
            names = [str(call.get("name", "?")) for call in message.get("tool_calls", [])]
            if names:
                earlier.append("ASSISTANT[tools: " + ", ".join(names) + "]")
            if message.get("content"):
                earlier.append("ASSISTANT: " + str(message["content"])[:200])
    evidence = []
    if earlier:
        evidence.append("[Earlier conversation digest]\n" + "\n".join(earlier))
    for message in messages[boundary:]:
        evidence.append("[" + message["role"] + "] " + str(message.get("content", "")))
        for call in message.get("tool_calls", []):
            evidence.append("[historical tool call] " + json.dumps(call, ensure_ascii=False))
    return "\n\n".join(evidence)


def build_plan(settings, job, library):
    capture = job.get("capture") or {}
    profile = capture.get("profile") or {}
    parent_model = profile.get("model")
    if settings.selection == "follow" and parent_model and parent_model.get("providerID") and parent_model.get("modelID"):
        model = {**parent_model, "variant": profile.get("variant")}
    else:
        provider, name = settings.model.split("/", 1)
        model = {"providerID": provider, "modelID": name, "variant": settings.variant}
    compatible = capture.get("compatibility") or {}
    reason = "forced-digest" if settings.context_mode == "digest" else compatible.get("reason", "parent-profile-unavailable")
    same = parent_model == {key: model[key] for key in ("providerID", "modelID")} and profile.get("variant") == model.get("variant")
    fork = settings.context_mode == "auto" and same and compatible.get("compatible") is True
    if not same:
        reason = "selected-model-variant-differs"
    instruction = review_instruction(library, include_index=True) + "\nHost skill discovery/bodies are an instance snapshot and may be older than the current files until restart. Python validates every write against current files."
    parent_input = token_count(capture.get("parentInputTokens"))
    parent_output = token_count(capture.get("parentOutputTokens"))
    assessed = None
    if parent_input is not None and parent_output is not None:
        assessed = parent_input + parent_output + len(instruction.encode("utf-8")) + 4096
    if fork and settings.max_fork_input_tokens is not None:
        if assessed is None:
            fork, reason = False, "fork-input-size-unknown"
        elif assessed > settings.max_fork_input_tokens:
            fork, reason = False, "fork-input-limit"
    if not fork:
        instruction = digest_history(project_messages(job["messages"])) + "\n\n" + instruction
    return {"reviewID": job["id"], "parentID": job["session_id"], "watermark": job["watermark"],
            "mode": "fork" if fork else "digest", "model": model, "agent": profile.get("agent", "build"),
            "profile": profile, "permission": capture.get("permission", []), "instruction": instruction,
            "budget": settings.input_budget(profile.get("contextWindow") if same else None), "steps": settings.steps,
            "decision": {"reason": reason, "forkInputTokens": assessed, "method": "host-input-output-plus-utf8-suffix-reserve" if assessed is not None else "unavailable", "fidelity": "host_requested", "skillDiscovery": "host_instance_snapshot", "wireBody": "unavailable", "limitations": profile.get("limitations", [])}}
