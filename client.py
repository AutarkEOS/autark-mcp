"""HTTP session for the Autark mock marketplace. The bearer token stays in memory."""

import json
import re
from typing import Any
from uuid import uuid4

import httpx

from settings import (
    COMPUTE_RUN_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    FUNDING_TIMEOUT_SECONDS,
    api_base_url,
    env_credentials,
)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


class MockShopApiError(Exception):
    """Raised when the mock-service API returns an error response."""

    def __init__(self, status: int | None, message: str, body: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


def new_idempotency_key() -> str:
    return f"mcp-{uuid4()}"


def validate_idempotency_key(key: str) -> str:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
        raise MockShopApiError(
            400,
            "idempotency_key must be 1-128 characters: letters, digits, and . _ : -",
        )
    return key


def redact_secrets(payload: Any) -> Any:
    """Drop session tokens before a payload can reach the model."""
    hidden = {"token", "token_type"}
    if isinstance(payload, dict):
        return {key: redact_secrets(value) for key, value in payload.items() if key not in hidden}
    if isinstance(payload, list):
        return [redact_secrets(item) for item in payload]
    return payload


class Session:
    """One process-wide marketplace session. Token is never serialized."""

    def __init__(self) -> None:
        self.token: str | None = None
        self._base = api_base_url()

    async def register(self, username: str, password: str) -> tuple[int, dict[str, Any]]:
        status, body = await self.request(
            "POST",
            "/api/auth/register",
            json_body={"username": username, "password": password, "role": "agent"},
            auth=False,
        )
        self._store_token(body)
        return status, redact_secrets(body)

    async def login(self, username: str, password: str) -> tuple[int, dict[str, Any]]:
        status, body = await self.request(
            "POST",
            "/api/auth/login",
            json_body={"username": username, "password": password},
            auth=False,
        )
        self._store_token(body)
        return status, redact_secrets(body)

    async def logout(self) -> tuple[int, dict[str, Any]]:
        try:
            status, body = await self.request("POST", "/api/auth/logout", auth=True)
        finally:
            # An expired or rejected token must still be dropped locally.
            self.token = None
        return status, redact_secrets(body)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auth: bool = False,
        timeout: float | None = None,
        accept_statuses: set[int] | None = None,
        _retried_auth: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        if auth:
            await self._ensure_login()

        headers: dict[str, str] = {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if idempotency_key:
            headers["Idempotency-Key"] = validate_idempotency_key(idempotency_key)

        query = {key: value for key, value in (params or {}).items() if value is not None}
        try:
            async with httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT_SECONDS) as client:
                response = await client.request(
                    method,
                    f"{self._base}{path}",
                    json=json_body,
                    params=query or None,
                    files=files,
                    headers=headers,
                )
        except httpx.TimeoutException:
            raise MockShopApiError(
                None,
                f"Request timed out calling {method} {path}. "
                f"Is mock-service running at {self._base}?",
            ) from None
        except httpx.ConnectError:
            raise MockShopApiError(
                None,
                f"Could not connect to mock-service at {self._base}. "
                "Start it with `bash mock-servivce/scripts/start.sh`.",
            ) from None

        payload = _decode(response)
        accepted = response.is_success or (
            accept_statuses is not None and response.status_code in accept_statuses
        )
        if accepted:
            if not isinstance(payload, dict):
                raise MockShopApiError(
                    response.status_code,
                    "Unexpected non-object response from API.",
                    payload,
                )
            return response.status_code, payload

        if response.status_code == 401 and auth and not _retried_auth and env_credentials():
            self.token = None
            await self._ensure_login()
            return await self.request(
                method,
                path,
                json_body=json_body,
                params=params,
                files=files,
                idempotency_key=idempotency_key,
                auth=auth,
                timeout=timeout,
                accept_statuses=accept_statuses,
                _retried_auth=True,
            )

        detail = payload.get("error") if isinstance(payload, dict) else str(payload)
        raise MockShopApiError(response.status_code, str(detail), payload if isinstance(payload, dict) else None)

    def _store_token(self, body: dict[str, Any]) -> None:
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise MockShopApiError(None, "Auth response did not include a session token.", redact_secrets(body))
        self.token = token

    async def _ensure_login(self) -> None:
        if self.token:
            return
        creds = env_credentials()
        if creds is None:
            raise MockShopApiError(
                401,
                "Not signed in. Call mock_shop_login or set MOCK_SHOP_USERNAME and MOCK_SHOP_PASSWORD.",
            )
        await self.login(*creds)


def _decode(response: httpx.Response) -> Any:
    try:
        return response.json()
    except json.JSONDecodeError:
        text = response.text.strip() or response.reason_phrase
        return {"error": text[:500]}


_session: Session | None = None


def session() -> Session:
    global _session
    if _session is None:
        _session = Session()
    return _session


__all__ = [
    "COMPUTE_RUN_TIMEOUT_SECONDS",
    "FUNDING_TIMEOUT_SECONDS",
    "MockShopApiError",
    "new_idempotency_key",
    "redact_secrets",
    "session",
    "validate_idempotency_key",
]
