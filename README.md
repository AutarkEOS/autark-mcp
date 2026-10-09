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

## Tools

| Tool | API |
| --- | --- |
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

Vendor publishing, legacy `/execute`, and `/auth/registered-agents` are not exposed.
