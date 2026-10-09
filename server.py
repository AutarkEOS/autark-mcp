#!/usr/bin/env python3
"""Agent-side MCP server for the Autark mock marketplace."""

import json
from typing import Any, Literal
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from client import (
    COMPUTE_RUN_TIMEOUT_SECONDS,
    FUNDING_TIMEOUT_SECONDS,
    MockShopApiError,
    new_idempotency_key,
    redact_secrets,
    session,
)

mcp = MCPServer(
    "mock_shop_mcp",
    instructions=(
        "Tools for the Autark mock marketplace (mock-servivce). "
        "Browse services and vendors, then act as an agent: wallet, cart, purchases, "
        "one-time API usages, and prepaid compute. "
        "Prices are integer minor units (1 minor = 0.01 SOL in this demo). "
        "Writes that take idempotency_key are safe to retry with the same key and the same body. "
        "A new key is a new payment. Seeded demo agents have no Solana keys, so purchases and "
        "compute funding fail until you register a new agent and fund Devnet. "
        "This server keeps the session token itself; never ask the user to paste a bearer token."
    ),
)

READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
SAFE_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)

_ID = r"^[a-z0-9_-]{1,64}$"
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class LineItem(BaseModel):
    """One catalog line. vendor_id is an optional assertion, not a price."""

    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(..., description="Offer id, e.g. `orange` or `vendor-b-apple`.", pattern=_ID)
    quantity: int = Field(..., description="Units, 1-999. Duplicate ids are summed by the API.", ge=1, le=999)
    vendor_id: str | None = Field(
        default=None,
        description="Optional vendor assertion. Omit unless you need to pin the seller.",
        pattern=_ID,
    )


def _json(data: Any) -> str:
    return json.dumps(redact_secrets(data), indent=2, ensure_ascii=False, default=str)


def _error(error: Exception) -> str:
    if isinstance(error, MockShopApiError):
        hint = ""
        if error.status == 401 and "username or password" in error.message.lower():
            hint = " Check the username and password. The previous session, if any, is still active."
        elif error.status == 401:
            hint = " Sign in with mock_shop_login, or set MOCK_SHOP_USERNAME and MOCK_SHOP_PASSWORD."
        elif error.status == 402:
            hint = " Compute debt or an unpaid result is blocking this. Fund the same account, then retry the read; do not upload the script again."
        elif error.status == 409:
            hint = " Conflict: insufficient funds, a stale cart revision, a reused idempotency key with a different body, or another funding still in flight. Deposit SOL before paying. Reuse the original key only when the body is unchanged and the previous outcome was unknown."
        elif error.status == 400 and "Solana" in error.message:
            hint = " Seeded demo agents have no Solana keys. Register a new agent and fund Devnet before paying."
        status = "HTTP unknown" if error.status is None else f"HTTP {error.status}"
        message = error.message.rstrip(".")
        return f"Error: {status}: {message}.{hint}"
    return f"Error: Unexpected failure ({type(error).__name__}): {error}"


def _page(limit: int | None, offset: int | None) -> dict[str, int]:
    params: dict[str, int] = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return params


def _lines(items: list[LineItem]) -> list[dict[str, Any]]:
    return [item.model_dump(exclude_none=True) for item in items]


def _path(value: str) -> str:
    return quote(value, safe="")


async def _public(method: str, path: str, **kwargs: Any) -> str:
    try:
        _status, body = await session().request(method, path, auth=False, **kwargs)
        return _json(body)
    except Exception as error:  # noqa: BLE001 — tool boundary
        return _error(error)


async def _authed(method: str, path: str, **kwargs: Any) -> str:
    try:
        _status, body = await session().request(method, path, auth=True, **kwargs)
        return _json(body)
    except Exception as error:  # noqa: BLE001
        return _error(error)


async def _write(method: str, path: str, *, key: str | None, **kwargs: Any) -> str:
    idem = key or new_idempotency_key()
    try:
        status, body = await session().request(
            method, path, auth=True, idempotency_key=idem, **kwargs
        )
        return _json({**body, "http_status": status, "idempotency_key": idem})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_health", title="Mock shop health", annotations=READ)
