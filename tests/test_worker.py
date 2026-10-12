"""The idle worker: consent, idleness, the vendor account, and running other agents' jobs."""

import base64
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

import keyring
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from keyring.backends import fail

from support import ConfigDirectoryTestCase, FakeSession

import settings
import vault
import worker
from client import MockShopApiError


def unbase58(text: str) -> bytes:
    number = 0
    for char in text:
        number = number * 58 + vault._ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + body

SHARING = {"enabled": True, "modes": ["script"], "rate_minor_per_second": 5, "idle_after_seconds": 0}
OFFER = {"id": "service-1", "price_minor": 5, "revision": 0}


class WorkerSession(FakeSession):
    def _store_token(self, body):
        self.token = body["token"]


class WorkerLoopTests(ConfigDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.session = WorkerSession()
        self.session.on("POST", "/api/auth/register", (201, {"token": "vendor-token", "user": {}}))
        self.session.on("GET", "/api/vendor/services", {"services": []})
        self.session.on("POST", "/api/vendor/services", (201, {"service": OFFER}))
        self.session.on("POST", "/api/worker/service-1/claim", (204, {}))
        for patcher in (patch.object(worker, "Session", lambda: self.session), patch.object(worker, "docker_ready", AsyncMock(return_value=True)),
                        patch.object(worker, "log", lambda message: None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self):
        return json.loads((self.directory / "worker-status.json").read_text(encoding="utf-8"))

    def saved(self):
        return json.loads((self.directory / "worker.json").read_text(encoding="utf-8"))[settings.api_base_url()]

    def secret(self):
        return vault.load(settings.api_base_url())

    async def test_without_consent_the_worker_stops_and_contacts_nobody(self):
        for config in (None, {"sharing": {"enabled": False}}):
            with self.subTest(config=config):
                if config:
                    self.write_config(**config)
                self.assertEqual(await worker.Worker().run(once=True), 0)
                self.assertEqual((self.status()["state"], self.session.calls), ("stopped", []))
                self.assertIsNone(worker.read_status())

    async def test_a_broken_config_shares_nothing(self):
        self.config_file.write_text("{broken", encoding="utf-8")
        await worker.Worker().run(once=True)
        self.assertEqual(self.session.calls, [])

    async def test_first_start_registers_a_vendor_and_publishes_the_offer(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        registered = self.session.made("POST", "/api/auth/register")[0]["json_body"]
        published = self.session.made("POST", "/api/vendor/services")[0]
        self.assertEqual((registered["role"], published["json_body"]["kind"], published["json_body"]["url"], published["json_body"]["price_minor"]),
                         ("vendor", "compute", "worker:script", 5))
        self.assertEqual((self.status()["state"], self.status()["offers"]), ("idle", {"script": "service-1"}))
        self.assertEqual(len(self.session.made("POST", "/api/worker/service-1/claim")), 1)

    async def test_the_login_and_payout_key_live_in_the_credential_store_not_in_files(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        registered = self.session.made("POST", "/api/auth/register")[0]["json_body"]
        secret = self.secret()
        self.assertEqual((secret["username"], secret["password"]), (registered["username"], registered["password"]))
        # The marketplace gets the address and a proof the key is ours, never the key.
        address = vault.payout_address(secret["payout_seed"])
        self.assertEqual(registered["payout_pubkey"], address)
        Ed25519PublicKey.from_public_bytes(unbase58(address)).verify(
            unbase58(registered["payout_signature"]), f"Autark payout address for {registered['username']}".encode())
        self.assertNotIn(secret["payout_seed"], json.dumps(self.session.calls))
        self.assertEqual(self.saved(), {"username": registered["username"], "payout_address": address, "offers": {"script": "service-1"}})
        for file in self.directory.iterdir():
            text = file.read_text(encoding="utf-8")
            self.assertNotIn(secret["password"], text, file.name)
            self.assertNotIn(secret["payout_seed"], text, file.name)
        self.assertEqual(self.status()["payout_address"], address)

    async def test_a_password_left_in_worker_json_moves_to_the_credential_store(self):
        self.write_config(sharing=SHARING)
        worker.write_json(self.directory / "worker.json", {settings.api_base_url(): {
            "username": "worker-old-1", "password": "old-password-123", "offers": {"script": "service-1"}}})
        self.session.on("POST", "/api/auth/login", {"token": "vendor-token", "user": {}})
        self.session.on("GET", "/api/vendor/services", {"services": [OFFER]})
        await worker.Worker().run(once=True)
        self.assertEqual(self.session.made("POST", "/api/auth/login")[0]["json_body"], {"username": "worker-old-1", "password": "old-password-123"})
        self.assertEqual(self.secret(), {"username": "worker-old-1", "password": "old-password-123"})
        self.assertEqual(self.saved(), {"username": "worker-old-1", "offers": {"script": "service-1"}})
        self.assertNotIn("old-password-123", (self.directory / "worker.json").read_text(encoding="utf-8"))
        self.assertEqual(self.session.made("POST", "/api/auth/register"), [])

    async def test_without_a_credential_store_the_worker_does_not_start(self):
        self.write_config(sharing=SHARING)
        keyring.set_keyring(fail.Keyring())
        self.assertEqual(await worker.Worker().run(once=True), 1)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.status()["state"], "stopped")
        self.assertIn("credential store", self.status()["last_error"])
        self.assertFalse((self.directory / "worker.json").exists())

    async def test_a_new_account_keeps_the_key_an_earlier_one_was_paid_to(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        first = self.secret()
        self.session.on("POST", "/api/auth/login", MockShopApiError(403, "This account was deactivated."))
        (self.directory / "worker-status.json").unlink()
        await worker.Worker().run(once=True)
        second = self.secret()
        self.assertNotEqual(second["username"], first["username"])
        self.assertEqual(second["retired_payout_seeds"], [first["payout_seed"]])
        self.assertNotEqual(second["payout_seed"], first["payout_seed"])

    async def test_the_payout_key_can_be_exported_for_a_wallet(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        output = io.StringIO()
        with patch.object(sys, "argv", ["worker.py", "--export-payout-key"]), redirect_stdout(output):
            self.assertEqual(worker.main(), 0)
        keypair = json.loads(output.getvalue())
        self.assertEqual((len(keypair), vault.base58(bytes(keypair[32:]))), (64, self.saved()["payout_address"]))

    async def test_turning_sharing_off_withdraws_the_offers(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        self.session.on("POST", "/api/auth/login", {"token": "vendor-token", "user": {}})
        self.session.on("DELETE", "/api/vendor/services/service-1", {"service": {**OFFER, "archived_at": "now"}})
        self.write_config(sharing={**SHARING, "enabled": False})
        (self.directory / "worker-status.json").unlink()
        self.assertEqual(await worker.Worker().run(once=True), 0)
        self.assertEqual(len(self.session.made("DELETE", "/api/vendor/services/service-1")), 1)
        self.assertEqual(self.saved()["offers"], {})
        self.assertEqual(self.status()["state"], "stopped")

    async def test_a_mode_no_longer_shared_is_withdrawn_and_a_withdrawn_offer_replaced(self):
        self.write_config(sharing={**SHARING, "modes": ["agent", "script"]})
        self.session.on("POST", "/api/vendor/services", lambda call: (201, {"service": {
            **OFFER, "id": "service-agent" if call["json_body"]["url"] == "worker:agent" else "service-1"}}))
        self.session.on("POST", "/api/worker/service-agent/claim", (204, {}))
        with patch.dict(os.environ, {"AUTARK_WORKER_AGENT_COMMAND": "agent"}):
            await worker.Worker().run(once=True)
        self.assertEqual(self.saved()["offers"], {"agent": "service-agent", "script": "service-1"})
        self.session.on("POST", "/api/auth/login", {"token": "vendor-token", "user": {}})
        self.session.on("GET", "/api/vendor/services", {"services": [{**OFFER, "archived_at": "2026-10-12T10:00:00+00:00"},
                                                                      {**OFFER, "id": "service-agent"}]})
        self.session.on("DELETE", "/api/vendor/services/service-agent", {"service": {}})
        self.session.on("POST", "/api/vendor/services", (201, {"service": {**OFFER, "id": "service-2"}}))
        self.session.on("POST", "/api/worker/service-2/claim", (204, {}))
        self.write_config(sharing=SHARING)
        (self.directory / "worker-status.json").unlink()
        await worker.Worker().run(once=True)
        self.assertEqual(len(self.session.made("DELETE", "/api/vendor/services/service-agent")), 1)
        self.assertEqual(self.saved()["offers"], {"script": "service-2"})

    async def test_next_start_reuses_the_account_and_reprices_the_offer(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        username = self.secret()["username"]
        self.session.on("POST", "/api/auth/login", {"token": "vendor-token-2", "user": {}})
        self.session.on("GET", "/api/vendor/services", {"services": [OFFER]})
        self.session.on("PATCH", "/api/vendor/services/service-1", {"service": {**OFFER, "price_minor": 8, "revision": 1}})
        self.write_config(sharing={**SHARING, "rate_minor_per_second": 8})
        (self.directory / "worker-status.json").unlink()
        await worker.Worker().run(once=True)
        self.assertEqual((len(self.session.made("POST", "/api/auth/register")), len(self.session.made("POST", "/api/vendor/services"))), (1, 1))
        self.assertEqual(self.session.made("PATCH", "/api/vendor/services/service-1")[0]["json_body"], {"revision": 0, "price_minor": 8})
        self.assertEqual(self.session.made("POST", "/api/auth/login")[0]["json_body"]["username"], username)

    async def test_an_active_agent_is_not_offered(self):
        self.write_config(sharing={**SHARING, "idle_after_seconds": 120})
        (self.directory / "last-activity").touch()
        await worker.Worker().run(once=True)
        self.assertEqual((self.session.made("POST", "/api/worker/service-1/claim"), self.status()["state"]), ([], "agent-active"))

    async def test_a_claimed_script_is_run_and_its_result_returned(self):
        self.write_config(sharing=SHARING)
        job = {"run_id": "run-1", "job_type": "script", "payload_base64": base64.b64encode(b"print(2+2)").decode(), "max_compute_seconds": "7.5"}
        self.session.on("POST", "/api/worker/service-1/claim", (200, {"job": job}))
        self.session.on("POST", "/api/worker/runs/run-1/result", {"run": {"billed_seconds": "0.2", "charge_minor": 1}})
        result = {"stdout": "4\n", "stderr": "", "exit_code": 0, "timed_out": False, "output_truncated": False, "elapsed_compute_seconds": "0.200"}
        with patch.object(worker, "run_script", AsyncMock(return_value=result)) as run_script:
            await worker.Worker().run(once=True)
        run_script.assert_awaited_once_with(b"print(2+2)", 7.5)
        self.assertEqual(self.session.made("POST", "/api/worker/runs/run-1/result")[0]["json_body"], result)

    async def logged_task(self, result_response, output="Silicon dawn breaks"):
        """Run one agent task through the loop and return what the worker logged."""
        self.write_config(sharing={**SHARING, "modes": ["agent"]})
        job = {"run_id": "run-1", "job_type": "task", "payload_base64": base64.b64encode(b"Write a haiku.").decode(),
               "max_compute_seconds": "300"}
        self.session.on("POST", "/api/worker/service-1/claim", (200, {"job": job}))
        self.session.on("POST", "/api/worker/runs/run-1/result", result_response)
        self.session.on("GET", "/api/wallet", {"wallet": {"solana_pubkey": "Payout111", "balance_minor": 30000, "solana_balance_sol": 0.03}})
        result = {"output": output, "exit_code": 0, "timed_out": False, "output_truncated": False, "elapsed_compute_seconds": "35.479"}
        messages = []
        with patch.dict(os.environ, {"AUTARK_WORKER_AGENT_COMMAND": '["claude", "-p"]'}), \
                patch.object(worker, "run_task", AsyncMock(return_value=result)), patch.object(worker, "log", messages.append):
            await worker.Worker().run(once=True)
        return "\n".join(messages)

    async def test_a_task_is_logged_from_the_prompt_to_the_payment(self):
        text = await self.logged_task({"run": {"status": "completed", "billed_seconds": "35.479", "charge_minor": 3548}, "replayed": False})
        expected = ["Job run-1 (task), up to 300 s.", "Prompt from the buyer, untrusted (14 characters):\n    | Write a haiku.",
                    "Running the agent command claude", "Finished in 35.479 s, exit code 0.",
                    "Answer sent to the buyer (19 characters):\n    | Silicon dawn breaks", "Sending the result of run-1",
                    "Job run-1 returned: billed 35.479 s, 3548 minor.", "Payment: 3548 minor (0.003548 SOL) taken from the buyer's prepaid credit",
                    "This session: 1 paid job(s), 3548 minor (0.003548 SOL) billed.",
                    "Payout address Payout111: 30000 minor in sales so far, 0.03 SOL on-chain.", "Waiting for the next job."]
        positions = [text.find(line) for line in expected]
        self.assertNotIn(-1, positions, text)
        self.assertEqual(positions, sorted(positions))

    async def test_a_rejected_result_is_logged_as_not_paid(self):
        text = await self.logged_task(MockShopApiError(409, "This job is closed: it is no longer running."), output="")
        self.assertIn("Result of run-1 was not accepted: This job is closed", text)
        self.assertIn("Not paid: a result the marketplace does not accept is not charged to the buyer.", text)
        self.assertIn("    | (empty)", text)
        self.assertEqual(self.session.made("GET", "/api/wallet"), [])

    async def test_each_new_funding_is_logged_once(self):
        sales = [{"id": "sale-1", "kind": "sale", "amount_minor": 1000, "balance_after_minor": 1000}]
        self.session.on("GET", "/api/wallet/transactions", lambda call: {"transactions": list(sales), "total": len(sales)})
        self.session.on("GET", "/api/wallet", {"wallet": {"solana_pubkey": "Payout111", "balance_minor": 31000, "solana_balance_sol": 0.031}})
        runner, messages = worker.Worker(), []
        with patch.object(worker, "log", messages.append):
            await runner.watch_sales()
            sales.insert(0, {"id": "sale-2", "kind": "sale", "amount_minor": 30000, "balance_after_minor": 31000})
            await runner.watch_sales()  # too soon: not checked again
            runner.sales_checked_at = float("-inf")
            await runner.watch_sales()
            runner.sales_checked_at = float("-inf")
            await runner.watch_sales()
        self.assertEqual(messages[0], "Sales so far: 1 funding(s), 1000 minor.")
        received = [message for message in messages if message.startswith("Payment received")]
        self.assertEqual(received, ["Payment received: a buyer funded 30000 minor (0.03 SOL) of credit, sent on-chain to the payout "
                                    "address. Sales total: 31000 minor."])
        self.assertEqual(len(self.session.made("GET", "/api/wallet/transactions")), 3)

    async def test_without_docker_scripts_are_not_served(self):
        self.write_config(sharing=SHARING)
        with patch.object(worker, "docker_ready", AsyncMock(return_value=False)):
            await worker.Worker().run(once=True)
        self.assertEqual(self.session.made("POST", "/api/worker/service-1/claim"), [])
        self.assertIn("Docker", self.status()["unavailable"]["script"])

    async def test_agent_tasks_need_a_configured_command(self):
        self.write_config(sharing={**SHARING, "modes": ["agent"]})
        with patch.dict(os.environ, {"AUTARK_WORKER_AGENT_COMMAND": ""}):
            await worker.Worker().run(once=True)
        self.assertEqual(self.session.made("POST", "/api/vendor/services")[0]["json_body"]["url"], "worker:agent")
        self.assertIn("AUTARK_WORKER_AGENT_COMMAND", self.status()["unavailable"]["agent"])

    async def test_a_second_worker_defers_to_a_running_one(self):
        self.write_config(sharing=SHARING)
        worker.write_json(self.directory / "worker-status.json", {"pid": -1, "state": "idle", "updated_at": time.time()})
        await worker.Worker().run(once=True)
        self.assertEqual(self.session.calls, [])

    async def test_a_marketplace_error_is_reported_not_fatal(self):
        self.write_config(sharing=SHARING)
        self.session.on("POST", "/api/auth/register", MockShopApiError(None, "Could not connect to mock-service."))
        self.assertEqual(await worker.Worker().run(once=True), 0)
        self.assertEqual((self.status()["state"], self.status()["last_error"]), ("error", "Could not connect to mock-service."))


class LogTests(ConfigDirectoryTestCase):
    def logged(self, label, text, limit=2000):
        messages = []
        with patch.object(worker, "LOG_TEXT_CHARS", limit), patch.object(worker, "log", messages.append):
            worker.log_text(label, text)
        return messages

    def test_a_strangers_text_cannot_drive_the_terminal(self):
        [message] = self.logged("Prompt", "\x1b[2J\x1b]0;owned\x07hi\r\nthere\u202e\x08 \x1b")
        self.assertEqual(message, "Prompt (27 characters):\n    | hi\n    | there ")

    def test_long_text_is_cut_and_can_be_hidden(self):
        [cut] = self.logged("Answer", "abcdefgh", limit=3)
        self.assertEqual(cut, "Answer (8 characters):\n    | abc\n    | ... 5 more characters not shown")
        [hidden] = self.logged("Answer", "secret plan", limit=0)
        self.assertEqual(hidden, "Answer: 11 characters, not shown (AUTARK_WORKER_LOG_TEXT_CHARS=0).")

    def test_a_console_without_utf8_still_gets_the_line(self):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="ascii")
        with redirect_stdout(stream):
            worker.log("Haiku: \u017curaw \u2603")
        stream.seek(0)
        self.assertTrue(stream.read().endswith("Haiku: ?uraw ?\n"))

    def test_minor_units_read_as_sol(self):
        self.assertEqual([worker.sol(3548), worker.sol(30000), worker.sol(0), worker.sol(None)], ["0.003548", "0.03", "0", "0"])


class ExecutionTests(ConfigDirectoryTestCase):
    async def test_output_budget_is_shared_and_cleaned(self):
        (out, err), truncated = worker.truncate(b"a\x00b", b"c")
        self.assertEqual((out, err, truncated), ("ab", "c", False))
        (out, err), truncated = worker.truncate(b"x" * 70000, b"lost")
        self.assertEqual((len(out), err, truncated), (worker.MAX_OUTPUT_BYTES, "", True))

    async def test_process_runs_to_completion_with_stdin(self):
        out, _err, code, timed_out, elapsed = await worker.run_process(
            [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"], b"hello", 20)
        self.assertEqual((out.strip(), code, timed_out), (b"HELLO", 0, False))
        self.assertRegex(elapsed, r"^[0-9]+\.[0-9]{3}$")

    async def test_process_is_stopped_at_the_limit(self):
        _out, _err, code, timed_out, elapsed = await worker.run_process([sys.executable, "-c", "import time; time.sleep(30)"], b"", 0.5)
        self.assertEqual((code, timed_out), (124, True))
        self.assertLessEqual(float(elapsed), 0.5)

    async def test_task_goes_to_the_agent_command_without_this_machines_secrets(self):
        command = [sys.executable, "-c", "import os, sys; print(sys.stdin.read()); print(os.environ.get('MOCK_SHOP_PASSWORD')); print(os.getcwd())"]
        with patch.dict(os.environ, {"MOCK_SHOP_PASSWORD": "buyer-secret"}):
            result = await worker.run_task(b"Summarise this.", 20, command)
        lines = result["output"].splitlines()
        self.assertEqual((lines[0], lines[1], result["exit_code"], result["timed_out"]), ("Summarise this.", "None", 0, False))
        self.assertNotEqual(os.path.realpath(lines[2]), os.path.realpath(os.getcwd()))

    async def test_a_missing_agent_command_fails_the_task_cleanly(self):
        result = await worker.run_task(b"task", 5, ["autark-no-such-command"])
        self.assertEqual((result["exit_code"], result["timed_out"]), (127, False))
        self.assertIn("could not start", result["output"])

    async def test_idleness_follows_the_last_tool_call(self):
        self.assertEqual(worker.seconds_since_activity(), float("inf"))
        (self.directory / "last-activity").touch()
        self.assertLess(worker.seconds_since_activity(), 5)


class TrustedHostTests(ConfigDirectoryTestCase):
    async def test_remote_marketplace_needs_a_listed_host_and_https(self):
        cases = (("https://autark.example.org", "autark.example.org", True), ("http://autark.example.org", "autark.example.org", False),
                 ("https://autark.example.org", "", False), ("https://evil.example.org", "autark.example.org", False),
                 ("http://127.0.0.1:500", "", True), ("https://autark.example.org/api", "autark.example.org, other.example", True))
        for url, trusted, accepted in cases:
            with self.subTest(url=url, trusted=trusted), patch.dict(os.environ, {"MOCK_SERVICE_API_URL": url, "MOCK_SERVICE_TRUSTED_HOSTS": trusted}):
                if accepted:
                    self.assertEqual(settings.api_base_url(), url.removesuffix("/api"))
                else:
                    with self.assertRaises(settings.ConfigError):
                        settings.api_base_url()


class OwnerOnlyFileTests(ConfigDirectoryTestCase):
    async def test_a_private_file_is_readable_by_its_owner_only(self):
        path = self.directory / "worker.json"
        worker.write_json(path, {"a": 1}, private=True)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": 1})
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return
        import subprocess
        acl = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True).stdout
        entries = [line.strip() for line in acl.splitlines()[:-1] if ":" in line]
        # One explicit entry: the owner. Nothing inherited from the directory (no "(I)").
        self.assertEqual(len(entries), 1, acl)
        self.assertNotIn("(I)", acl)
        self.assertIn("(F)", acl)
