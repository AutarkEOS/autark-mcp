"""Local agent configuration: purpose, spending limits and ranking preferences.

The file is set once and then steers every search and purchase. It lives on the
agent's machine, never in the marketplace, and holds no credentials.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONFIG_VERSION = 1
KINDS = ("product", "api-usage", "compute")
PURCHASE_MODES = ("ask", "auto")
SPENDING_LIMITS = ("max_per_transaction_minor", "daily_budget_minor")
MAX_MINOR = 2_147_483_647
MAX_PURPOSE_LENGTH = 500
# Top-level preference fields accepted by the marketplace ranking (POST /api/services/rank).
PREFERENCE_FIELDS = ("profile", "priorities", "weights", "limits", "expectations", "risk_tolerance")


class AgentConfigError(ValueError):
    """The configuration is malformed, or an update would loosen a spending limit."""


def config_path() -> Path:
    override = os.environ.get("AUTARK_AGENT_CONFIG", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".autark" / "agent-config.json"


def audit_path() -> Path:
    override = os.environ.get("AUTARK_DECISION_LOG", "").strip()
    return Path(override).expanduser() if override else config_path().parent / "decisions.jsonl"


def validate_preferences(preferences: Any) -> dict[str, Any]:
    """Check the shape only. The marketplace validates the values on every ranking request."""
    if preferences is None:
        return {}
    if not isinstance(preferences, dict):
        raise AgentConfigError("preferences must be a JSON object.")
    unknown = sorted(str(key) for key in preferences.keys() - set(PREFERENCE_FIELDS))
    if unknown:
        raise AgentConfigError("Unknown preferences fields: " + ", ".join(unknown) + ".")
    return preferences


def _limit(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= MAX_MINOR:
        raise AgentConfigError(f"spending.{name} must be null or an integer between 0 and {MAX_MINOR}.")
    return value


def validate_config(raw: Any) -> dict[str, Any]:
    """Return a complete, normalized config or raise AgentConfigError."""
    if not isinstance(raw, dict):
        raise AgentConfigError("Agent config must be a JSON object.")
    unknown = sorted(str(key) for key in raw.keys() - {"version", "purpose", "purchase_mode", "spending", "preferences"})
    if unknown:
        raise AgentConfigError("Agent config has unknown fields: " + ", ".join(unknown) + ".")
    if raw.get("version", CONFIG_VERSION) != CONFIG_VERSION:
        raise AgentConfigError(f"Unsupported agent config version; expected {CONFIG_VERSION}.")
    purpose = raw.get("purpose", "")
    if not isinstance(purpose, str) or len(purpose) > MAX_PURPOSE_LENGTH or "\x00" in purpose:
        raise AgentConfigError(f"purpose must be text of at most {MAX_PURPOSE_LENGTH} characters.")
    mode = raw.get("purchase_mode", "ask")
    if mode not in PURCHASE_MODES:
        raise AgentConfigError("purchase_mode must be ask or auto.")
    spending = {} if raw.get("spending") is None else raw["spending"]
    if not isinstance(spending, dict):
        raise AgentConfigError("spending must be an object.")
    unknown = sorted(str(key) for key in spending.keys() - {*SPENDING_LIMITS, "allowed_kinds"})
    if unknown:
        raise AgentConfigError("spending has unknown fields: " + ", ".join(unknown) + ".")
    kinds = spending.get("allowed_kinds")
    if kinds is not None and (
        not isinstance(kinds, list)
        or any(kind not in KINDS for kind in kinds)
        or len(set(kinds)) != len(kinds)
    ):
        raise AgentConfigError("spending.allowed_kinds must be null or distinct values from: " + ", ".join(KINDS) + ".")
    return {
        "version": CONFIG_VERSION,
        "purpose": purpose.strip(),
        "purchase_mode": mode,
        "spending": {
            **{name: _limit(spending.get(name), name) for name in SPENDING_LIMITS},
            "allowed_kinds": None if kinds is None else sorted(kinds),
        },
        "preferences": validate_preferences(raw.get("preferences")),
    }


def load_config() -> dict[str, Any] | None:
    """None when no file exists. A present but unreadable file raises, so callers fail closed."""
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AgentConfigError(f"Agent config at {path} could not be read: {error.strerror or error}.") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise AgentConfigError(f"Agent config at {path} is not valid JSON: {error.msg}.") from None
    return validate_config(raw)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def save_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_config(config)
    _write_atomic(config_path(), json.dumps(normalized, indent=2, ensure_ascii=False) + "\n")
    return normalized


def apply_update(current: dict[str, Any] | None, update: dict[str, Any]) -> dict[str, Any]:
    """Merge a tool-supplied update into the config.

    The model may create the config, change purpose and preferences, and tighten
    spending. It may never loosen spending or switch itself to autonomous buying:
    those changes are a human edit of the file.
    """
    unknown = sorted(update.keys() - {"purpose", "purchase_mode", "preferences", *SPENDING_LIMITS, "allowed_kinds"})
    if unknown:
        raise AgentConfigError("Unknown config fields: " + ", ".join(unknown) + ".")
    base = current or validate_config({})
    spending = dict(base["spending"])
    merged = {**base, "spending": spending}
    for name in ("purpose", "preferences", "purchase_mode"):
        if name in update:
            merged[name] = update[name]
    for name in (*SPENDING_LIMITS, "allowed_kinds"):
        if name in update:
            spending[name] = update[name]
    merged = validate_config(merged)
    if current is None:
        return merged

    blocked = []
    if current["purchase_mode"] == "ask" and merged["purchase_mode"] == "auto":
        blocked.append("purchase_mode ask -> auto")
    for name in SPENDING_LIMITS:
        old, new = current["spending"][name], merged["spending"][name]
        if old is not None and (new is None or new > old):
            blocked.append(f"{name} {old} -> {'no limit' if new is None else new}")
    old_kinds, new_kinds = current["spending"]["allowed_kinds"], merged["spending"]["allowed_kinds"]
    if old_kinds is not None and (new_kinds is None or set(new_kinds) - set(old_kinds)):
        blocked.append(f"allowed_kinds {old_kinds} -> {'all' if new_kinds is None else new_kinds}")
    if blocked:
        raise AgentConfigError(
            "This update would loosen the agent's own limits (" + "; ".join(blocked) + "). "
            f"Only a human can do that, by editing {config_path()}."
        )
    return merged


def append_audit(entry: dict[str, Any]) -> None:
    """Append one decision to the local log. Raises OSError when it cannot be recorded."""
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **entry}
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
