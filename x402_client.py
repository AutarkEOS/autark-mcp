"""x402 V2 payment client for the Autark MCP server.

Implements the HTTP x402 V2 protocol:
  1. Make an ordinary GET/POST request.
  2. If the server responds 402, parse the ``PAYMENT-REQUIRED`` header (base64 JSON).
  3. Select an acceptable payment requirement using the agent's spending policy.
  4. Build and sign a ``PaymentPayload`` for the selected requirement.
  5. Retry the original request with the ``PAYMENT-SIGNATURE`` header.
  6. Parse the ``PAYMENT-RESPONSE`` header from the 200 reply.

References
----------
- https://solana.com/docs/payments/agentic-payments/intro-to-x402
- https://x402.org/x402-v2-launch/

Only the ``exact`` scheme on Solana Devnet is implemented here.  The ``upto``
scheme and mainnet are out of scope for the hackathon MVP.

Usage::

    result = await fetch_with_x402(
        url="https://example.com/api/weather",
        method="GET",
        arguments={},
        agent_keypair_encrypted=encrypted_privkey_bytes,
        max_payment_minor=50,          # spending limit in minor units
        allowed_assets=None,           # None = accept any asset
    )
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# CAIP-2 identifier for Solana Devnet (first 32 chars of genesis hash).
SOLANA_DEVNET = "solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1"
SOLANA_MAINNET = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"

# Devnet USDC mint (for display / filtering; we pay in SOL for the hackathon).
DEVNET_USDC_MINT = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"


class X402Error(Exception):
    """Base class for x402 protocol errors."""


class X402RequiresPayment(X402Error):
    """A 402 response was received but no acceptable requirement was found."""

    def __init__(self, message: str, requirements: list[dict]):
        super().__init__(message)
        self.requirements = requirements


class X402PolicyRejection(X402Error):
    """A valid requirement was found but it was blocked by the spending policy."""


def _decode_header(header_value: str) -> Any:
    """Decode a base64-encoded x402 protocol object from a header."""
    try:
        padded = header_value + "=" * (-len(header_value) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception as exc:
        raise X402Error(f"Could not decode x402 header: {exc}") from exc


def _encode_payload(payload: dict) -> str:
    """Encode a dict as a base64url-encoded JSON string for a request header."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _select_requirement(
    requirements: list[dict],
    *,
    max_payment_minor: int | None,
    allowed_networks: list[str] | None,
) -> dict | None:
    """Return the first requirement acceptable under the agent's constraints.

    Preference: exact/solana-devnet SOL, then exact/solana-devnet USDC.
    Returns None when no requirement passes.
    """
    preferred_networks = allowed_networks or [SOLANA_DEVNET]
    for req in requirements:
        if req.get("scheme") != "exact":
            continue
        network = req.get("network", "")
        if network not in preferred_networks:
            continue
        amount = req.get("amount")
        try:
            amount_int = int(amount) if amount is not None else None
        except (ValueError, TypeError):
            continue
        if max_payment_minor is not None and amount_int is not None and amount_int > max_payment_minor:
            continue
        return req
    return None


def _agent_private_key(encrypted_privkey: str) -> bytes:
    """Agent keys are encrypted with the marketplace's server-side key, which this process must
    never hold, and the marketplace API never returns them. Signing needs a key the agent keeps
    locally; that is not implemented yet."""
    raise X402Error(
        "Signing x402 payments needs an agent-held Solana keypair, which is not implemented. "
        "The marketplace never shares its encrypted agent keys with this server."
    )


