"""Deterministic spending policy, checked before every tool that moves funds.

The model proposes a purchase; this code approves or rejects it from the local
agent config. Anything that cannot be verified is rejected.

This is the agent-side check. The marketplace enforces its own account policy
(PUT /api/agent/spending-policy) inside every paying transaction, so a client
that skips this server is still limited once that policy is set.
"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote as url_quote

from agent_config import AgentConfigError, append_audit, audit_path, config_path, load_config

LEDGER_PAGE_SIZE = 100
MAX_LEDGER_PAGES = 50
# Long enough to cover any retry of a timed-out payment, short enough to keep the file small.
APPROVAL_TTL = timedelta(hours=48)


class PolicyRejection(Exception):
    def __init__(self, reason: str, details: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or {}


def approvals_path() -> Path:
    """Idempotency key -> fingerprint of the body it was approved for, kept next to the decision log.

    A retry after a timeout, even after a server restart, must not be rejected just
    because the first attempt already used the budget.
    """
    return audit_path().parent / "approvals.json"


def _load_approvals() -> dict[str, dict[str, str]]:
    try:
        data = json.loads(approvals_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unknown approvals only mean a retry is checked again: the safe direction.
        return {}
    return data if isinstance(data, dict) else {}


def _remember_approval(key: str, print_: str) -> None:
    now = datetime.now(timezone.utc)
    approvals = {}
    for existing, entry in _load_approvals().items():
        try:
            if isinstance(entry, dict) and now - datetime.fromisoformat(entry["at"]) < APPROVAL_TTL:
                approvals[existing] = entry
        except (KeyError, TypeError, ValueError):
            continue
    approvals[key] = {"fingerprint": print_, "at": now.isoformat()}
    path = approvals_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(approvals, file)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _was_approved(key: str, print_: str) -> bool:
    entry = _load_approvals().get(key)
    return isinstance(entry, dict) and entry.get("fingerprint") == print_


def reset_approvals() -> None:
    try:
        approvals_path().unlink()
    except OSError:
        pass


def fingerprint(action: str, body: dict[str, Any]) -> str:
    # ranking_seed is audit metadata attached by this process; it is not part of the
    # paid basket. After a restart the in-memory seed is gone, so including it here
    # would make a same-key retry look like a new spend.
    commercial = {key: value for key, value in body.items() if key != "ranking_seed"}
    return hashlib.sha256(json.dumps([action, commercial], sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def quote_items(session: Any, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Price a direct purchase from the live catalog; the caller never supplies prices."""
    quantities: dict[str, int] = {}
    for line in lines:
        quantities[line["product_id"]] = quantities.get(line["product_id"], 0) + line["quantity"]
    total, kinds, priced = 0, set(), []
    for product_id, quantity in sorted(quantities.items()):
        _status, body = await session.request("GET", f"/api/services/{url_quote(product_id, safe='')}")
        service = body.get("service")
        if not isinstance(service, dict) or type(service.get("price_minor")) is not int or service["price_minor"] < 0:
            raise PolicyRejection(f"Could not read a price for {product_id}.")
        total += service["price_minor"] * quantity
        kinds.add(service.get("kind"))
        priced.append({"product_id": product_id, "quantity": quantity, "unit_price_minor": service["price_minor"], "kind": service.get("kind")})
    return {"total_minor": total, "kinds": sorted(str(kind) for kind in kinds), "lines": priced}


async def quote_cart(session: Any, revision: int) -> dict[str, Any]:
    _status, body = await session.request("GET", "/api/cart", auth=True)
    cart = body.get("cart", body)
    if cart.get("revision") != revision:
        raise PolicyRejection(
            f"Cart is at revision {cart.get('revision')}, not {revision}; its total cannot be verified. Read the cart again.")
    total = cart.get("total_minor")
    if type(total) is not int or total < 0:
        raise PolicyRejection("Could not read a single-currency cart total.")
    items = cart.get("items") or []
    return {"total_minor": total, "kinds": sorted({str(item.get("kind")) for item in items}),
            "lines": [{"product_id": item.get("product_id"), "quantity": item.get("quantity"),
                       "unit_price_minor": item.get("unit_price_minor"), "kind": item.get("kind")} for item in items]}


async def quote_amount(amount_minor: int, kind: str) -> dict[str, Any]:
    return {"total_minor": amount_minor, "kinds": [kind], "lines": []}


