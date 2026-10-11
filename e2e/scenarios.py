"""End-to-end scenarios: a real MCP server over stdio against the real backend.

Each scenario gets its own temporary agent-config directory. Checks are recorded
with check(); a scenario never raises on a failed expectation.
"""
import asyncio
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MCP_ROOT = Path(__file__).resolve().parents[1]


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def __call__(self, label: str, condition: bool, detail: str = "") -> None:
        print(("PASS " if condition else "FAIL ") + label + (f"  [{detail}]" if detail else ""), flush=True)
        if not condition:
            self.failures.append(label)


class Agent:
    """One MCP server process, like one session of an agent client."""

    def __init__(self, api_url: str, config_dir: Path, username: str | None = None, password: str | None = None,
                 environment: dict[str, str] | None = None):
        env = {**os.environ, "MOCK_SERVICE_API_URL": api_url, "AUTARK_AGENT_CONFIG": str(config_dir / "agent-config.json"),
               "AUTARK_DECISION_LOG": str(config_dir / "decisions.jsonl"), **(environment or {})}
        env.pop("MOCK_SHOP_USERNAME", None)
        env.pop("MOCK_SHOP_PASSWORD", None)
        if username:
            env.update(MOCK_SHOP_USERNAME=username, MOCK_SHOP_PASSWORD=password)
        self.params = StdioServerParameters(command=sys.executable, args=["-E", str(MCP_ROOT / "server.py")], env=env)

    async def run(self, steps):
        async with stdio_client(self.params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, **arguments):
                result = await session.call_tool(name, arguments)
                text = result.content[0].text
                return text if text.startswith("Error") else json.loads(text)

            call.session = session
            return await steps(call)