async def mock_shop_health() -> str:
    """Check that the marketplace API and PostgreSQL are reachable. Does not check Solana or the provider."""
    return await _public("GET", "/api/health")


@mcp.tool(name="mock_shop_list_services", title="List services", annotations=READ)
async def mock_shop_list_services(
    kind: Literal["product", "api-usage", "compute"] | None = Field(
        default=None, description="Filter by offer kind. `service` is not valid."
    ),
    vendor_id: str | None = Field(default=None, description="Filter by vendor id, e.g. `vendor-a`.", pattern=_ID),
    product_type: str | None = Field(
        default=None, description="Filter by product type, e.g. `orange`, `apple`, `api-usage`, `compute`.", pattern=_ID
    ),
) -> str:
    """List catalog offers. Prices are minor units. Public API/compute offers omit private url and response_field. Use metrics, not the archival top-level quality columns."""
    return await _public(
        "GET",
        "/api/services",
        params={"kind": kind, "vendor_id": vendor_id, "product_type": product_type},
    )


@mcp.tool(name="mock_shop_get_service", title="Get service", annotations=READ)
async def mock_shop_get_service(
    service_id: str = Field(..., description="Offer id, e.g. `orange` or `vendor-a-thailand-weather`.", pattern=_ID),
) -> str:
    """Read one offer plus its purchase_stats. 404 if the id does not exist."""
    return await _public("GET", f"/api/services/{_path(service_id)}")


@mcp.tool(name="mock_shop_get_service_metrics", title="Get service metrics", annotations=READ)
async def mock_shop_get_service_metrics(
    service_id: str = Field(..., description="Offer id.", pattern=_ID),
) -> str:
    """Read system metrics for one offer. SLA, latency, review text, compliance, and categories are mocks. purchases, unique_buyers, and retention_rate come from agent order history."""
    return await _public("GET", f"/api/services/{_path(service_id)}/metrics")


@mcp.tool(name="mock_shop_get_service_purchase_count", title="Service purchase count", annotations=READ)
async def mock_shop_get_service_purchase_count(
    service_id: str = Field(..., description="Offer id.", pattern=_ID),
) -> str:
    """Global purchase totals for one offer. Counts every status, including historical anonymous orders."""
    return await _public("GET", f"/api/services/{_path(service_id)}/purchases/count")


@mcp.tool(name="mock_shop_list_vendors", title="List vendors", annotations=READ)
async def mock_shop_list_vendors() -> str:
    """List public vendors and their offer counts."""
    return await _public("GET", "/api/vendors")


@mcp.tool(name="mock_shop_get_vendor", title="Get vendor", annotations=READ)
async def mock_shop_get_vendor(
    vendor_id: str = Field(..., description="Vendor id, e.g. `vendor-a`.", pattern=_ID),
    include_offers: bool = Field(default=False, description="Also fetch that vendor's public offers."),
) -> str:
    """Read one public vendor. Set include_offers to attach their catalog."""
    try:
        _status, body = await session().request("GET", f"/api/vendors/{_path(vendor_id)}")
        if include_offers:
            _offer_status, offers = await session().request(
                "GET", f"/api/vendors/{_path(vendor_id)}/products"
            )
            body = {**body, "products": offers.get("products", [])}
        return _json(body)
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_global_purchase_stats", title="Global purchase stats", annotations=READ)
async def mock_shop_global_purchase_stats() -> str:
    """Public marketplace totals, including anonymous history. This is not the signed-in agent's own stats."""
    return await _public("GET", "/api/purchases/count/total")


