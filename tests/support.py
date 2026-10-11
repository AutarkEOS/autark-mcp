"""Shared fixtures: an isolated config directory and a scripted marketplace session."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from client import MockShopApiError  # noqa: E402


class FakeSession:
    """Answers marketplace requests from a table and records every call."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.token = "test-token"

    def on(self, method: str, path: str, response: Any) -> None:
        """`response` is a body dict, a (status, body) tuple, an exception, or a callable(call) -> any of those."""
        self.routes[(method, path)] = response

    def made(self, method: str, path: str | None = None) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["method"] == method and (path is None or call["path"] == path)]

    async def request(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        call = {"method": method, "path": path, **kwargs}
        self.calls.append(call)
        if (method, path) not in self.routes:
            raise MockShopApiError(404, f"No scripted response for {method} {path}")
        response = self.routes[(method, path)]
        if callable(response):
            response = response(call)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, response


def service(product_id: str, price_minor: int, kind: str = "product") -> dict[str, Any]:
    return {"service": {"id": product_id, "price_minor": price_minor, "kind": kind, "currency": "SOL"}}


def ledger(*rows: tuple[str, int]) -> dict[str, Any]:
    """Rows are (created_at ISO text, amount_minor), newest first."""
    return {"transactions": [{"created_at": created, "amount_minor": amount, "kind": "purchase" if amount < 0 else "topup"}
                             for created, amount in rows], "total": len(rows)}


class ConfigDirectoryTestCase(unittest.IsolatedAsyncioTestCase):
    """Points the agent config and decision log at a temporary directory."""

    def setUp(self) -> None:
        import policy

        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)
        self.config_file = self.directory / "agent-config.json"
        self.audit_file = self.directory / "decisions.jsonl"
        previous = {name: os.environ.get(name) for name in ("AUTARK_AGENT_CONFIG", "AUTARK_DECISION_LOG", "AUTARK_WORKER_AUTOSTART")}
        os.environ["AUTARK_AGENT_CONFIG"] = str(self.config_file)
        os.environ["AUTARK_DECISION_LOG"] = str(self.audit_file)
        # No test may leave a background worker behind.
        os.environ["AUTARK_WORKER_AUTOSTART"] = "0"

        def restore() -> None:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        policy.reset_approvals()
        self.addCleanup(policy.reset_approvals)

    def write_config(self, **fields: Any) -> None:
        self.config_file.write_text(json.dumps(fields), encoding="utf-8")

    def audit(self) -> list[dict[str, Any]]:
        if not self.audit_file.exists():
            return []
        return [json.loads(line) for line in self.audit_file.read_text(encoding="utf-8").splitlines()]
