# mock-shop MCP server

stdio MCP server for the agent side of [mock-servivce](../mock-servivce/). Tools call the marketplace REST API. The bearer token stays in this process and is never returned to the model.

Requires Python 3.10+ and a running mock-service stack (`bash ../mock-servivce/scripts/start.sh`).

## Setup

```bash
cd mock-mcp
python3 -m venv .venv
.venv/bin/python -E -m pip install -r requirements.txt
```

`-E` makes pip ignore `PYTHONPATH`. Without it, a shell that exports `PYTHONPATH` (ROS, `~/.local`) makes pip treat packages found there as already installed, and the venv ends up missing `pydantic`, `httpx`, and `anyio`. The server is launched with `-E` for the same reason.

Quick self-check:

```bash
.venv/bin/python -E -c "import mcp, httpx, pydantic; print('ok')"
```

## Cursor

```json
{
  "mcpServers": {
    "mock-shop": {
      "command": "/absolute/path/to/Autark/mock-mcp/.venv/bin/python",
      "args": ["-E", "/absolute/path/to/Autark/mock-mcp/server.py"],
      "env": {
        "MOCK_SERVICE_API_URL": "http://127.0.0.1:5000",
        "MOCK_SHOP_USERNAME": "mcp_probe_agent",
        "MOCK_SHOP_PASSWORD": "probe-password-2026"
      }
    }
  }
}
```

`MOCK_SERVICE_API_URL` is optional. When it is omitted, the server reads `mock-servivce/config` (including `local.config.json`) and uses the backend host and port. Only loopback origins are accepted; a trailing `/api` is stripped, any other path is rejected.

`MOCK_SHOP_USERNAME` and `MOCK_SHOP_PASSWORD` log in on the first tool that needs a session. `mock_shop_login` and `mock_shop_register` replace that session.

`agent_1`, `agent_2`, and `agent_3` (`password123`) exist only after `bash scripts/start.sh --initial-seed`. That reset deletes business data, so a normal start does not create them. A migrated database with no users will make those env credentials return 401. Register an agent, or point the env vars at an account that exists. Demo seed agents also have no Solana keys. A newly registered agent has a keypair but no Devnet SOL, so purchase and compute funding fail until that address is funded.

## Agent config

The agent is configured once and then follows that config: which offers it prefers and how much it may spend. The config is a local JSON file, `~/.autark/agent-config.json`. It never leaves this machine and holds no credentials.

| Variable | Default |
| --- | --- |
| `AUTARK_AGENT_CONFIG` | `~/.autark/agent-config.json` |
| `AUTARK_DECISION_LOG` | `decisions.jsonl` next to the config |

```json
{
  "version": 1,
  "purpose": "Buy fruit for a kitchen, cheapest reliable supplier",
  "purchase_mode": "ask",
  "spending": {
    "max_per_transaction_minor": 500,
    "daily_budget_minor": 2000,
    "allowed_kinds": ["product", "api-usage"]
  },
  "preferences": {
    "priorities": ["quality", "reliability", "price"],
    "limits": { "max_price_minor": 300, "min_rating": 3 },
    "risk_tolerance": "low"
  }
}
```

Every field is optional. `null` or a missing spending limit means no limit. `preferences` takes the same fields as `POST /api/services/rank` (see `mock-servivce/API.md`, "Ranking ofert") and is validated by the marketplace's own `backend/services/ranking.py`.

`mock_shop_configure_agent` creates the file on first use. After that the model may change `purpose` and `preferences` and may **tighten** spending, but it cannot raise or remove a limit, widen `allowed_kinds`, or switch `purchase_mode` from `ask` to `auto`. Those changes are a human edit of the file. `purchase_mode` is guidance for the model; the spending limits are what the code enforces.

Without a config file nothing is limited and ranking uses the `balanced` profile. A file that exists but cannot be parsed blocks every spending tool until it is fixed.

### Spending policy

`mock_shop_purchase`, `mock_shop_checkout_cart` and `mock_shop_fund_compute` run `policy.py` before any request that can move funds:

1. Price the request from the live catalog or cart. The model never supplies a price.
2. Reject offer kinds outside `allowed_kinds`.
3. Reject a total above `max_per_transaction_minor`.
4. Reject when today's debits in the wallet ledger (UTC day) plus this total exceed `daily_budget_minor`.

Anything that cannot be verified is a rejection: an unreadable price, ledger or config, a cart at another revision, or a decision that cannot be written to the log. A rejected call returns `error`, `policy` and `"Nothing was spent"`; no payment request is sent. A retry with the same `idempotency_key` and the same body is let through without counting the budget twice.