async def _sign_solana_payment(
    requirement: dict,
    encrypted_privkey: str,
) -> dict:
    """Build and sign a PaymentPayload for a Solana exact requirement.

    Returns the payload dict ready to be base64-encoded into PAYMENT-SIGNATURE.

    The payment is a SOL transfer (lamports = amount * 1_000 where 1 minor = 0.000001 SOL)
    to the ``payTo`` address in the requirement.  For USDC requirements a separate
    SPL-token transfer would be needed; that is not implemented here.
    """
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction
    from solana.rpc.async_api import AsyncClient

    from agent_config import AgentConfigError

    import os

    privkey_bytes = _agent_private_key(encrypted_privkey)
    sender_kp = Keypair.from_bytes(privkey_bytes)
    pay_to = requirement.get("payTo")
    if not pay_to:
        raise X402Error("Requirement is missing payTo address.")

    amount_lamports = int(requirement["amount"]) * 1_000  # minor → lamports

    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.devnet.solana.com")
    async with AsyncClient(rpc_url, timeout=10) as client:
        blockhash_resp = await client.get_latest_blockhash()
        blockhash = blockhash_resp.value.blockhash
        ix = transfer(TransferParams(
            from_pubkey=sender_kp.pubkey(),
            to_pubkey=Pubkey.from_string(pay_to),
            lamports=amount_lamports,
        ))
        tx = Transaction.new_with_payer([ix], sender_kp.pubkey())
        tx.sign([sender_kp], blockhash)
        tx_bytes = bytes(tx)

    return {
        "scheme": "exact",
        "network": requirement.get("network", SOLANA_DEVNET),
        "asset": requirement.get("asset"),
        "amount": requirement["amount"],
        "payTo": pay_to,
        "from": str(sender_kp.pubkey()),
        "transaction": base64.b64encode(tx_bytes).decode(),
    }


async def fetch_with_x402(
    *,
    url: str,
    method: str = "GET",
    arguments: dict | None = None,
    encrypted_privkey: str | None = None,
    max_payment_minor: int | None = None,
    allowed_networks: list[str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Make an HTTP request, handling a 402 response automatically.

    Returns a dict with keys:
        status (int), body (any), payment_response (dict|None),
        paid (bool), requirement (dict|None), amount_paid (int|None).

    Raises X402RequiresPayment when a 402 is received but no requirement passes
    the agent's constraints.  Raises X402PolicyRejection when the amount exceeds
    the spending limit.  Raises X402Error for protocol-level errors.
    """
    params = arguments or {}
    headers_first: dict[str, str] = {"Accept": "application/json"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
        # Initial request.
        if method.upper() == "GET":
            resp = await http.get(url, params=params, headers=headers_first)
        else:
            resp = await http.request(method, url, json=params, headers=headers_first)

        if resp.status_code != 402:
            return {
                "status": resp.status_code,
                "body": _try_json(resp),
                "paid": False,
                "requirement": None,
                "amount_paid": None,
                "payment_response": None,
            }

        # Parse the 402 payment requirements.
        pr_header = resp.headers.get("PAYMENT-REQUIRED") or resp.headers.get("X-PAYMENT-REQUIRED")
        if not pr_header:
            raise X402Error("Server returned 402 but no PAYMENT-REQUIRED header was found.")

        pr_object = _decode_header(pr_header)
        requirements: list[dict] = pr_object.get("accepts") or pr_object.get("requirements") or []
        if not requirements:
            raise X402Error("PAYMENT-REQUIRED object contains no requirements.")

        requirement = _select_requirement(
            requirements,
            max_payment_minor=max_payment_minor,
            allowed_networks=allowed_networks,
        )
        if requirement is None:
            raise X402RequiresPayment(
                f"No acceptable x402 payment requirement found among {len(requirements)} option(s). "
                f"Spending limit: {max_payment_minor} minor. "
                f"Available networks: {[r.get('network') for r in requirements]}.",
                requirements,
            )

        amount_minor = int(requirement.get("amount", 0))
        if max_payment_minor is not None and amount_minor > max_payment_minor:
            raise X402PolicyRejection(
                f"x402 payment of {amount_minor} minor exceeds the per-transaction limit of {max_payment_minor} minor."
            )

        if encrypted_privkey is None:
            raise X402Error("An x402 payment is required but no agent keypair is available.")

        payload = await _sign_solana_payment(requirement, encrypted_privkey)
        payment_signature_header = _encode_payload(payload)

        # Retry with the signed payment.
        retry_headers = {**headers_first, "PAYMENT-SIGNATURE": payment_signature_header}
        if method.upper() == "GET":
            paid_resp = await http.get(url, params=params, headers=retry_headers)
        else:
            paid_resp = await http.request(method, url, json=params, headers=retry_headers)

        payment_response_header = paid_resp.headers.get("PAYMENT-RESPONSE")
        payment_response = _decode_header(payment_response_header) if payment_response_header else None

        return {
            "status": paid_resp.status_code,
            "body": _try_json(paid_resp),
            "paid": paid_resp.status_code == 200,
            "requirement": requirement,
            "amount_paid": amount_minor,
            "payment_response": payment_response,
        }


def _try_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text
