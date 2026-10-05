import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import yaml

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SUPPORT_PREFIXES = ("references/", "templates/", "scripts/")


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Library:
    def __init__(self, root: Path, store, approval_generated="manual"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store
        self.approval_generated = approval_generated

    def list_skills(self):
        entries = []
        if not self.root.exists():
            return entries
        for skill_dir in sorted(path for path in self.root.iterdir() if path.is_dir() and _NAME.fullmatch(path.name)):
            document = skill_dir / "SKILL.md"
            if not document.is_file():
                continue
            text = document.read_text(encoding="utf-8")
            record = self.store.skill(skill_dir.name)
            origin = record["origin"] if record else ("generated" if _generated_marker(text) else "user")
            pinned = bool(record and record["pinned"])
            protection = record["protection"] if record else None
            marked = _generated_marker(text)
            entries.append({
                "name": skill_dir.name,
                "description": _description(text),
                "agentManaged": origin == "generated" and marked and not pinned and not protection,
                "userOwned": origin != "generated" or not marked,
                "pinned": pinned,
                "protection": protection,
            })
        return entries

    def skill_index(self):
        lines = []
        for skill in self.list_skills():
            status = "generated" if skill["agentManaged"] else "user-owned"
            if skill["pinned"]:
                status = "pinned"
            if skill["protection"]:
                status = skill["protection"]
            lines.append(f"- {skill['name']}: {skill['description']} ({status})")
        return "\n".join(lines)

    def read_skill(self, name):
        target = self._target(name, "SKILL.md")
        if target is None or not target.is_file():
            return None
        return target.read_text(encoding="utf-8")

    def skill_report(self):
        blocks = []
        for skill in self.list_skills():
            skill_dir = self._skill_dir(skill["name"])
            if skill_dir is None:
                continue
            document = skill_dir / "SKILL.md"
            text = document.read_text(encoding="utf-8") if document.is_file() else ""
            files = []
            for path in sorted(skill_dir.rglob("*")):
                if not path.is_file() or path.name == "SKILL.md":
                    continue
                relative = path.relative_to(skill_dir).as_posix()
                if relative.startswith(_SUPPORT_PREFIXES):
                    files.append(relative)
            status = "generated" if skill["agentManaged"] else "user-owned"
            if skill["pinned"]:
                status = "pinned"
            if skill["protection"]:
                status = skill["protection"]
            file_line = f"\nfiles: {', '.join(files)}" if files else ""
            blocks.append(f"## {skill['name']} ({status}){file_line}\n{text}".rstrip())
        return "\n\n".join(blocks)

    def apply_feedback(self, review_id, change, absorbing_proposal=None):
        if not isinstance(change, dict):
            return self._refuse(review_id, "", "change", "Each change must be an object.")
        name = change.get("name") if isinstance(change.get("name"), str) else ""
        action = change.get("action") if isinstance(change.get("action"), str) else ""
        if not _NAME.fullmatch(name):
            return self._refuse(review_id, name, action, "Invalid skill name.")
        if action == "delete":
            return self._refuse(review_id, name, action, "Bare delete is refused.")
        self.store.add_evidence(review_id, "change", {"name": name, "action": action, "change": change})
        if action == "create":
            return self._create(review_id, name, _compose_skill(name, change), change.get("files") or [])
        if action in {"edit", "patch", "write_file", "remove_file"}:
            arguments = {
                "content": _compose_skill(name, change) if action == "edit" else change.get("content"),
                "file_path": change.get("file_path") or change.get("path"),
                "file_content": change.get("content") if action == "write_file" else change.get("file_content"),
                "old_string": change.get("old_string"),
                "new_string": change.get("new_string"),
                "absorbing_proposal": absorbing_proposal,
            }
            if action == "remove_file" and not absorbing_proposal:
                return self._refuse(review_id, name, action, "Standalone support-file cleanup is refused.")
            marks = set()
            target = self._target(name, arguments.get("file_path") or "SKILL.md")
            if target and target.is_file():
                self.view(name, arguments.get("file_path"), marks)
            return self._change_existing(review_id, action, name, arguments, marks)
        return self._refuse(review_id, name, action or "change", "action must be create, edit, patch, write_file, or remove_file.")

    def view(self, name, file_path, read_marks):
        target = self._target(name, file_path or "SKILL.md")
        if target is None or not target.is_file():
            return {"success": False, "error": f"Skill file not found: {name} {file_path or 'SKILL.md'}"}
        text = target.read_text(encoding="utf-8")
        digest = file_hash(target)
        read_marks.add(str(target.resolve()))
        return {"success": True, "name": name, "filePath": _display_path(target, self._skill_dir(name)), "content": text, "hash": digest}

    def manage(self, review_id, arguments, read_marks):
        action = arguments.get("action")
        name = arguments.get("name")
        if action == "delete":
            return self._refuse(review_id, name, action, f"Refusing to delete skill '{name}'. A background review cannot delete a skill.")
        if not isinstance(name, str) or not _NAME.match(name):
            return self._refuse(review_id, str(name), str(action), "Skill name must be lowercase words separated by hyphens.")
        if action == "create":
            return self._create(review_id, name, arguments.get("content"))
        if action in {"patch", "edit", "write_file", "remove_file"}:
            return self._change_existing(review_id, action, name, arguments, read_marks)
        return self._refuse(review_id, name, str(action), f"Unknown skill action '{action}'.")

    def approve(self, proposal_id, status="approved"):
        proposal = self.store.proposal(proposal_id)
        if proposal is None:
            return {"ok": False, "error": f"Unknown proposal {proposal_id}"}
        if proposal["status"] != "pending":
            return {"ok": False, "error": f"Proposal {proposal_id} is {proposal['status']}"}
        payload = proposal["payload"]
        action = payload["action"]
        name = payload["name"]
        if action == "create":
            if self._exists(name):
                return {"ok": False, "error": f"Skill '{name}' already exists. Proposal {proposal_id} stays pending."}
            content = _stamp_generated(name, payload["content"])
            if content is None:
                return {"ok": False, "error": f"Skill '{name}' needs a description. Proposal {proposal_id} stays pending."}
            files = payload.get("files") or []
            self._create_package(name, content, files)
            self.store.save_skill(name, "generated", pinned=False, protection=None)
        elif action == "patch":
            target = self._target(name, payload.get("file_path") or "SKILL.md")
            if target is None or not target.is_file() or file_hash(target) != proposal["base_hash"]:
                return {"ok": False, "error": f"Skill file changed since proposal {proposal_id}. It stays pending."}
            text = target.read_text(encoding="utf-8")
            count = text.count(payload["old_string"])
            if count != 1:
                return {"ok": False, "error": f"Patch no longer matches uniquely. Proposal {proposal_id} stays pending."}
            updated = text.replace(payload["old_string"], payload["new_string"], 1)
            if (payload.get("file_path") or "SKILL.md") == "SKILL.md":
                updated = _stamp_generated(name, updated)
                if updated is None:
                    return {"ok": False, "error": f"Skill '{name}' needs a description. Proposal {proposal_id} stays pending."}
            self._replace_file(target, updated)
        elif action == "edit":
            target = self._target(name, "SKILL.md")
            if target is None or not target.is_file() or file_hash(target) != proposal["base_hash"]:
                return {"ok": False, "error": f"SKILL.md changed since proposal {proposal_id}. It stays pending."}
            content = _stamp_generated(name, payload["content"])
            if content is None:
                return {"ok": False, "error": f"Skill '{name}' needs a description. Proposal {proposal_id} stays pending."}
            self._replace_file(target, content)
        elif action == "write_file":
            relative = payload["file_path"]
            target = self._target(name, relative)
            if target is None:
                return {"ok": False, "error": "Support path is invalid. Proposal stays pending."}
            if target.exists() and file_hash(target) != proposal["base_hash"]:
                return {"ok": False, "error": f"Support file changed since proposal {proposal_id}. It stays pending."}
            if not target.exists() and proposal["base_hash"] is not None:
                return {"ok": False, "error": f"Support file disappeared. Proposal {proposal_id} stays pending."}
            self._replace_file(target, payload["file_content"])
        elif action == "remove_file":
            absorber = self.store.proposal(payload.get("absorbing_proposal"))
            if not absorber or absorber["status"] not in {"approved", "applied"}:
                return {"ok": False, "error": "Absorbing edit must be approved before support removal."}
            target = self._target(name, payload["file_path"])
            if target is None or not target.is_file() or file_hash(target) != proposal["base_hash"]:
                return {"ok": False, "error": f"Support file changed since proposal {proposal_id}. It stays pending."}
            self._replace_file(target, None)
        else:
            return {"ok": False, "error": f"Cannot apply action {action}"}
        self.store.mark_proposal(proposal_id, status)
        return {"ok": True, "proposalId": proposal_id, "skill": name}

    def reject(self, proposal_id):
        proposal = self.store.proposal(proposal_id)
        if proposal is None:
            return {"ok": False, "error": f"Unknown proposal {proposal_id}"}
        if proposal["status"] != "pending":
            return {"ok": False, "error": f"Proposal {proposal_id} is {proposal['status']}"}
        self.store.mark_proposal(proposal_id, "rejected")
        return {"ok": True, "proposalId": proposal_id}

    def adopt(self, name):
        skill_dir = self._skill_dir(name)
        if skill_dir is None or not (skill_dir / "SKILL.md").is_file():
            return {"ok": False, "error": f"No skill named '{name}' to adopt"}
        current = self.store.skill(name)
        pinned = bool(current and current["pinned"])
        protection = current["protection"] if current else None
        document = skill_dir / "SKILL.md"
        stamped = _stamp_generated(name, document.read_text(encoding="utf-8"))
        if stamped is None:
            return {"ok": False, "error": f"Skill '{name}' needs a description before it can be adopted"}
        self._replace_file(document, stamped)
        self.store.save_skill(name, "generated", pinned=pinned, protection=protection)
        return {"ok": True, "skill": name, "origin": "generated"}

    def pin(self, name, pinned):
        skill_dir = self._skill_dir(name)
        if skill_dir is None or not (skill_dir / "SKILL.md").is_file():
            return {"ok": False, "error": f"No skill named '{name}' to pin"}
        current = self.store.skill(name)
        if current is None:
            self.store.save_skill(name, "user", pinned=pinned, protection=None)
        elif not self.store.set_pinned(name, pinned):
            return {"ok": False, "error": f"No skill named '{name}' to pin"}
        return {"ok": True, "skill": name, "pinned": pinned}

    def _create(self, review_id, name, content, files=None):
        if self._exists(name):
            return self._refuse(review_id, name, "create", f"Skill '{name}' already exists. Patch it instead of creating it.")
        if not isinstance(content, str) or not content.strip():
            return self._refuse(review_id, name, "create", "A new skill requires SKILL.md content.")
        if _frontmatter_name(content) not in {None, name}:
            return self._refuse(review_id, name, "create", "SKILL.md name does not match the skill directory.")
        content = _stamp_generated(name, content)
        if content is None:
            return self._refuse(review_id, name, "create", "A new skill requires a description of when to use it.")
        files = files or []
        paths = set()
        for extra in files:
            if not isinstance(extra, dict) or not isinstance(extra.get("path"), str) or not _support_path(extra["path"]) or self._target(name, extra["path"]) is None or not isinstance(extra.get("content"), str) or extra["path"] in paths:
                return self._refuse(review_id, name, "create", "Invalid or duplicate support file.")
            paths.add(extra["path"])
        proposal_id = self.store.add_proposal(
            review_id, name, "create", f"create '{name}'",
            {"action": "create", "name": name, "content": content, "files": files}, None,
        )
        return self._publish_if_auto({"success": True, "staged": True, "proposalId": proposal_id, "message": f"Staged create for '{name}'. Not saved until approved."})

    def _change_existing(self, review_id, action, name, arguments, read_marks, *, require_read=True):
        refusal = self._ownership_refusal(name, action)
        if refusal:
            return self._refuse(review_id, name, action, refusal)
        relative = "SKILL.md" if action in {"patch", "edit"} and not arguments.get("file_path") else arguments.get("file_path") or "SKILL.md"
        if action == "edit":
            relative = "SKILL.md"
        target = self._target(name, relative)
        if target is None:
            return self._refuse(review_id, name, action, "Skill path is outside the plugin library.")
        if action == "write_file" and not _support_path(relative):
            return self._refuse(review_id, name, action, "write_file only creates files under references/, templates/, or scripts/.")
        if action == "remove_file" and (relative == "SKILL.md" or not _support_path(relative)):
            return self._refuse(review_id, name, action, "remove_file only removes an existing support file.")
        exists = target.is_file()
        if action != "write_file" and not exists:
            return self._refuse(review_id, name, action, f"File not found: {relative}")
        if require_read and exists and str(target.resolve()) not in read_marks:
            return self._refuse(
                review_id, name, action,
                f"Refusing {action} for '{name}': load {relative} with skill_view during this review, then retry once.",
            )
        if not exists and action == "write_file":
            content = arguments.get("file_content")
            if not isinstance(content, str):
                return self._refuse(review_id, name, action, "write_file requires file_content.")
            proposal_id = self.store.add_proposal(
                review_id, name, action, f"write {relative} in '{name}'",
                {"action": action, "name": name, "file_path": relative, "file_content": content}, None,
            )
            return self._publish_if_auto({"success": True, "staged": True, "proposalId": proposal_id, "message": f"Staged {relative} for '{name}'."})
        digest = file_hash(target)
        if action == "patch":
            old_string = arguments.get("old_string")
            new_string = arguments.get("new_string")
            if not isinstance(old_string, str) or not isinstance(new_string, str) or not old_string:
                return self._refuse(review_id, name, action, "patch requires old_string and new_string.")
            text = target.read_text(encoding="utf-8")
            if text.count(old_string) != 1:
                return self._refuse(review_id, name, action, "old_string must match exactly one place in the file.")
            payload = {"action": "patch", "name": name, "file_path": relative, "old_string": old_string, "new_string": new_string}
            gist = f"patch '{name}' {relative}"
        elif action == "edit":
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return self._refuse(review_id, name, action, "edit requires the full SKILL.md content.")
            content = _stamp_generated(name, content)
            if content is None:
                return self._refuse(review_id, name, action, "SKILL.md requires a description of when to use the skill.")
            payload = {"action": "edit", "name": name, "content": content}
            gist = f"rewrite '{name}'"
        elif action == "write_file":
            content = arguments.get("file_content")
            if not isinstance(content, str):
                return self._refuse(review_id, name, action, "write_file requires file_content.")
            payload = {"action": "write_file", "name": name, "file_path": relative, "file_content": content}
            gist = f"write {relative} in '{name}'"
        else:
            if not arguments.get("absorbing_proposal"):
                return self._refuse(review_id, name, action, "Standalone support-file cleanup is refused.")
            payload = {"action": "remove_file", "name": name, "file_path": relative, "absorbing_proposal": arguments["absorbing_proposal"]}
            gist = f"remove {relative} from '{name}'"
        proposal_id = self.store.add_proposal(review_id, name, action, gist, payload, digest)
        return self._publish_if_auto({"success": True, "staged": True, "proposalId": proposal_id, "message": f"Staged {action} for '{name}'. The file is unchanged until approved."})

    def _publish_if_auto(self, result):
        if result.get("staged") and not result.get("proposalId"):
            return {"success": False, "error": "Review session was deleted."}
        if not result.get("staged") or self.approval_generated != "auto":
            return result
        applied = self.approve(result["proposalId"], status="applied")
        if not applied["ok"]:
            return {"success": False, "error": applied["error"]}
        return {"success": True, "applied": True, "staged": False, "proposalId": result["proposalId"], "message": "Applied the skill change."}

    def read_under_root(self, relative, read_marks):
        target = self._library_file(relative)
        if target is None or not target.is_file():
            return {"success": False, "error": "File is outside the service skill library or does not exist."}
        text = target.read_text(encoding="utf-8")
        digest = file_hash(target)
        read_marks.add(str(target.resolve()))
        return {"success": True, "path": str(target.relative_to(self.root)), "content": text, "hash": digest, "filePath": str(target.relative_to(self.root))}

    def search(self, query):
        if not isinstance(query, str) or not query:
            return {"success": False, "error": "search_files requires a query."}
        matches = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except UnicodeError:
                continue
            for number, line in enumerate(lines, start=1):
                if query in line:
                    matches.append({"path": str(path.relative_to(self.root)), "line": number, "text": line[:200]})
                    if len(matches) >= 20:
                        return {"success": True, "matches": matches}
        return {"success": True, "matches": matches}

    def _library_file(self, relative):
        if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in Path(relative).parts:
            return None
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root.resolve()):
            return None
        return target

    def _ownership_refusal(self, name, action):
        if not self._exists(name):
            return f"Skill '{name}' does not exist."
        record = self.store.skill(name)
        origin = record["origin"] if record else "generated"
        pinned = bool(record and record["pinned"])
        protection = record["protection"] if record else None
        if protection:
            return f"Refusing background {action} for {protection} skill '{name}'."
        if pinned:
            return f"Refusing background {action} for pinned skill '{name}'."
        document = self._skill_dir(name) / "SKILL.md"
        marked = _generated_marker(document.read_text(encoding="utf-8")) if document.is_file() else False
        if origin != "generated" or not marked:
            return f"Refusing background {action} for skill '{name}': it is user-owned and not agent-managed."
        return None

    def _refuse(self, review_id, name, action, message):
        self.store.add_evidence(review_id, "refusal", {"name": name, "action": action, "error": message})
        return {"success": False, "error": message}

    def _exists(self, name):
        skill_dir = self._skill_dir(name)
        return skill_dir is not None and (skill_dir / "SKILL.md").is_file()

    def _skill_dir(self, name):
        if not isinstance(name, str) or not _NAME.match(name):
            return None
        skill_dir = (self.root / name).resolve()
        if not skill_dir.is_relative_to(self.root.resolve()):
            return None
        return skill_dir

    def _target(self, name, relative):
        skill_dir = self._skill_dir(name)
        if skill_dir is None or not isinstance(relative, str) or not relative or relative.startswith("/"):
            return None
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            return None
        target = (skill_dir / relative).resolve()
        if not target.is_relative_to(skill_dir):
            return None
        return target

    def _write_skill_file(self, name, relative, content):
        target = self._target(name, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._replace_file(target, content)

    def _replace_file(self, target: Path, content: str):
        relative = target.relative_to(self.root.resolve())
        package = self.root / relative.parts[0]
        temporary = Path(tempfile.mkdtemp(prefix=".skill-stage-", dir=self.root))
        staged = temporary / "package"
        backup = self.root / (".skill-backup-" + uuid.uuid4().hex)
        try:
            shutil.copytree(package, staged, symlinks=True)
            replacement = staged.joinpath(*relative.parts[1:])
            if content is None:
                replacement.unlink()
            else:
                replacement.parent.mkdir(parents=True, exist_ok=True)
                replacement.write_text(content, encoding="utf-8")
            os.replace(package, backup)
            try:
                os.replace(staged, package)
            except BaseException:
                os.replace(backup, package)
                raise
            shutil.rmtree(backup, ignore_errors=True)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _create_package(self, name, content, files):
        target = self._skill_dir(name)
        if target is None or target.exists():
            raise ValueError("Skill package already exists or name is invalid")
        temporary = Path(tempfile.mkdtemp(prefix=".skill-stage-", dir=self.root))
        try:
            (temporary / "SKILL.md").write_text(content, encoding="utf-8")
            for extra in files:
                path = temporary / extra["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(extra["content"], encoding="utf-8")
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)


def _compose_skill(name, change):
    content = change.get("content") if isinstance(change.get("content"), str) else ""
    description = change.get("description")
    if isinstance(description, str) and description.strip() and not content.lstrip().startswith("---"):
        return f"---\nname: {name}\ndescription: {description.strip()}\n---\n{content}"
    return content


def _stamp_generated(name, content):
    body, header = _split_frontmatter(content)
    header = dict(header or {})
    description = header.get("description")
    if not isinstance(description, str) or not description.strip():
        description = _infer_description(body)
    if not isinstance(description, str) or not description.strip():
        return None
    metadata = header.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata = {"origin": "generated", **{key: value for key, value in metadata.items() if key != "origin"}}
    extra = {key: value for key, value in header.items() if key not in {"name", "description", "metadata"}}
    lines = ["---", f"name: {name}", f"description: {_yaml_plain(description.strip())}"]
    for key, value in extra.items():
        lines.append(yaml.safe_dump({key: value}, sort_keys=False).strip())
    lines.append(yaml.safe_dump({"metadata": metadata}, sort_keys=False).strip())
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def _infer_description(body):
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("- ", "* ")):
            stripped = stripped[2:].strip()
        if stripped:
            return stripped
    return ""


def _yaml_plain(value):
    if any(char in value for char in ":#{}[]&*!|>%@`'\\\"") or value[:1] in " \t" or value[-1:] in " \t" or "\n" in value:
        return yaml.safe_dump(value, default_style='"').strip()
    return value


def _generated_marker(text):
    header = _frontmatter(text)
    metadata = header.get("metadata") if isinstance(header, dict) else None
    return isinstance(metadata, dict) and metadata.get("origin") == "generated"


def _split_frontmatter(text):
    header = _frontmatter(text)
    if not isinstance(header, dict):
        return text, None
    end = text.find("\n---", 3)
    rest = text[end + 4 :]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    return rest, header


def _description(text):
    header = _frontmatter(text)
    if isinstance(header, dict) and isinstance(header.get("description"), str):
        return header["description"]
    return ""


def _frontmatter_name(text):
    header = _frontmatter(text)
    if isinstance(header, dict):
        return header.get("name")
    return None


def _frontmatter(text):
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    loaded = yaml.safe_load(text[3:end])
    return loaded


def _support_path(relative):
    return relative.startswith(_SUPPORT_PREFIXES) and not relative.endswith("/")


def _display_path(target: Path, skill_dir: Path):
    return str(target.relative_to(skill_dir))