@mcp.tool(name="mock_shop_register", title="Register agent", annotations=WRITE)
async def mock_shop_register(
    username: str = Field(..., description="New agent username, 3-64 chars: a-z, 0-9, _ . -", pattern=r"^[a-z0-9_.-]{3,64}$"),
    password: str = Field(..., description="Password, 8-128 characters. Not echoed back.", min_length=8, max_length=128),
) -> str:
    """Register an agent and keep the session server-side. The response has user and expires_at, never the bearer token. Registration creates a Solana keypair; it does not airdrop Devnet SOL."""
    try:
        status, body = await session().register(username, password)
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_login", title="Log in", annotations=SAFE_WRITE)
async def mock_shop_login(
    username: str = Field(..., description="Existing username.", pattern=r"^[a-z0-9_.-]{3,64}$"),
    password: str = Field(..., description="Password. Not echoed back.", min_length=8, max_length=128),
) -> str:
    """Log in and replace the server-side session. Demo seed logins are agent_1, agent_2, agent_3, and vendor_a is not an agent. Password for the seed agents is password123. The bearer token is not returned."""
    try:
        status, body = await session().login(username, password)
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_logout", title="Log out", annotations=SAFE_WRITE)
async def mock_shop_logout() -> str:
    """Log out the current server-side session only. Other sessions and the saved cart remain."""
    try:
        if session().token is None:
            return _json({"status": "logged_out", "http_status": 200})
        status, body = await session().logout()
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_whoami", title="Current agent", annotations=READ)
async def mock_shop_whoami() -> str:
    """Return the signed-in user. Uses env credentials when no login tool has been called yet."""
    return await _authed("GET", "/api/auth/me")


@mcp.tool(name="mock_shop_get_wallet", title="Get wallet", annotations=READ)
async def mock_shop_get_wallet() -> str:
    """Read the agent wallet. A successful call can mirror the on-chain SOL balance into SQL. Currency in the DTO is SOL."""
    return await _authed("GET", "/api/wallet")


@mcp.tool(name="mock_shop_topup_wallet", title="Top up wallet", annotations=WRITE)
async def mock_shop_topup_wallet(
    amount_minor: int = Field(..., description="SQL credit in minor units, at least 1. Does not guarantee a Devnet airdrop.", ge=1, le=2_147_483_647),
    idempotency_key: str | None = Field(
        default=None,
        description="Retry key. Omit to generate one. Reuse it with the same amount after a timeout.",
        max_length=128,
    ),
) -> str:
    """Credit the agent SQL wallet and try a Devnet airdrop of at most 1 SOL. Replay does not airdrop again. The returned idempotency_key must be reused for a retry of this same amount."""
    return await _write(
        "POST",
        "/api/wallet/topups",
        key=idempotency_key,
        json_body={"amount_minor": amount_minor},
    )


@mcp.tool(name="mock_shop_list_wallet_transactions", title="Wallet ledger", annotations=READ)
async def mock_shop_list_wallet_transactions(
    limit: int | None = Field(default=None, description="Page size 1-100. Default 20.", ge=1, le=100),
    offset: int | None = Field(default=None, description="Rows to skip.", ge=0),
) -> str:
    """List the signed-in agent's wallet ledger, newest first."""
    return await _authed("GET", "/api/wallet/transactions", params=_page(limit, offset))


@mcp.tool(name="mock_shop_get_cart", title="Get cart", annotations=READ)
async def mock_shop_get_cart() -> str:
    """Read the durable cart and its revision. Prices are current quotes, not a reservation."""
    return await _authed("GET", "/api/cart")


