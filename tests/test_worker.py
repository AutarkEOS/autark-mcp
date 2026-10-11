"""The idle worker: consent, idleness, the vendor account, and running other agents' jobs."""

import base64
import json
import os
import sys
import time
from unittest.mock import AsyncMock, patch

from support import ConfigDirectoryTestCase, FakeSession

import settings
import worker
from client import MockShopApiError

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
        saved = json.loads((self.directory / "worker.json").read_text(encoding="utf-8"))[settings.api_base_url()]
        self.assertEqual((saved["username"], saved["password"], saved["offers"]), (registered["username"], registered["password"], {"script": "service-1"}))
        self.assertEqual((self.status()["state"], self.status()["offers"]), ("idle", {"script": "service-1"}))
        self.assertEqual(len(self.session.made("POST", "/api/worker/service-1/claim")), 1)

    async def test_next_start_reuses_the_account_and_reprices_the_offer(self):
        self.write_config(sharing=SHARING)
        await worker.Worker().run(once=True)
        self.session.on("POST", "/api/auth/login", {"token": "vendor-token-2", "user": {}})
        self.session.on("GET", "/api/vendor/services", {"services": [OFFER]})
        self.session.on("PATCH", "/api/vendor/services/service-1", {"service": {**OFFER, "price_minor": 8, "revision": 1}})
        self.write_config(sharing={**SHARING, "rate_minor_per_second": 8})
        (self.directory / "worker-status.json").unlink()
        await worker.Worker().run(once=True)
        self.assertEqual((len(self.session.made("POST", "/api/auth/register")), len(self.session.made("POST", "/api/vendor/services"))), (1, 1))
        self.assertEqual(self.session.made("PATCH", "/api/vendor/services/service-1")[0]["json_body"], {"revision": 0, "price_minor": 8})

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
