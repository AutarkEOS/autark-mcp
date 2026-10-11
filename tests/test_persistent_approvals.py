"""Approvals survive a restart of the MCP server."""

import json
from datetime import datetime, timedelta, timezone

from support import ConfigDirectoryTestCase, FakeSession, ledger, service

import policy


class PersistentApprovalTests(ConfigDirectoryTestCase):
    async def asyncSetUp(self):
        self.session = FakeSession()
        self.session.on("GET", "/api/services/apple", service("apple", 100))
        self.session.on("GET", "/api/wallet/transactions", ledger())
        self.write_config(spending={"daily_budget_minor": 200})

    async def authorize(self, key="order-1", quantity=2):
        lines = [{"product_id": "apple", "quantity": quantity}]
        return await policy.authorize(self.session, action="purchase", idempotency_key=key, body={"items": lines},
                                      quote=lambda: policy.quote_items(self.session, lines))

    async def test_an_approval_survives_a_restart(self):
        await self.authorize()
        stored = json.loads(policy.approvals_path().read_text(encoding="utf-8"))
        self.assertEqual(set(stored), {"order-1"})
        # Nothing is kept in memory: a new process reads the same file.
        now = datetime.now(timezone.utc).isoformat()
        self.session.on("GET", "/api/wallet/transactions", ledger((now, -200)))
        self.assertEqual((await self.authorize())["status"], "retry_of_approved_request")

    async def test_ranking_seed_does_not_change_the_retry_fingerprint(self):
        lines = [{"product_id": "apple", "quantity": 2}]
        await policy.authorize(
            self.session, action="purchase", idempotency_key="order-1",
            body={"items": lines, "ranking_seed": "pay"},
            quote=lambda: policy.quote_items(self.session, lines),
        )
        now = datetime.now(timezone.utc).isoformat()
        self.session.on("GET", "/api/wallet/transactions", ledger((now, -200)))
        retry = await policy.authorize(
            self.session, action="purchase", idempotency_key="order-1",
            body={"items": lines},
            quote=lambda: policy.quote_items(self.session, lines),
        )
        self.assertEqual(retry["status"], "retry_of_approved_request")

    async def test_old_approvals_are_pruned_and_a_damaged_file_means_checking_again(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        policy.approvals_path().write_text(json.dumps({"ancient": {"fingerprint": "x", "at": stale}, "junk": 5}), encoding="utf-8")
        await self.authorize()
        self.assertEqual(set(json.loads(policy.approvals_path().read_text(encoding="utf-8"))), {"order-1"})
        policy.approvals_path().write_text("{damaged", encoding="utf-8")
        now = datetime.now(timezone.utc).isoformat()
        self.session.on("GET", "/api/wallet/transactions", ledger((now, -200)))
        with self.assertRaises(policy.PolicyRejection):
            await self.authorize()