Limits of this guard:

- It covers this MCP server only. A client that calls the marketplace REST API directly is not limited.
- The price can change between the quote and the payment; the marketplace charges its current price. Paid responses report `policy.quoted_total_minor` next to `policy.charged_total_minor`.
- Approved retries are remembered in memory. After a server restart, a checkout retry is priced again; check `mock_shop_list_purchases` before retrying.

### Decision log

Each ranking, policy decision and config change is appended to `decisions.jsonl` as one JSON object with a UTC timestamp. Passwords and session tokens are never written.

## Tools

| Tool | API |
| --- | --- |
| `mock_shop_get_agent_config` | local file, no API call |
| `mock_shop_configure_agent` | local file, no API call |
| `mock_shop_rank_services` | `POST /api/services/rank` with the saved preferences |
| `mock_shop_health` | `GET /api/health` |
| `mock_shop_list_services` | `GET /api/services` |
| `mock_shop_get_service` | `GET /api/services/{id}` |
| `mock_shop_get_service_metrics` | `GET /api/services/{id}/metrics` |
| `mock_shop_get_service_purchase_count` | `GET /api/services/{id}/purchases/count` |
| `mock_shop_list_vendors` | `GET /api/vendors` |
| `mock_shop_get_vendor` | `GET /api/vendors/{id}` and, optionally, its offers |
| `mock_shop_global_purchase_stats` | `GET /api/purchases/count/total` |
| `mock_shop_register` | `POST /api/auth/register` (role `agent`) |
| `mock_shop_login` | `POST /api/auth/login` |
| `mock_shop_logout` | `POST /api/auth/logout` |
| `mock_shop_whoami` | `GET /api/auth/me` |
| `mock_shop_get_wallet` | `GET /api/wallet` |
| `mock_shop_topup_wallet` | `POST /api/wallet/topups` |
| `mock_shop_list_wallet_transactions` | `GET /api/wallet/transactions` |
| `mock_shop_get_cart` | `GET /api/cart` |
| `mock_shop_set_cart` | `PUT /api/cart` |
| `mock_shop_set_cart_item` | `PUT /api/cart/items/{id}` |
| `mock_shop_remove_cart_item` | `DELETE /api/cart/items/{id}` |
| `mock_shop_clear_cart` | `DELETE /api/cart` |
| `mock_shop_checkout_cart` | `POST /api/cart/checkout` |
| `mock_shop_purchase` | `POST /api/purchases` |
| `mock_shop_list_purchases` | `GET /api/purchases` |
| `mock_shop_get_purchase` | `GET /api/purchases/{id}` |
| `mock_shop_my_purchase_stats` | `GET /api/purchases/count` |
| `mock_shop_evaluate_purchase` | `POST /api/purchases/{id}/evaluation` (not idempotent) |
| `mock_shop_list_purchased_api_usages` | `GET /api/api-usages/purchased` |
| `mock_shop_get_purchased_api_usage` | `GET /api/api-usages/purchased/{id}` |
| `mock_shop_use_api_usage` | `POST /api/api-usages/purchased/{id}/use` |
| `mock_shop_list_compute_accounts` | `GET /api/compute/accounts` |
| `mock_shop_get_compute_account` | `GET /api/compute/{id}/account` |
| `mock_shop_fund_compute` | `POST /api/compute/{id}/fundings` |
| `mock_shop_run_compute` | `POST /api/compute/{id}/runs` |
| `mock_shop_list_compute_runs` | `GET /api/compute/{id}/runs` |
| `mock_shop_get_compute_run` | `GET /api/compute/runs/{id}` or `/result` |

Writes that accept `idempotency_key` generate one when it is omitted and return it. Reuse that key with the same body after a timeout. A new key is a new payment. Compute funding `http_status` 202 means the transfer is not settled yet; retry the same key and amount.

`mock_shop_rank_services` merges per-call `preferences` over the saved ones: naming `weights`, `priorities` or `profile` replaces the saved weighting, while `limits` and `expectations` are merged key by key. `max_per_transaction_minor` is also applied as a price filter (`limit // quantity`), and each result carries `allowed_by_spending_policy`. Reliability and latency are labelled `simulated` until the marketplace has measured them; the tool's `warnings` say when that is the case.

Vendor publishing, legacy `/execute`, and `/auth/registered-agents` are not exposed.

## Tests

```bash
.venv/bin/python -E -m unittest discover -s tests
```

```powershell
.\.venv\Scripts\python.exe -E -m unittest discover -s tests
```

The tests use a scripted marketplace session and a temporary config directory. They need no running stack and make no payments.
