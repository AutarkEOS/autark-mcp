"""Local agent configuration: purpose, spending limits, ranking preferences and idle sharing.

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
# What this machine may do for other agents while its own agent is idle (see worker.py).
SHARE_MODES = ("script", "agent")
SHARING_DEFAULTS = {"enabled": False, "modes": ["script"], "rate_minor_per_second": 5, "idle_after_seconds": 120}
# Tool argument -> sharing field. idle_after_seconds is a human edit of the file.
SHARING_UPDATES = {"share_when_idle": "enabled", "share_modes": "modes", "share_rate_minor_per_second": "rate_minor_per_second"}
MAX_SHARE_RATE = 1_000_000


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


def state_path(name: str) -> Path:
    """Worker state lives next to the agent config: last-activity, worker.json, worker-status.json, worker.log."""
    return config_path().parent / name


def validate_sharing(raw: Any) -> dict[str, Any]:
    sharing = {} if raw is None else raw
    if not isinstance(sharing, dict):
        raise AgentConfigError("sharing must be an object.")
    unknown = sorted(str(key) for key in sharing.keys() - SHARING_DEFAULTS.keys())
    if unknown:
        raise AgentConfigError("sharing has unknown fields: " + ", ".join(unknown) + ".")
    merged = {**SHARING_DEFAULTS, **sharing}
    if type(merged["enabled"]) is not bool:
        raise AgentConfigError("sharing.enabled must be true or false.")
    modes = merged["modes"]
    if not isinstance(modes, list) or not modes or any(mode not in SHARE_MODES for mode in modes) or len(set(modes)) != len(modes):
        raise AgentConfigError("sharing.modes must list distinct values from: " + ", ".join(SHARE_MODES) + ".")
    if type(merged["rate_minor_per_second"]) is not int or not 1 <= merged["rate_minor_per_second"] <= MAX_SHARE_RATE:
        raise AgentConfigError(f"sharing.rate_minor_per_second must be an integer between 1 and {MAX_SHARE_RATE}.")
    if type(merged["idle_after_seconds"]) is not int or not 0 <= merged["idle_after_seconds"] <= 86400:
        raise AgentConfigError("sharing.idle_after_seconds must be an integer between 0 and 86400.")
    return {**merged, "modes": sorted(modes)}


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
    unknown = sorted(str(key) for key in raw.keys() - {"version", "purpose", "purchase_mode", "spending", "preferences", "sharing"})
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
        "sharing": validate_sharing(raw.get("sharing")),
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
    spending. It may never loosen spending, switch itself to autonomous buying, or
    open this machine to other agents' work after the first configuration: those
    changes are a human edit of the file.
    """
    unknown = sorted(update.keys() - {"purpose", "purchase_mode", "preferences", *SPENDING_LIMITS, "allowed_kinds", *SHARING_UPDATES})
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
    merged["sharing"] = {**base["sharing"], **{field: update[name] for name, field in SHARING_UPDATES.items() if name in update}}
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
    # Sharing runs other agents' work here, so only a human may turn it on or widen it.
    old_sharing, new_sharing = current["sharing"], merged["sharing"]
    if new_sharing["enabled"] and not old_sharing["enabled"]:
        blocked.append("sharing off -> on")
    if new_sharing["enabled"] and set(new_sharing["modes"]) - set(old_sharing["modes"]):
        blocked.append(f"sharing modes {old_sharing['modes']} -> {new_sharing['modes']}")
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