@mcp.tool(name="mock_shop_set_cart", title="Replace cart", annotations=SAFE_WRITE)
async def mock_shop_set_cart(
    items: list[LineItem] = Field(..., description="Full cart contents. Pass an empty list to clear.", max_length=20),
    revision: int | None = Field(default=None, description="Cart revision from mock_shop_get_cart. Omit to overwrite without a concurrency check.", ge=0),
) -> str:
    """Replace the cart. A stale revision with a different cart returns 409. The same saved state returns 200 without bumping revision."""
    body: dict[str, Any] = {"items": _lines(items)}
    if revision is not None:
        body["revision"] = revision
    try:
        status, payload = await session().request("PUT", "/api/cart", json_body=body, auth=True)
        return _json({**payload, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_set_cart_item", title="Set cart item quantity", annotations=SAFE_WRITE)
async def mock_shop_set_cart_item(
    product_id: str = Field(..., description="Offer id.", pattern=_ID),
    quantity: int = Field(..., description="Absolute quantity, 1-999. This does not add to the current quantity.", ge=1, le=999),
) -> str:
    """Set one line to an absolute quantity. Retrying the same quantity does not increment it."""
    try:
        status, body = await session().request(
            "PUT",
            f"/api/cart/items/{_path(product_id)}",
            json_body={"quantity": quantity},
            auth=True,
        )
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_remove_cart_item", title="Remove cart item", annotations=SAFE_WRITE)
async def mock_shop_remove_cart_item(
    product_id: str = Field(..., description="Offer id to remove.", pattern=_ID),
) -> str:
    """Remove one offer from the cart. Removing a missing line still returns the cart."""
    try:
        status, body = await session().request(
            "DELETE", f"/api/cart/items/{_path(product_id)}", auth=True
        )
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_clear_cart", title="Clear cart", annotations=SAFE_WRITE)
async def mock_shop_clear_cart() -> str:
    """Delete every cart line. Does not spend funds and does not check revision."""
    try:
        status, body = await session().request("DELETE", "/api/cart", auth=True)
        return _json({**body, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_checkout_cart", title="Checkout cart", annotations=WRITE)
async def mock_shop_checkout_cart(
    revision: int = Field(..., description="revision from the cart you intend to buy.", ge=0),
    idempotency_key: str | None = Field(
        default=None,
        description="Retry key. After a timeout, retry this same revision and key even if GET cart shows a newer revision.",
        max_length=128,
    ),
) -> str:
    """Pay for the current cart on Solana and clear it in SQL. Reuse the returned idempotency_key with the same revision after a timeout. A new key is a new payment. Seeded agents without Solana keys fail before payment."""
    return await _write(
        "POST",
        "/api/cart/checkout",
        key=idempotency_key,
        json_body={"revision": revision},
    )


@mcp.tool(name="mock_shop_purchase", title="Purchase offers", annotations=WRITE)
async def mock_shop_purchase(
    items: list[LineItem] = Field(..., description="1-20 lines. Does not change the cart.", min_length=1, max_length=20),
    idempotency_key: str | None = Field(
        default=None,
        description="Retry key. Reuse it only with the same quantities. A new key buys again.",
        max_length=128,
    ),
) -> str:
    """Buy offers directly. This spends Devnet SOL (1 minor = 0.01 SOL plus gas) and then records SQL. Seeded demo agents have no keys and fail with 400. Keep the returned idempotency_key if the response is lost."""
    return await _write(
        "POST",
        "/api/purchases",
        key=idempotency_key,
        json_body={"items": _lines(items)},
    )


@mcp.tool(name="mock_shop_list_purchases", title="Purchase history", annotations=READ)
async def mock_shop_list_purchases(
    limit: int | None = Field(default=None, description="Page size 1-100. Default 20.", ge=1, le=100),
    offset: int | None = Field(default=None, description="Rows to skip.", ge=0),
) -> str:
    """List the signed-in agent's receipts, newest first."""
    return await _authed("GET", "/api/purchases", params=_page(limit, offset))


@mcp.tool(name="mock_shop_get_purchase", title="Get purchase", annotations=READ)
async def mock_shop_get_purchase(
    purchase_id: str = Field(..., description="Receipt UUID.", pattern=_UUID),
) -> str:
    """Read one of the signed-in agent's receipts. Another agent's receipt is 404."""
    return await _authed("GET", f"/api/purchases/{_path(purchase_id)}")


@mcp.tool(name="mock_shop_my_purchase_stats", title="My purchase stats", annotations=READ)
async def mock_shop_my_purchase_stats() -> str:
    """Totals for the signed-in agent only. Use mock_shop_global_purchase_stats for the public marketplace."""
    return await _authed("GET", "/api/purchases/count")


@mcp.tool(
    name="mock_shop_evaluate_purchase",
    title="Evaluate purchase",
    annotations=WRITE,
    description=(
        "Submit a 1-5 rating for a receipt you own. Not idempotent: every call overwrites the rating "
        "and increments the archival api_call_count, even a retry. System metrics stay mocked."
    ),
)
async def mock_shop_evaluate_purchase(
    purchase_id: str = Field(..., description="Receipt UUID you own.", pattern=_UUID),
    rating: int = Field(..., description="Integer rating from 1 to 5.", ge=1, le=5),
    comment: str | None = Field(default=None, description="Optional comment. There is no separate idempotency key."),
) -> str:
    """Rate one owned receipt. Do not retry blindly; each call counts again."""
    body: dict[str, Any] = {"rating": rating}
    if comment is not None:
        body["comment"] = comment
    try:
        status, payload = await session().request(
            "POST",
            f"/api/purchases/{_path(purchase_id)}/evaluation",
            json_body=body,
            auth=True,
        )
        return _json({**payload, "http_status": status})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_list_purchased_api_usages", title="List purchased API usages", annotations=READ)
async def mock_shop_list_purchased_api_usages(
    status: Literal["active", "used"] | None = Field(default=None, description="Filter by entitlement status."),
    limit: int | None = Field(default=None, description="Page size 1-100. Default 20.", ge=1, le=100),
    offset: int | None = Field(default=None, description="Rows to skip.", ge=0),
) -> str:
    """List one-time API entitlements. The id here is the entitlement UUID, not the catalog offer id."""
    params = _page(limit, offset)
    if status is not None:
        params["status"] = status
    return await _authed("GET", "/api/api-usages/purchased", params=params)


@mcp.tool(name="mock_shop_get_purchased_api_usage", title="Get purchased API usage", annotations=READ)
async def mock_shop_get_purchased_api_usage(
    entitlement_id: str = Field(..., description="Entitlement UUID from the purchased list.", pattern=_UUID),
) -> str:
    """Read one owned API entitlement, including its call snapshot when already used."""
    return await _authed("GET", f"/api/api-usages/purchased/{_path(entitlement_id)}")


@mcp.tool(name="mock_shop_use_api_usage", title="Use purchased API", annotations=WRITE)
async def mock_shop_use_api_usage(
    entitlement_id: str = Field(..., description="Active entitlement UUID.", pattern=_UUID),
    arguments: dict[str, Any] | None = Field(
        default=None,
        description="Request arguments validated against the offer schema. Use {} for weather. Random needs integer min and max.",
    ),
) -> str:
    """Consume one API entitlement by calling the mock provider. HTTP 200 means the attempt was recorded; read api_call_status. The same UUID and arguments replay without a new call. Different arguments after a call return 409. A failed provider call still consumes the entitlement."""
    try:
        status, body = await session().request(
            "POST",
            f"/api/api-usages/purchased/{_path(entitlement_id)}/use",
            json_body={"arguments": arguments or {}},
            auth=True,
        )
        call = (body.get("service") or {}).get("api_call") or {}
        return _json({**body, "http_status": status, "api_call_status": call.get("status")})
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_list_compute_accounts", title="List compute accounts", annotations=READ)
async def mock_shop_list_compute_accounts() -> str:
    """List compute budgets that already exist for this agent. A never-funded offer is omitted here."""
    return await _authed("GET", "/api/compute/accounts")


@mcp.tool(name="mock_shop_get_compute_account", title="Get compute account", annotations=READ)
async def mock_shop_get_compute_account(
    service_id: str = Field(..., description="Compute offer id, e.g. `vendor-a-python-compute`.", pattern=_ID),
) -> str:
    """Read one compute budget, including a virtual zero balance if it was never funded."""
    return await _authed("GET", f"/api/compute/{_path(service_id)}/account")


@mcp.tool(name="mock_shop_fund_compute", title="Fund compute", annotations=WRITE)
async def mock_shop_fund_compute(
    service_id: str = Field(..., description="Compute offer id.", pattern=_ID),
    amount_minor: int = Field(
        ...,
        description="Credit to buy. 1000 minor transfers 10 SOL. Debt is repaid before extra credit is added.",
        ge=1,
        le=2_147_483_647,
    ),
    idempotency_key: str | None = Field(
        default=None,
        description="Retry key. Required to settle a prepared funding. Do not change the amount.",
        max_length=128,
    ),
) -> str:
    """Move SOL into a compute budget. http_status 201/200 paid is settled. 202 prepared is not settled: retry the same key and amount. 409 with funding.status failed stays failed for that key. This does not airdrop SOL."""
    idem = idempotency_key or new_idempotency_key()
    try:
        status, body = await session().request(
            "POST",
            f"/api/compute/{_path(service_id)}/fundings",
            json_body={"amount_minor": amount_minor},
            auth=True,
            idempotency_key=idem,
            timeout=FUNDING_TIMEOUT_SECONDS,
            accept_statuses={202, 409},
        )
        if status == 409 and "funding" not in body:
            raise MockShopApiError(409, str(body.get("error", body)), body)
        funding = body.get("funding") if isinstance(body.get("funding"), dict) else {}
        state = funding.get("status")
        hint = None
        if status == 202 or state == "prepared":
            hint = (
                "Confirmation pending. Retry mock_shop_fund_compute with the same "
                "service_id, amount_minor, and idempotency_key. Do not start a second payment."
            )
        elif state == "failed":
            hint = (
                "This funding key is failed. Read funding.error. "
                "Do not retry this key as a new payment."
            )
        payload = {**body, "http_status": status, "idempotency_key": idem}
        if hint:
            payload["hint"] = hint
        return _json(payload)
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_run_compute", title="Run compute", annotations=WRITE)
async def mock_shop_run_compute(
    service_id: str = Field(..., description="Funded compute offer id.", pattern=_ID),
    source: str = Field(..., description="Python source. Empty source is allowed. Max 65536 bytes."),
    filename: str = Field(default="script.py", description="Upload name ending in .py, 1-200 chars.", min_length=1, max_length=200),
    idempotency_key: str | None = Field(
        default=None,
        description="Retry key. Identity is the source bytes, not the filename. Reuse the key and the same source after a timeout.",
        max_length=128,
    ),
) -> str:
    """Upload Python and run it against prepaid credit. HTTP 201 can still be a failed or indebted run; read run.status, unpaid_minor, and result_available. A provider failure does not charge. Do not re-upload after an ambiguous timeout; retry the same key and source."""
    if "\x00" in filename or not filename.lower().endswith(".py"):
        return _error(MockShopApiError(400, "filename must end in .py and must not contain NUL."))
    source_bytes = source.encode("utf-8")
    if len(source_bytes) > 65536:
        return _error(MockShopApiError(413, "Python source exceeds 65536 bytes."))
    idem = idempotency_key or new_idempotency_key()
    try:
        status, body = await session().request(
            "POST",
            f"/api/compute/{_path(service_id)}/runs",
            files={"file": (filename, source_bytes, "text/x-python")},
            auth=True,
            idempotency_key=idem,
            timeout=COMPUTE_RUN_TIMEOUT_SECONDS,
        )
        run = body.get("run") if isinstance(body.get("run"), dict) else {}
        return _json({
            **body,
            "http_status": status,
            "idempotency_key": idem,
            "run_status": run.get("status"),
            "result_available": run.get("result_available"),
            "unpaid_minor": run.get("unpaid_minor"),
        })
    except Exception as error:  # noqa: BLE001
        return _error(error)


@mcp.tool(name="mock_shop_list_compute_runs", title="List compute runs", annotations=READ)
async def mock_shop_list_compute_runs(
    service_id: str = Field(..., description="Compute offer id.", pattern=_ID),
    limit: int | None = Field(default=None, description="Page size 1-100. Default 20.", ge=1, le=100),
    offset: int | None = Field(default=None, description="Rows to skip.", ge=0),
) -> str:
    """List this agent's runs for one compute offer, newest first."""
    return await _authed("GET", f"/api/compute/{_path(service_id)}/runs", params=_page(limit, offset))


@mcp.tool(name="mock_shop_get_compute_run", title="Get compute run", annotations=READ)
async def mock_shop_get_compute_run(
    run_id: str = Field(..., description="Run UUID.", pattern=_UUID),
    include_result: bool = Field(
        default=False,
        description="Read the result endpoint. Unpaid debt returns 402; fund the account, then call this again. Do not rerun the script.",
    ),
) -> str:
    """Read one owned run. Set include_result only after unpaid_minor is 0. Running or failed runs have no result."""
    suffix = "/result" if include_result else ""
    return await _authed("GET", f"/api/compute/runs/{_path(run_id)}{suffix}")


if __name__ == "__main__":
    mcp.run()