async def spent_today(session: Any, now: datetime | None = None) -> int:
    """Sum of today's debits (UTC day) from the agent's own wallet ledger, newest first."""
    now = now or datetime.now(timezone.utc)
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = 0
    for page in range(MAX_LEDGER_PAGES):
        _status, body = await session.request(
            "GET", "/api/wallet/transactions", auth=True,
            params={"limit": LEDGER_PAGE_SIZE, "offset": page * LEDGER_PAGE_SIZE})
        rows = body.get("transactions")
        if not isinstance(rows, list):
            raise PolicyRejection("Wallet ledger response was not understood.")
        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                amount = row["amount_minor"]
            except (KeyError, TypeError, ValueError):
                raise PolicyRejection("Wallet ledger entry was not understood.") from None
            if created.tzinfo is None or type(amount) is not int:
                raise PolicyRejection("Wallet ledger entry was not understood.")
            if created < start:
                return spent
            if amount < 0:
                spent -= amount
        if len(rows) < LEDGER_PAGE_SIZE:
            return spent
    raise PolicyRejection("Too many wallet entries today to verify the daily budget.")


async def authorize(
    session: Any,
    *,
    action: str,
    idempotency_key: str,
    body: dict[str, Any],
    quote: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Return the approval record, or raise PolicyRejection. Never spends."""
    try:
        config = load_config()
    except AgentConfigError as error:
        raise PolicyRejection(f"Agent config is unreadable, so spending is blocked: {error}") from None
    if config is None:
        return {"approved": True, "status": "no_config", "note": "No agent config exists, so no spending limits apply."}

    spending, print_ = config["spending"], fingerprint(action, body)
    record: dict[str, Any] = {"type": "policy", "action": action, "idempotency_key": idempotency_key, "limits": spending}
    try:
        if _was_approved(idempotency_key, print_):
            record.update(approved=True, status="retry_of_approved_request")
        else:
            try:
                priced = await quote()
            except PolicyRejection:
                raise
            except Exception as error:  # noqa: BLE001 — an unverifiable price is a rejection
                raise PolicyRejection(f"Could not verify the price: {error}") from None
            total = priced["total_minor"]
            record.update(quoted_total_minor=total, kinds=priced["kinds"], lines=priced["lines"])
            allowed = spending["allowed_kinds"]
            if allowed is not None and set(priced["kinds"]) - set(allowed):
                blocked = sorted(set(priced["kinds"]) - set(allowed))
                raise PolicyRejection(f"Offer kind not allowed by the agent config: {', '.join(blocked)}. Allowed: {', '.join(allowed) or 'none'}.")
            ceiling = spending["max_per_transaction_minor"]
            if ceiling is not None and total > ceiling:
                raise PolicyRejection(f"Total {total} minor exceeds the per-transaction limit of {ceiling} minor.")
            budget = spending["daily_budget_minor"]
            if budget is not None:
                try:
                    spent = await spent_today(session)
                except PolicyRejection:
                    raise
                except Exception as error:  # noqa: BLE001
                    raise PolicyRejection(f"Could not verify today's spending: {error}") from None
                record.update(spent_today_minor=spent, daily_budget_minor=budget)
                if spent + total > budget:
                    raise PolicyRejection(
                        f"Total {total} minor would exceed the daily budget: {spent} of {budget} minor already spent today (UTC).")
            record.update(approved=True, status="approved")
    except PolicyRejection as rejection:
        record.update(approved=False, status="rejected", reason=rejection.reason)
        rejection.details = record
        try:
            append_audit(record)
        except OSError:
            pass
        raise

    try:
        append_audit(record)
        _remember_approval(idempotency_key, print_)
    except OSError as error:
        raise PolicyRejection(f"The decision could not be recorded, so spending is blocked: {error.strerror or error}",
                              {**record, "approved": False, "status": "rejected"}) from None
    return record


def rejection_payload(rejection: PolicyRejection, idempotency_key: str) -> dict[str, Any]:
    return {
        "error": "Blocked by the agent spending policy: " + rejection.reason,
        "policy": {"approved": False, **rejection.details},
        "idempotency_key": idempotency_key,
        "hint": f"Nothing was spent. Limits can only be raised by a human editing {config_path()}.",
    }
