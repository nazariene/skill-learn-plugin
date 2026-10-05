import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import SkillServiceError

def opencode_config_dir():
    return Path(os.environ.get("OPENCODE_CONFIG_DIR") or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "opencode").expanduser()


DEFAULT_HOME = os.environ.get("SKILL_LEARN_HOME") or str((opencode_config_dir() / "plugins" / "skill-learn").resolve())
CUES = {"initiated", "started", "finished", "unchanged", "failed", "cancelled"}
GROUPS = {
    "runtime": {"python", "leaseSeconds"},
    "triggers": {"idle", "turns"},
    "llm": {"selection", "model", "variant", "steps", "contextWindow"},
    "review": {"contextMode", "maxInputTokens", "maxForkInputTokens"},
    "library": {"root"}, "approval": {"user", "generated"},
    "notifications": {"enabled", "volume", "types"}, "reports": {"enabled", "root"},
}


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    values = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise SkillServiceError("Settings keys must be strings")
        if key in values:
            raise SkillServiceError(f"Duplicate YAML key: {key}")
        values[key] = loader.construct_object(value_node, deep=deep)
    return values


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def group(values, name, allowed):
    selected = values.get(name, {})
    if not isinstance(selected, dict):
        raise SkillServiceError(f"{name} must be a mapping")
    unknown = set(selected) - allowed
    if unknown:
        raise SkillServiceError(f"Unknown {name} setting: {', '.join(sorted(unknown))}")
    return selected


def integer(value, name, minimum=None, nullable=False):
    if nullable and value is None:
        return value
    if type(value) is not int or (minimum is not None and value < minimum):
        raise SkillServiceError(f"{name} must be an integer" + (f" >= {minimum}" if minimum is not None else ""))
    return value


def boolean(value, name):
    if type(value) is not bool:
        raise SkillServiceError(f"{name} must be true or false")
    return value


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise SkillServiceError(f"{name} must be a non-empty string")
    return value.strip()


def path_from(home, value, name):
    path = Path(text(value, name)).expanduser()
    return (path if path.is_absolute() else home / path).resolve()


@dataclass(frozen=True)
class Settings:
    home: Path
    python: str
    lease_seconds: int
    triggers: dict
    selection: str
    model: str
    variant: str | None
    steps: int
    context_window: int | None
    context_mode: str
    max_input_tokens: int | None
    max_fork_input_tokens: int | None
    library_root: Path
    approval_user: str
    approval_generated: str
    notifications_enabled: bool
    notification_volume: float
    notification_types: dict
    reports_enabled: bool
    reports_root: Path

    def input_budget(self, window=None):
        if self.max_input_tokens is not None:
            return self.max_input_tokens if self.max_input_tokens > 0 else None
        window = window or self.context_window
        return max(1, int(window * .75)) if window else 120000


def load_settings(home=DEFAULT_HOME):
    home = Path(home).expanduser().resolve()
    file = home / "settings.yaml"
    try:
        loaded = yaml.load(file.read_text(encoding="utf-8"), Loader=UniqueLoader) if file.is_file() else {}
    except yaml.YAMLError as error:
        raise SkillServiceError(f"Invalid YAML: {error}") from error
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict) or set(loaded) - set(GROUPS):
        raise SkillServiceError("Unknown settings groups or non-mapping settings")
    groups = {name: group(loaded, name, allowed) for name, allowed in GROUPS.items()}
    runtime, llm, review = groups["runtime"], groups["llm"], groups["review"]
    selection = llm.get("selection", "follow")
    mode = review.get("contextMode", "auto")
    if not isinstance(selection, str) or not isinstance(mode, str) or selection not in {"follow", "configured"} or mode not in {"auto", "digest"}:
        raise SkillServiceError("Invalid llm.selection or review.contextMode")
    model = text(llm.get("model", "openai/gpt-5.5"), "llm.model")
    if "/" not in model or not all(model.split("/", 1)):
        raise SkillServiceError("llm.model requires provider/model")
    variant = llm.get("variant", "medium")
    if variant is not None:
        variant = text(variant, "llm.variant")
    triggers = {}
    for name, default, minimum in (("idle", 15, 0), ("turns", 25, 1)):
        count_key = "seconds" if name == "idle" else "count"
        values = group(groups["triggers"], name, {"enabled", count_key})
        triggers[name] = {"enabled": boolean(values.get("enabled", True), f"triggers.{name}.enabled"),
                          count_key: integer(values.get(count_key, default), f"triggers.{name}.{count_key}", minimum)}
    approval = groups["approval"]
    user, generated = approval.get("user", "manual"), approval.get("generated", "auto")
    if not isinstance(user, str) or not isinstance(generated, str) or user not in {"manual", "auto"} or generated not in {"manual", "auto"}:
        raise SkillServiceError("Approval settings must be manual or auto")
    notifications, reports = groups["notifications"], groups["reports"]
    enabled = boolean(notifications.get("enabled", True), "notifications.enabled")
    volume = notifications.get("volume", 1)
    if type(volume) not in {int, float} or not 0 <= volume <= 1:
        raise SkillServiceError("notifications.volume must be from 0 through 1")
    types = group(notifications, "types", CUES)
    types = {cue: boolean(types.get(cue, True), f"notifications.types.{cue}") for cue in CUES}
    override = os.environ.get("SKILL_LEARNING_NOTIFICATIONS")
    if override not in {None, "0", "1"}:
        raise SkillServiceError("SKILL_LEARNING_NOTIFICATIONS must be 0 or 1")
    enabled = enabled if override is None else override == "1"
    library_root = groups["library"].get("root")
    library_root = (opencode_config_dir() / "skills").resolve() if library_root is None else path_from(home, library_root, "library.root")
    return Settings(
        home, text(runtime.get("python", "python3"), "runtime.python"),
        integer(runtime.get("leaseSeconds", 60), "runtime.leaseSeconds", 1), triggers,
        selection, model, variant, integer(llm.get("steps", 16), "llm.steps", 1),
        integer(llm.get("contextWindow"), "llm.contextWindow", 1, True), mode,
        integer(review.get("maxInputTokens"), "review.maxInputTokens", nullable=True),
        integer(review.get("maxForkInputTokens", 120000), "review.maxForkInputTokens", 1, True),
        library_root, user, generated,
        enabled, float(volume), types, boolean(reports.get("enabled", True), "reports.enabled"),
        path_from(home, reports.get("root", "reports"), "reports.root"),
    )