def decision_log(config_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (config_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]


def debits(ledger: dict) -> list[int]:
    return [row["amount_minor"] for row in ledger["transactions"] if row["amount_minor"] < 0]


async def decision(api_url: str, config_dir: Path, check: Checks) -> None:
    """Ranking, one-time configuration and policy rejections. Nothing is bought."""

    async def steps(call):
        tools = {tool.name for tool in (await call.session.list_tools()).tools}
        check("server lists the ranking, config and sync tools", {"mock_shop_rank_services", "mock_shop_get_agent_config",
              "mock_shop_configure_agent", "mock_shop_sync_spending_policy"} <= tools, f"{len(tools)} tools")
        check("marketplace is reachable", (await call("mock_shop_health")).get("status") == "ok")
        check("no config on first run", (await call("mock_shop_get_agent_config"))["configured"] is False)
        default = await call("mock_shop_rank_services", product_type="apple", seed="e2e")
        check("unconfigured ranking uses the balanced default and hints at configuration",
              default["decision"]["preferences"]["weights_source"] == "default" and "hint" in default)

        created = await call("mock_shop_configure_agent", purpose="E2E frugal fruit buyer", max_per_transaction_minor=500,
                             daily_budget_minor=1000, allowed_kinds=["product"], preferences={"profile": "cheapest"})
        check("config is created on disk", created["created"] is True and (config_dir / "agent-config.json").exists())
        check("without a session the marketplace sync is reported, not fatal",
              created["marketplace_sync"]["status"] == "not_signed_in", created["marketplace_sync"]["status"])
        frugal = await call("mock_shop_rank_services", product_type="apple", seed="e2e")
        order = [item["service_id"] for item in frugal["decision"]["results"]]
        check("frugal agent picks the cheapest apple", frugal["decision"]["winner"]["service_id"] == "vendor-b-apple", " > ".join(order))
        check("saved preferences are used without passing them", frugal["decision"]["preferences"]["profile"] == "cheapest")
        check("spending limit became a price filter", frugal["decision"]["preferences"]["limits"] == {"max_price_minor": 500})
        check("apples are not scored on latency or reliability",
              frugal["decision"]["preferences"]["not_applicable"] == ["speed", "reliability"]
              and not any("simulated" in warning for warning in frugal["decision"]["warnings"]))

        await call("mock_shop_configure_agent", preferences={"profile": "quality_first"})
        quality = await call("mock_shop_rank_services", product_type="apple", seed="e2e")
        best = quality["decision"]["results"][0]
        check("quality-first agent picks the best reviewed apple", quality["decision"]["winner"]["service_id"] == "vendor-c-apple",
              " > ".join(item["service_id"] for item in quality["decision"]["results"]))
        rating = best["breakdown"]["quality"]
        check("quality comes from weighted agent reviews", rating["source"] == "agent_reviews" and rating["samples"] == 6,
              f"samples={rating['samples']} estimate={rating['estimate']}")
        total = sum(part["contribution"] for part in best["breakdown"].values())
        check("breakdown sums to the score", abs(total - best["score"]) < 1e-5, f"{total:.6f} vs {best['score']}")

        strict = await call("mock_shop_rank_services", product_type="apple", preferences={"limits": {"min_rating": 4}})
        check("a rating floor eliminates the others with reasons",
              [item["service_id"] for item in strict["decision"]["results"]] == ["vendor-c-apple"] and len(strict["decision"]["eliminated"]) == 2)
        repeat = await call("mock_shop_rank_services", product_type="apple", seed="e2e")
        check("same seed reproduces the ranking", repeat["decision"] == quality["decision"])

        api = await call("mock_shop_rank_services", service_ids=["vendor-a-thailand-weather"])
        weather = api["decision"]["results"][0]["breakdown"]
        check("API reliability and latency are measured", weather["reliability"]["source"] == "measured"
              and weather["reliability"]["value"] == 90.0 and weather["speed"]["source"] == "measured",
              f"{weather['reliability']['value']}% / {weather['speed']['value']} ms")
        check("offer kind outside allowed_kinds is marked", api["decision"]["results"][0]["allowed_by_spending_policy"] is False)

        loosened = await call("mock_shop_configure_agent", max_per_transaction_minor=100000)
        check("raising a limit is refused", isinstance(loosened, str) and "Only a human" in loosened)
        check("limit on disk is unchanged", (await call("mock_shop_get_agent_config"))["config"]["spending"]["max_per_transaction_minor"] == 500)

        blocked = await call("mock_shop_purchase", items=[{"product_id": "vendor-c-apple", "quantity": 2}])
        check("purchase above the per-transaction limit is blocked", "exceeds the per-transaction limit" in blocked.get("error", ""))
        kind = await call("mock_shop_purchase", items=[{"product_id": "vendor-a-thailand-weather", "quantity": 1}])
        check("purchase of a disallowed kind is blocked", "Offer kind not allowed" in kind.get("error", ""))
        compute = await call("mock_shop_fund_compute", service_id="vendor-a-python-compute", amount_minor=100)
        check("compute funding of a disallowed kind is blocked", compute.get("policy", {}).get("approved") is False)

    await Agent(api_url, config_dir).run(steps)
    kinds = [entry["type"] for entry in decision_log(config_dir)]
    rejected = sum(1 for entry in decision_log(config_dir) if entry["type"] == "policy" and not entry["approved"])
    check("decision log records rankings, config changes and rejections",
          kinds.count("ranking") >= 5 and kinds.count("config") == 2 and rejected == 3,
          ", ".join(f"{name}={kinds.count(name)}" for name in sorted(set(kinds))))


async def payment(api_url: str, config_dir: Path, check: Checks) -> None:
    """Approved purchases on the backend's test payment path, enforced on both sides."""
    username, password = "e2e-payer", "e2e-password-2026"
    agent = Agent(api_url, config_dir, username, password)
    state = {}

    async def first_session(call):
        registered = await call("mock_shop_register", username=username, password=password)
        check("agent registered with a Solana keypair", isinstance(registered, dict) and registered["http_status"] == 201)
        wallet = await call("mock_shop_get_wallet")
        check("test payment path starts with 10.00 SOL mirrored, no airdrop",
              wallet["wallet"]["balance_minor"] == 1000 and wallet["wallet"]["currency"] == "SOL",
              str(wallet["wallet"].get("balance_minor")))

        configured = await call("mock_shop_configure_agent", purpose="E2E payer", max_per_transaction_minor=500, daily_budget_minor=600,
                                allowed_kinds=["product", "api-usage"], preferences={"profile": "cheapest"})
        sync = configured["marketplace_sync"]
        check("limits are copied to the marketplace account policy",
              sync["status"] == "synced" and sync["policy"]["daily_budget_minor"] == 600, sync["status"])
        ranked = await call("mock_shop_rank_services", product_type="apple", quantity=2, seed="pay")
        winner = ranked["decision"]["winner"]["service_id"]
        estimate = ranked["decision"]["results"][0]["offer"]["estimated_total"]["amount_minor"]
        check("ranking picks the cheapest apple and prices two units", (winner, estimate) == ("vendor-b-apple", 440), f"{winner} {estimate}")

        bought = await call("mock_shop_purchase", items=[{"product_id": winner, "quantity": 2}], idempotency_key="e2e-order-1")
        policy = bought.get("policy", {})
        check("purchase is approved and settled", bought.get("http_status") == 201 and policy.get("status") == "approved",
              json.dumps({key: bought.get(key) for key in ("http_status", "error")}))
        check("quoted total equals charged total", (policy.get("quoted_total_minor"), policy.get("charged_total_minor")) == (440, 440))
        purchase = bought["purchase"]
        state["purchase_id"] = purchase["id"]
        check("receipt is paid with a transaction signature", purchase["status"] == "paid" and bool(purchase.get("solana_tx_signature")))
        wallet = await call("mock_shop_get_wallet")
        check("wallet debited exactly once", wallet["wallet"]["balance_minor"] == 560, str(wallet["wallet"]["balance_minor"]))

        retry = await call("mock_shop_purchase", items=[{"product_id": winner, "quantity": 2}], idempotency_key="e2e-order-1")
        check("retry replays the same receipt without a second charge",
              retry.get("http_status") == 200 and retry.get("replayed") is True and retry["policy"]["status"] == "retry_of_approved_request")

        over = await call("mock_shop_purchase", items=[{"product_id": winner, "quantity": 1}])
        check("next purchase crossing the daily budget is blocked", "already spent today" in over.get("error", ""), over.get("error", "")[:90])
        small = await call("mock_shop_purchase", items=[{"product_id": "vendor-a-thailand-weather", "quantity": 1}], idempotency_key="e2e-order-2")
        check("small purchase inside the remaining budget is approved", small.get("http_status") == 201)

        cart = await call("mock_shop_set_cart_item", product_id=winner, quantity=1)
        blocked_cart = await call("mock_shop_checkout_cart", revision=cart["cart"]["revision"])
        check("checkout crossing the budget is blocked", "would exceed the daily budget" in blocked_cart.get("error", ""))

        rated = await call("mock_shop_evaluate_purchase", purchase_id=purchase["id"], rating=5)
        check("the buyer can rate the paid receipt", rated.get("http_status") == 200)
        after = await call("mock_shop_rank_services", service_ids=[winner], seed="pay")
        quality = after["decision"]["results"][0]["breakdown"]["quality"]
        check("the new rating reaches the ranking", quality["source"] == "agent_reviews" and quality["samples"] == 7,
              f"samples={quality['samples']}")
        config = await call("mock_shop_get_agent_config")
        check("the marketplace reports today's spend", config["marketplace_policy"]["spent_today_minor"] == 445,
              str(config["marketplace_policy"].get("spent_today_minor")))

    async def after_restart(call):
        retry = await call("mock_shop_purchase", items=[{"product_id": "vendor-b-apple", "quantity": 2}], idempotency_key="e2e-order-1")
        check("after an MCP restart a retry still replays the paid order",
              retry.get("http_status") == 200 and retry.get("replayed") is True, retry.get("error", "")[:90])
        ledger = await call("mock_shop_list_wallet_transactions")
        check("only approved purchases reached the ledger", debits(ledger) == [-5, -440], str(debits(ledger)))

    await agent.run(first_session)
    await agent.run(after_restart)

    # A client that skips the MCP server is still bound by the account policy.
    with httpx.Client(base_url=api_url, timeout=30) as client:
        token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
        direct = client.post("/api/purchases", json={"items": [{"product_id": "vendor-b-apple", "quantity": 2}]},
                             headers={"Authorization": "Bearer " + token, "Idempotency-Key": "e2e-direct-1"})
        check("a direct REST purchase over the budget is blocked by the marketplace",
              direct.status_code == 403 and "account spending policy" in direct.json().get("error", ""), str(direct.status_code))
        loosen = client.put("/api/agent/spending-policy", json={"daily_budget_minor": 100000}, headers={"Authorization": "Bearer " + token})
        check("the session alone cannot raise the marketplace limit", loosen.status_code == 403)
        feedback = client.get("/api/services/vendor-b-apple/feedback").json()
        own = [item for item in feedback["feedback"] if item["purchase_id"] == state.get("purchase_id")]
        check("the rating is exported as ERC-8004 feedback with proof of payment",
              len(own) == 1 and own[0]["value"] == 100 and own[0]["proofOfPayment"]["txHash"] is not None)

    statuses = [entry["status"] for entry in decision_log(config_dir) if entry["type"] == "policy"]
    check("decision log has every approval and rejection", statuses ==
          ["approved", "retry_of_approved_request", "rejected", "approved", "rejected", "retry_of_approved_request"], str(statuses))


async def chain(api_url: str, config_dir: Path, check: Checks) -> None:
    """One real Solana Devnet purchase (0.05 SOL + fee). Needs Devnet SOL on the agent address."""
    username, password = "e2e-chain-payer", "e2e-password-2026"

    async def steps(call):
        registered = await call("mock_shop_register", username=username, password=password)
        if isinstance(registered, str) and "HTTP 409" in registered:
            registered = await call("mock_shop_login", username=username, password=password)
        pubkey = registered["user"]["solana_pubkey"]
        print(f"agent address: {pubkey}", flush=True)
        wallet = await call("mock_shop_get_wallet")
        sol = wallet["wallet"].get("solana_balance_sol") or 0
        check("agent wallet holds Devnet SOL", sol >= 0.06, f"{sol} SOL")
        if sol < 0.06:
            print(f"Fund {pubkey} with 0.1 Devnet SOL (https://faucet.solana.com), then rerun with --database and --keep-database.")
            return
        await call("mock_shop_configure_agent", max_per_transaction_minor=10, daily_budget_minor=10, allowed_kinds=["api-usage"])
        bought = await call("mock_shop_purchase", items=[{"product_id": "vendor-a-thailand-weather", "quantity": 1}],
                            idempotency_key=f"e2e-chain-{datetime.now(timezone.utc):%Y%m%d%H%M%S}")
        signature = (bought.get("purchase") or {}).get("solana_tx_signature")
        check("on-chain purchase approved and paid", bought.get("http_status") == 201 and bool(signature), str(bought.get("error") or signature))
        if signature:
            print(f"explorer: https://explorer.solana.com/tx/{signature}?cluster=devnet", flush=True)
        blocked = await call("mock_shop_purchase", items=[{"product_id": "vendor-a-thailand-weather", "quantity": 3}])
        check("purchase over the limit is blocked before any transfer", "per-transaction limit" in blocked.get("error", ""))

    await Agent(api_url, config_dir, username, password).run(steps)


async def worker(api_url: str, config_dir: Path, check: Checks) -> None:
    """One machine consents to share its idle time; another agent rents it for a script and a written task.

    Needs Docker. Payments use the test path. The "agent" answering the task is a stand-in script, not an LLM.
    """
    seller_dir = config_dir / "seller"
    seller_dir.mkdir()
    stand_in = seller_dir / "stand_in_agent.py"
    stand_in.write_text("import sys\nprint('DONE: ' + sys.stdin.read().strip())\n", encoding="utf-8")
    seller = Agent(api_url, seller_dir, environment={
        "AUTARK_WORKER_POLL_SECONDS": "1", "AUTARK_WORKER_AGENT_COMMAND": json.dumps([sys.executable, str(stand_in)])})

    def worker_state():
        try:
            return json.loads((seller_dir / "worker-status.json").read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            return None

    async def share(call):
        created = await call("mock_shop_configure_agent", purpose="E2E machine that shares its idle time", share_when_idle=True,
                             share_modes=["script", "agent"], share_rate_minor_per_second=5)
        check("first configuration records the consent and starts the worker",
              created["config"]["sharing"]["enabled"] is True and created["sharing_status"]["worker"] == "starting",
              str(created["sharing_status"]["worker"]))
        # idle_after_seconds is a human setting; the test plays the human so it need not wait two minutes.
        config = json.loads((seller_dir / "agent-config.json").read_text(encoding="utf-8"))
        config["sharing"]["idle_after_seconds"] = 0
        (seller_dir / "agent-config.json").write_text(json.dumps(config), encoding="utf-8")
        # The seller's client stays open while the other agent rents the machine.
        await Agent(api_url, config_dir).run(lambda renter: rent(renter, call))

    async def revoke(call):
        off = await call("mock_shop_configure_agent", share_when_idle=False)
        check("the model can turn sharing off", off["config"]["sharing"]["enabled"] is False, str(off)[:200])
        again = await call("mock_shop_configure_agent", share_when_idle=True)
        check("but only a human can turn it back on", isinstance(again, str) and "Only a human" in again, str(again)[:120])

    async def rent(call, seller_call):
        await call("mock_shop_register", username="e2e-renter", password="e2e-password-2026")
        offers = {}
        for _ in range(240):  # the first start may pull the sandbox image
            listed = await call("mock_shop_list_services", kind="compute")
            offers = {service["execution"]: service for service in listed["services"] if service.get("worker_online")}
            if len(offers) == 2:
                break
            await asyncio.sleep(1)
        log = (seller_dir / "worker.log").read_text(encoding="utf-8")[-300:] if (seller_dir / "worker.log").exists() else "no worker log"
        check("the worker started on its own and published both offers as online", set(offers) == {"worker-script", "worker-agent"},
              log if len(offers) != 2 else "")
        if len(offers) != 2:
            return
        script, agent = offers["worker-script"]["id"], offers["worker-agent"]["id"]
        check("the offers hide how they are reached", all("url" not in offer and offer["price_minor"] == 5 for offer in offers.values()))
        ranked = await call("mock_shop_rank_services", service_ids=[script])
        check("ranking accepts an online worker", ranked["decision"]["winner"]["service_id"] == script)

        funded = await call("mock_shop_fund_compute", service_id=script, amount_minor=100)
        check("credit is bought for the script offer", funded.get("account", {}).get("balance_minor") == 100, str(funded.get("error") or ""))
        queued = await call("mock_shop_run_compute", service_id=script, source="print(6 * 7)")
        check("the script is queued, not run by the marketplace", (queued.get("http_status"), queued.get("run_status")) == (202, "queued"),
              str(queued.get("error") or ""))
        run = (await call("mock_shop_wait_compute_run", run_id=queued["run"]["id"], timeout_seconds=120))["run"]
        result = run.get("result") or {}
        check("the other machine ran it and the output came back as untrusted data",
              run["status"] == "completed" and (result.get("untrusted") or {}).get("stdout") == "42\n", str(run.get("error") or run["status"]))
        billed = float(run.get("elapsed_compute_seconds") or 0)
        check("billed per second by the marketplace clock, never more than the worker reported",
              run["charge_minor"] == math.ceil(billed * 5) and billed <= float(result.get("reported_elapsed_seconds", 0)) + 0.001,
              f"{billed} s -> {run['charge_minor']} minor")
        account = (await call("mock_shop_get_compute_account", service_id=script))["account"]
        check("credit went down by exactly the charge", account["balance_minor"] == 100 - run["charge_minor"], str(account["balance_minor"]))

        stuck = await call("mock_shop_run_compute", service_id=script, source="import time\nwhile True: time.sleep(1)")
        slow = (await call("mock_shop_wait_compute_run", run_id=stuck["run"]["id"], timeout_seconds=120))["run"]
        check("a script that never ends is stopped when the credit runs out",
              slow["status"] == "completed" and slow["result"]["timed_out"] is True, f"{slow['status']} after {slow.get('elapsed_compute_seconds')} s")
        left = (await call("mock_shop_get_compute_account", service_id=script))["account"]["balance_minor"]
        check("and the credit is never overdrawn", left >= 0 and slow["charge_minor"] <= account["balance_minor"], f"{left} left")

        await call("mock_shop_fund_compute", service_id=agent, amount_minor=200)
        refused = await call("mock_shop_run_compute", service_id=agent, source="print(1)")
        check("an agent offer does not take scripts", "written tasks only" in str(refused.get("error")), str(refused.get("error"))[:80])
        task = await call("mock_shop_submit_agent_task", service_id=agent, task="Count the vowels in 'marketplace'.")
        check("the written task is queued", (task.get("http_status"), task["run"]["status"], task["run"]["job_type"]) == (202, "queued", "task"))
        answered = (await call("mock_shop_wait_compute_run", run_id=task["run"]["id"], timeout_seconds=120))["run"]
        answer = ((answered.get("result") or {}).get("untrusted") or {}).get("output", "")
        check("the idle agent's answer comes back as untrusted data, billed per second",
              answered["status"] == "completed" and answer.strip() == "DONE: Count the vowels in 'marketplace'." and answered["charge_minor"] >= 1,
              f"{answer.strip()!r}, {answered['charge_minor']} minor")

        await revoke(seller_call)
        for _ in range(30):
            if worker_state() == "stopped":
                break
            await asyncio.sleep(1)
        check("the worker stops by itself once sharing is off", worker_state() == "stopped", str(worker_state()))
        before = (await call("mock_shop_get_compute_account", service_id=agent))["account"]["balance_minor"]
        orphan = await call("mock_shop_submit_agent_task", service_id=agent, task="Anyone there?")
        if isinstance(orphan, dict) and orphan.get("run", {}).get("status") == "queued":
            lost = (await call("mock_shop_wait_compute_run", run_id=orphan["run"]["id"], timeout_seconds=120))["run"]
            unpaid = lost["status"] == "failed" and lost["charge_minor"] == 0
        else:
            unpaid = "offline" in str(orphan)
        after = (await call("mock_shop_get_compute_account", service_id=agent))["account"]["balance_minor"]
        check("with the worker gone a job fails and nothing is charged", unpaid and after == before, f"{before} -> {after}")
        gone = None
        for _ in range(45):
            gone = (await call("mock_shop_get_service", service_id=script))["service"]["worker_online"]
            if gone is False:
                break
            await asyncio.sleep(1)
        check("the offer drops out of the catalog as offline", gone is False)
        ranked = await call("mock_shop_rank_services", service_ids=[script])
        check("ranking eliminates the offline worker",
              [reason["limit"] for item in ranked["decision"]["eliminated"] for reason in item["reasons"]] == ["worker_online"])

    try:
        await seller.run(share)
    finally:
        # Never leave the worker behind: without consent it exits within a poll.
        config = json.loads((seller_dir / "agent-config.json").read_text(encoding="utf-8"))
        config["sharing"]["enabled"] = False
        (seller_dir / "agent-config.json").write_text(json.dumps(config), encoding="utf-8")
        for _ in range(30):
            if worker_state() in (None, "stopped"):
                break
            await asyncio.sleep(1)


SCENARIOS = {"decision": decision, "payment": payment, "chain": chain, "worker": worker}
