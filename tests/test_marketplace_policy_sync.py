"""The MCP server mirrors local limits into the marketplace account policy."""

import json
import os
from unittest.mock import patch

from support import ConfigDirectoryTestCase, FakeSession, service

import server
from client import MockShopApiError

POLICY_PATH = "/api/agent/spending-policy"
LOOSEN = MockShopApiError(403, "Raising or removing a limit (max_per_transaction_minor) requires current_password: "
                               "only the account owner can loosen spending limits.")


class MarketplaceSyncTests(ConfigDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.session.on("PUT", POLICY_PATH, lambda call: {"policy": {**call["json_body"], "revision": 1}})
        self.session.on("GET", POLICY_PATH, {"policy": {"max_per_transaction_minor": 500, "spent_today_minor": 40}})
        self.session.on("GET", "/api/auth/me", {"user": {"username": "owner-agent"}})
        patcher = patch.object(server, "session", lambda: self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        previous = {name: os.environ.get(name) for name in ("MOCK_SHOP_USERNAME", "MOCK_SHOP_PASSWORD")}
        for name in previous:
            os.environ.pop(name, None)

        def restore():
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)

    async def call(self, name, **arguments):
        text = (await server.mcp.call_tool(name, arguments)).content[0].text
        return text if text.startswith("Error:") else json.loads(text)

    def puts(self):
        return [call["json_body"] for call in self.session.made("PUT", POLICY_PATH)]

    async def test_configuring_limits_copies_them_to_the_marketplace(self):
        created = await self.call("mock_shop_configure_agent", max_per_transaction_minor=500, allowed_kinds=["product"])
        self.assertEqual(created["marketplace_sync"]["status"], "synced")
        self.assertEqual(self.puts(), [{"max_per_transaction_minor": 500, "daily_budget_minor": None, "allowed_kinds": ["product"]}])
        await self.call("mock_shop_configure_agent", purpose="only the purpose changes")
        self.assertEqual(len(self.puts()), 1, "a change without limits does not touch the marketplace")
        await self.call("mock_shop_configure_agent", daily_budget_minor=900)
        self.assertEqual(self.puts()[-1]["daily_budget_minor"], 900)
        self.assertNotIn("current_password", json.dumps(self.puts()))
        self.assertEqual(self.audit()[-1]["marketplace_sync"]["status"], "synced")

    async def test_sync_outcomes_are_reported_not_fatal(self):
        for failure, status in ((MockShopApiError(401, "Not signed in."), "not_signed_in"), (LOOSEN, "marketplace_stricter"),
                                (MockShopApiError(None, "timed out"), "failed")):
            with self.subTest(status=status):
                self.config_file.unlink(missing_ok=True)
                self.session.on("PUT", POLICY_PATH, failure)
                created = await self.call("mock_shop_configure_agent", max_per_transaction_minor=500)
                self.assertEqual((created["configured"], created["marketplace_sync"]["status"]), (True, status))

    async def test_get_agent_config_shows_the_marketplace_policy(self):
        await self.call("mock_shop_configure_agent", purpose="x")
        read = await self.call("mock_shop_get_agent_config")
        self.assertEqual(read["marketplace_policy"], {"max_per_transaction_minor": 500, "spent_today_minor": 40})
        self.session.on("GET", POLICY_PATH, MockShopApiError(401, "Not signed in."))
        self.assertIn("Not signed in", (await self.call("mock_shop_get_agent_config"))["marketplace_policy"]["error"])

    async def test_human_edit_is_synced_and_loosening_uses_only_the_owner_env_password(self):
        self.write_config(spending={"max_per_transaction_minor": 5000})
        attempts = []

        def put(call):
            attempts.append(call["json_body"])
            if "current_password" not in call["json_body"]:
                return LOOSEN
            # Like the real endpoint, the response never echoes the password.
            return {"policy": {k: v for k, v in call["json_body"].items() if k != "current_password"}}

        self.session.on("PUT", POLICY_PATH, put)
        # Without env credentials there is no owner password: the marketplace stays stricter.
        result = await self.call("mock_shop_sync_spending_policy")
        self.assertEqual(result["marketplace_sync"]["status"], "marketplace_stricter")
        # Env credentials of another account are never sent.
        os.environ.update(MOCK_SHOP_USERNAME="someone-else", MOCK_SHOP_PASSWORD="their-password")
        self.assertEqual((await self.call("mock_shop_sync_spending_policy"))["marketplace_sync"]["status"], "marketplace_stricter")
        self.assertFalse(any("current_password" in body for body in attempts))
        os.environ.update(MOCK_SHOP_USERNAME="Owner-Agent", MOCK_SHOP_PASSWORD="owner-password")
        synced = await self.call("mock_shop_sync_spending_policy")
        self.assertEqual((synced["marketplace_sync"]["status"], synced["marketplace_sync"]["loosened_with_owner_password"]), ("synced", True))
        self.assertEqual(attempts[-1]["current_password"], "owner-password")
        # The password never reaches the model or the decision log.
        self.assertNotIn("owner-password", json.dumps(synced))
        self.assertNotIn("owner-password", self.audit_file.read_text(encoding="utf-8"))

    async def test_configure_never_loosens_with_the_owner_password(self):
        os.environ.update(MOCK_SHOP_USERNAME="owner-agent", MOCK_SHOP_PASSWORD="owner-password")
        self.session.on("PUT", POLICY_PATH, LOOSEN)
        created = await self.call("mock_shop_configure_agent", max_per_transaction_minor=5000)
        self.assertEqual(created["marketplace_sync"]["status"], "marketplace_stricter")
        self.assertEqual(self.session.made("GET", "/api/auth/me"), [])

    async def test_sync_requires_a_config(self):
        self.assertIn("No agent config exists", await self.call("mock_shop_sync_spending_policy"))

    async def test_marketplace_rejection_is_explained(self):
        self.session.on("GET", "/api/services/apple", service("apple", 100))
        self.session.on("POST", "/api/purchases", MockShopApiError(
            403, "Blocked by the account spending policy: total 200 minor exceeds the per-transaction limit of 100 minor."))
        answer = await self.call("mock_shop_purchase", items=[{"product_id": "apple", "quantity": 2}])
        self.assertIn("HTTP 403: Blocked by the account spending policy", answer["error"])
        self.assertIn("Nothing was spent", answer["hint"])
        self.assertRegex(answer["idempotency_key"], r"^mcp-")
        self.assertIn("new key is a new payment", answer["hint"].lower())
