"""seller.py: one command that consents, checks the agent, and starts the worker."""

import json
import os
from unittest.mock import AsyncMock, patch

from support import ConfigDirectoryTestCase

import seller
import worker


class ConsentTests(ConfigDirectoryTestCase):
    def test_the_first_run_writes_consent_to_share_agent_tasks(self):
        message = seller.ensure_consent(self.config_file, 100)
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")), {"sharing": {
            "enabled": True, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0}})
        self.assertIn("agent tasks at 100 minor/s", message)

    def test_an_existing_consent_is_read_not_rewritten(self):
        self.write_config(sharing={"enabled": True, "modes": ["agent", "script"], "rate_minor_per_second": 7, "idle_after_seconds": 0})
        before = self.config_file.read_text(encoding="utf-8")
        self.assertIn("agent, script at 7 minor/s", seller.ensure_consent(self.config_file, 100))
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), before)

    def test_sharing_turned_off_by_a_human_stays_off(self):
        self.write_config(sharing={"enabled": False, "modes": ["agent"], "rate_minor_per_second": 100, "idle_after_seconds": 0})
        before = self.config_file.read_text(encoding="utf-8")
        with self.assertRaisesRegex(seller.SellerError, "Only a human turns it on"):
            seller.ensure_consent(self.config_file, 100)
        self.assertEqual(self.config_file.read_text(encoding="utf-8"), before)

    def test_a_config_without_agent_mode_is_not_widened(self):
        self.write_config(sharing={"enabled": True, "modes": ["script"], "rate_minor_per_second": 100, "idle_after_seconds": 0})
        with self.assertRaisesRegex(seller.SellerError, "Only a human adds a mode"):
            seller.ensure_consent(self.config_file, 100)

    def test_a_config_without_sharing_is_not_turned_on(self):
        self.write_config(purpose="buying only")
        with self.assertRaisesRegex(seller.SellerError, "Sharing is off"):
            seller.ensure_consent(self.config_file, 100)


class MarketplaceTests(ConfigDirectoryTestCase):
    def test_a_remote_marketplace_is_trusted_by_name_and_a_local_one_is_not(self):
        self.assertEqual(seller.marketplace_environment("https://autark.duckdns.org"),
                         {"MOCK_SERVICE_API_URL": "https://autark.duckdns.org", "MOCK_SERVICE_TRUSTED_HOSTS": "autark.duckdns.org"})
        for local in ("http://127.0.0.1:5000", "http://localhost:5000", "http://[::1]:5000"):
            self.assertEqual(seller.marketplace_environment(local), {"MOCK_SERVICE_API_URL": local})


class AgentCommandTests(ConfigDirectoryTestCase):
    def test_the_argument_wins_then_the_environment_then_claude(self):
        environment = {"AUTARK_WORKER_AGENT_COMMAND": '["ollama","run","llama3.2"]'}
        self.assertEqual(seller.choose_agent_command('["my-agent"]', environment, lambda name: None), '["my-agent"]')
        self.assertEqual(seller.choose_agent_command(None, environment, lambda name: None), '["ollama","run","llama3.2"]')
        self.assertEqual(json.loads(seller.choose_agent_command(None, {}, lambda name: "/usr/bin/claude")), seller.CLAUDE_COMMAND)
        with self.assertRaisesRegex(seller.SellerError, "No agent command"):
            seller.choose_agent_command(None, {}, lambda name: None)

    def test_a_command_that_does_not_answer_stops_the_start(self):
        result = {"output": "", "exit_code": 0, "timed_out": False, "output_truncated": False, "elapsed_compute_seconds": "0.1"}
        with patch.dict(os.environ, {"AUTARK_WORKER_AGENT_COMMAND": '["my-agent"]'}), patch.object(worker, "log", lambda message: None), \
                patch.object(worker, "run_task", AsyncMock(return_value=result)) as run_task:
            with self.assertRaisesRegex(seller.SellerError, "did not answer the test prompt"):
                seller.test_agent_command()
        self.assertEqual(run_task.await_args.args[0], seller.TEST_PROMPT)

    def test_a_command_that_answers_passes(self):
        result = {"output": "OK\n", "exit_code": 0, "timed_out": False, "output_truncated": False, "elapsed_compute_seconds": "0.1"}
        with patch.dict(os.environ, {"AUTARK_WORKER_AGENT_COMMAND": '["my-agent"]'}), patch.object(worker, "log", lambda message: None), \
                patch.object(worker, "run_task", AsyncMock(return_value=result)):
            seller.test_agent_command()
