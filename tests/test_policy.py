from datetime import datetime, timedelta, timezone

from support import ConfigDirectoryTestCase, FakeSession, ledger, service

import policy
from client import MockShopApiError
from policy import PolicyRejection

NOW = datetime.now(timezone.utc)
MIDNIGHT = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
TODAY = MIDNIGHT.isoformat()
YESTERDAY = (MIDNIGHT - timedelta(microseconds=1)).isoformat()
LINES = [{"product_id": "apple", "quantity": 2}]


class PolicyTests(ConfigDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        self.session.on("GET", "/api/services/apple", service("apple", 100))
        self.session.on("GET", "/api/wallet/transactions", ledger())

    async def authorize(self, lines=None, key="key-1", action="purchase"):
        lines = LINES if lines is None else lines
        return await policy.authorize(self.session, action=action, idempotency_key=key, body={"items": lines},
                                      quote=lambda: policy.quote_items(self.session, lines))

    async def rejected(self, **kwargs):
        with self.assertRaises(PolicyRejection) as raised:
            await self.authorize(**kwargs)
        return raised.exception

    def limits(self, **spending):
        self.write_config(spending=spending)

    # --- no config / broken config --------------------------------------

    async def test_without_a_config_nothing_is_limited_or_fetched(self):
        verdict = await self.authorize()
        self.assertEqual((verdict["approved"], verdict["status"]), (True, "no_config"))
        self.assertEqual(self.session.calls, [])

    async def test_unreadable_config_blocks_spending(self):
        self.config_file.write_text("{broken", encoding="utf-8")
        rejection = await self.rejected()
        self.assertIn("unreadable", rejection.reason)
        self.assertEqual(self.session.calls, [])

    # --- per-transaction limit ------------------------------------------

    async def test_per_transaction_limit_is_inclusive(self):
        self.limits(max_per_transaction_minor=200)
        verdict = await self.authorize()
        self.assertEqual((verdict["approved"], verdict["quoted_total_minor"]), (True, 200))
        self.limits(max_per_transaction_minor=199)
        rejection = await self.rejected(key="key-2")
        self.assertEqual(rejection.reason, "Total 200 minor exceeds the per-transaction limit of 199 minor.")
        self.assertEqual(rejection.details["approved"], False)

    async def test_quote_uses_catalog_prices_and_sums_duplicate_lines(self):
        self.session.on("GET", "/api/services/pear", service("pear", 7))
        quote = await policy.quote_items(self.session, [{"product_id": "apple", "quantity": 1}, {"product_id": "pear", "quantity": 3},
                                                       {"product_id": "apple", "quantity": 4, "vendor_id": "vendor-a"}])
        self.assertEqual(quote["total_minor"], 521)
        self.assertEqual(quote["lines"][0], {"product_id": "apple", "quantity": 5, "unit_price_minor": 100, "kind": "product"})
        self.assertEqual(len(self.session.made("GET", "/api/services/apple")), 1)

    async def test_unverifiable_price_is_a_rejection(self):
        self.limits(max_per_transaction_minor=10_000)
        failures = [MockShopApiError(404, "Product does not exist."), MockShopApiError(None, "timed out"), {"service": {"kind": "product"}},
                    {"service": {"price_minor": "100"}}, {"service": {"price_minor": -5}}, {"nothing": True}, RuntimeError("boom")]
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                self.session.on("GET", "/api/services/apple", failure)
                rejection = await self.rejected(key=f"bad-{index}")
                self.assertIn("price", rejection.reason)

    async def test_zero_limit_blocks_everything(self):
        self.limits(max_per_transaction_minor=0)
        await self.rejected()

    # --- allowed kinds ---------------------------------------------------

    async def test_allowed_kinds(self):
        self.session.on("GET", "/api/services/gpu", service("gpu", 10, kind="compute"))
        self.limits(allowed_kinds=["product"])
        self.assertTrue((await self.authorize())["approved"])
        mixed = [{"product_id": "apple", "quantity": 1}, {"product_id": "gpu", "quantity": 1}]
        rejection = await self.rejected(lines=mixed, key="key-2")
        self.assertIn("compute", rejection.reason)
        self.limits(allowed_kinds=[])
        self.assertIn("Allowed: none", (await self.rejected(key="key-3")).reason)

    # --- daily budget ----------------------------------------------------

    async def test_daily_budget_counts_only_todays_debits(self):
        self.session.on("GET", "/api/wallet/transactions", ledger(
            (NOW.isoformat(), -300), (NOW.isoformat(), 5000), (TODAY, -200), (YESTERDAY, -9999)))
        self.assertEqual(await policy.spent_today(self.session, NOW), 500)
        self.limits(daily_budget_minor=700)
        verdict = await self.authorize()
        self.assertEqual((verdict["spent_today_minor"], verdict["daily_budget_minor"], verdict["approved"]), (500, 700, True))
        self.limits(daily_budget_minor=699)
        rejection = await self.rejected(key="key-2")
        self.assertIn("500 of 699 minor already spent today", rejection.reason)

    async def test_empty_ledger_and_zero_budget(self):
        self.limits(daily_budget_minor=200)
        self.assertEqual((await self.authorize())["spent_today_minor"], 0)
        self.limits(daily_budget_minor=0)
        await self.rejected(key="key-2")

    async def test_daily_budget_reads_every_page_of_today(self):
        pages = {0: [(NOW.isoformat(), -1)] * 100, 100: [(NOW.isoformat(), -1)] * 100, 200: [(NOW.isoformat(), -1)] * 3 + [(YESTERDAY, -50)]}
        self.session.on("GET", "/api/wallet/transactions", lambda call: ledger(*pages[call["params"]["offset"]]))
        self.assertEqual(await policy.spent_today(self.session, NOW), 203)
        self.assertEqual([call["params"]["offset"] for call in self.session.made("GET", "/api/wallet/transactions")], [0, 100, 200])
        self.assertTrue(all(call["auth"] for call in self.session.made("GET", "/api/wallet/transactions")))

    async def test_unbounded_ledger_is_a_rejection(self):
        self.session.on("GET", "/api/wallet/transactions", lambda call: ledger(*[(NOW.isoformat(), -1)] * 100))
        with self.assertRaises(PolicyRejection):
            await policy.spent_today(self.session, NOW)
        self.assertEqual(len(self.session.calls), policy.MAX_LEDGER_PAGES)

    async def test_unverifiable_ledger_is_a_rejection(self):
        self.limits(daily_budget_minor=10_000)
        failures = [MockShopApiError(401, "Not signed in."), {"transactions": "none"}, {"transactions": [{"amount_minor": -1}]},
                    {"transactions": [{"created_at": "yesterday", "amount_minor": -1}]},
                    {"transactions": [{"created_at": "2026-10-09T10:00:00", "amount_minor": -1}]},
                    {"transactions": [{"created_at": NOW.isoformat(), "amount_minor": "-1"}]}, RuntimeError("boom")]
        for index, failure in enumerate(failures):
            with self.subTest(failure=failure):
                self.session.on("GET", "/api/wallet/transactions", failure)
                await self.rejected(key=f"bad-{index}")

    async def test_ledger_is_not_read_without_a_daily_budget(self):
        self.limits(max_per_transaction_minor=1000)
        await self.authorize()
        self.assertEqual(self.session.made("GET", "/api/wallet/transactions"), [])

    # --- retries ---------------------------------------------------------

    async def test_retry_with_the_same_key_and_body_skips_the_budget(self):
        self.limits(daily_budget_minor=200)
        await self.authorize()
        # The first attempt has now used the whole budget.
        self.session.on("GET", "/api/wallet/transactions", ledger((NOW.isoformat(), -200)))
        calls = len(self.session.calls)
        retry = await self.authorize()
        self.assertEqual((retry["approved"], retry["status"]), (True, "retry_of_approved_request"))
        self.assertEqual(len(self.session.calls), calls)
        # A new key, or the same key with another body, is a new payment and is checked again.
        await self.rejected(key="key-2")
        await self.rejected(lines=[{"product_id": "apple", "quantity": 1}])

    async def test_a_rejected_request_is_not_remembered_as_approved(self):
        self.limits(max_per_transaction_minor=1)
        await self.rejected()
        await self.rejected()

    # --- cart and compute ------------------------------------------------

    async def test_cart_quote(self):
        self.session.on("GET", "/api/cart", {"cart": {"revision": 4, "total_minor": 640, "items": [
            {"product_id": "apple", "quantity": 2, "unit_price_minor": 220, "kind": "product"},
            {"product_id": "weather", "quantity": 4, "unit_price_minor": 50, "kind": "api-usage"}]}})
        quote = await policy.quote_cart(self.session, 4)
        self.assertEqual((quote["total_minor"], quote["kinds"]), (640, ["api-usage", "product"]))
        with self.assertRaises(PolicyRejection) as stale:
            await policy.quote_cart(self.session, 3)
        self.assertIn("revision 4, not 3", stale.exception.reason)
        self.session.on("GET", "/api/cart", {"cart": {"revision": 4, "total_minor": None, "items": []}})
        with self.assertRaises(PolicyRejection):
            await policy.quote_cart(self.session, 4)

    async def test_compute_funding_amount(self):
        self.limits(max_per_transaction_minor=1000, allowed_kinds=["compute"])
        verdict = await policy.authorize(self.session, action="fund_compute", idempotency_key="fund-1",
                                         body={"service_id": "gpu", "amount_minor": 1000},
                                         quote=lambda: policy.quote_amount(1000, "compute"))
        self.assertEqual((verdict["approved"], verdict["kinds"]), (True, ["compute"]))
        with self.assertRaises(PolicyRejection):
            await policy.authorize(self.session, action="fund_compute", idempotency_key="fund-2",
                                   body={"service_id": "gpu", "amount_minor": 1001},
                                   quote=lambda: policy.quote_amount(1001, "compute"))

    # --- audit -----------------------------------------------------------

    async def test_every_decision_is_logged_without_secrets(self):
        self.limits(max_per_transaction_minor=200)
        await self.authorize()
        self.limits(max_per_transaction_minor=10)
        await self.rejected(key="key-2")
        entries = self.audit()
        self.assertEqual([(entry["status"], entry["approved"], entry["idempotency_key"]) for entry in entries],
                         [("approved", True, "key-1"), ("rejected", False, "key-2")])
        self.assertEqual(entries[1]["reason"], "Total 200 minor exceeds the per-transaction limit of 10 minor.")
        self.assertEqual(entries[0]["lines"], [{"product_id": "apple", "quantity": 2, "unit_price_minor": 100, "kind": "product"}])
        self.assertNotIn("test-token", self.audit_file.read_text(encoding="utf-8"))
        self.assertTrue(all(datetime.fromisoformat(entry["at"]).tzinfo for entry in entries))

    async def test_spending_is_blocked_when_the_decision_cannot_be_logged(self):
        import os
        blocker = self.directory / "not-a-directory"
        blocker.write_text("file", encoding="utf-8")
        os.environ["AUTARK_DECISION_LOG"] = str(blocker / "decisions.jsonl")
        self.limits(max_per_transaction_minor=1000)
        rejection = await self.rejected()
        self.assertIn("could not be recorded", rejection.reason)
        # The failed attempt must not count as an approval for a later retry.
        await self.rejected()

    def test_rejection_payload_tells_the_model_nothing_was_spent(self):
        payload = policy.rejection_payload(PolicyRejection("too much", {"status": "rejected"}), "key-9")
        self.assertEqual((payload["error"], payload["policy"], payload["idempotency_key"]),
                         ("Blocked by the agent spending policy: too much", {"approved": False, "status": "rejected"}, "key-9"))
        self.assertIn("Nothing was spent", payload["hint"])
        self.assertIn(str(self.config_file), payload["hint"])

    def test_fingerprint_depends_on_action_and_body(self):
        base = policy.fingerprint("purchase", {"items": LINES})
        self.assertEqual(base, policy.fingerprint("purchase", {"items": [{"quantity": 2, "product_id": "apple"}]}))
        self.assertNotEqual(base, policy.fingerprint("checkout_cart", {"items": LINES}))
        self.assertNotEqual(base, policy.fingerprint("purchase", {"items": [{"product_id": "apple", "quantity": 3}]}))
