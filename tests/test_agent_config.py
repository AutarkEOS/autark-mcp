import json
import os
from pathlib import Path

from support import ConfigDirectoryTestCase

import agent_config
from agent_config import AgentConfigError, apply_update, load_config, save_config, validate_config

LIMITED = {"purpose": "buy fruit", "purchase_mode": "ask",
           "spending": {"max_per_transaction_minor": 500, "daily_budget_minor": 2000, "allowed_kinds": ["product", "api-usage"]},
           "preferences": {"profile": "cheapest"}}


class AgentConfigTests(ConfigDirectoryTestCase):
    def test_missing_file_is_no_config(self):
        self.assertIsNone(load_config())

    def test_defaults(self):
        self.assertEqual(validate_config({}), {
            "version": 1, "purpose": "", "purchase_mode": "ask",
            "spending": {"max_per_transaction_minor": None, "daily_budget_minor": None, "allowed_kinds": None},
            "preferences": {}})

    def test_save_and_load_round_trip_without_leftovers(self):
        saved = save_config(LIMITED)
        self.assertEqual(load_config(), saved)
        self.assertEqual(saved["spending"]["allowed_kinds"], ["api-usage", "product"])
        self.assertEqual([path.name for path in self.directory.iterdir()], ["agent-config.json"])
        self.assertEqual(json.loads(self.config_file.read_text(encoding="utf-8")), saved)

    def test_save_creates_missing_directories_and_replaces_the_file(self):
        os.environ["AUTARK_AGENT_CONFIG"] = str(self.directory / "nested" / "deeper" / "config.json")
        save_config({"purpose": "first"})
        save_config({"purpose": "second"})
        self.assertEqual(load_config()["purpose"], "second")

    def test_paths_follow_the_environment(self):
        self.assertEqual(agent_config.config_path(), self.config_file)
        del os.environ["AUTARK_DECISION_LOG"]
        self.assertEqual(agent_config.audit_path(), self.directory / "decisions.jsonl")
        del os.environ["AUTARK_AGENT_CONFIG"]
        self.assertEqual(agent_config.config_path(), Path.home() / ".autark" / "agent-config.json")

    def test_unreadable_files_raise_instead_of_meaning_no_limits(self):
        for text in ("{not json", "[]", '"text"', '{"spending": {"daily_budget_minor": -1}}', '{"surprise": 1}'):
            with self.subTest(text=text):
                self.config_file.write_text(text, encoding="utf-8")
                with self.assertRaises(AgentConfigError):
                    load_config()

    def test_invalid_configs_are_rejected(self):
        invalid = [
            {"version": 2}, {"purpose": 5}, {"purpose": "x" * 501}, {"purpose": "nul\x00"}, {"purchase_mode": "yolo"},
            {"spending": []}, {"spending": {"extra": 1}}, {"spending": {"max_per_transaction_minor": -1}},
            {"spending": {"max_per_transaction_minor": 1.5}}, {"spending": {"max_per_transaction_minor": True}},
            {"spending": {"daily_budget_minor": 2_147_483_648}}, {"spending": {"daily_budget_minor": "10"}},
            {"spending": {"allowed_kinds": "product"}}, {"spending": {"allowed_kinds": ["service"]}},
            {"spending": {"allowed_kinds": ["product", "product"]}},
            {"preferences": []}, {"preferences": "cheapest"}, {"preferences": {"surprise": 1}},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(AgentConfigError):
                validate_config(raw)

    def test_preference_values_are_left_to_the_marketplace(self):
        valid = {"priorities": ["quality", "price"], "limits": {"min_rating": 4}, "risk_tolerance": "low"}
        self.assertEqual(validate_config({"preferences": valid})["preferences"], valid)
        # Only the marketplace knows the profiles and ranges; it rejects these on the next ranking.
        unchecked = {"profile": "best", "weights": {"price": -1}}
        self.assertEqual(validate_config({"preferences": unchecked})["preferences"], unchecked)


class TightenOnlyTests(ConfigDirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.current = validate_config(LIMITED)

    def test_first_configuration_may_set_anything(self):
        created = apply_update(None, {"purpose": "research", "purchase_mode": "auto", "max_per_transaction_minor": 900,
                                      "daily_budget_minor": 5000, "allowed_kinds": ["compute"], "preferences": {"profile": "fastest"}})
        self.assertEqual((created["purchase_mode"], created["spending"]["max_per_transaction_minor"],
                          created["spending"]["allowed_kinds"]), ("auto", 900, ["compute"]))

    def test_tightening_is_allowed(self):
        updated = apply_update(self.current, {"max_per_transaction_minor": 100, "daily_budget_minor": 0, "allowed_kinds": ["product"]})
        self.assertEqual(updated["spending"], {"max_per_transaction_minor": 100, "daily_budget_minor": 0, "allowed_kinds": ["product"]})
        self.assertEqual((updated["purpose"], updated["preferences"]), ("buy fruit", {"profile": "cheapest"}))

    def test_equal_limits_and_empty_updates_change_nothing(self):
        self.assertEqual(apply_update(self.current, {}), self.current)
        self.assertEqual(apply_update(self.current, {"max_per_transaction_minor": 500, "daily_budget_minor": 2000,
                                                     "allowed_kinds": ["api-usage", "product"]}), self.current)

    def test_loosening_is_refused_and_names_the_file(self):
        loosening = [
            {"max_per_transaction_minor": 501}, {"daily_budget_minor": 2001}, {"max_per_transaction_minor": None},
            {"daily_budget_minor": None}, {"allowed_kinds": ["product", "api-usage", "compute"]}, {"allowed_kinds": ["compute"]},
            {"allowed_kinds": None}, {"purchase_mode": "auto"},
            {"max_per_transaction_minor": 100, "daily_budget_minor": 9999},
        ]
        for update in loosening:
            with self.subTest(update=update), self.assertRaises(AgentConfigError) as raised:
                apply_update(self.current, update)
            self.assertIn(str(self.config_file), str(raised.exception))
            self.assertIn("Only a human", str(raised.exception))

    def test_unset_limits_can_be_introduced_later(self):
        open_config = validate_config({})
        updated = apply_update(open_config, {"daily_budget_minor": 300, "allowed_kinds": ["product"]})
        self.assertEqual((updated["spending"]["daily_budget_minor"], updated["spending"]["allowed_kinds"]), (300, ["product"]))

    def test_auto_can_go_back_to_ask_and_preferences_are_free(self):
        auto = validate_config({**LIMITED, "purchase_mode": "auto"})
        self.assertEqual(apply_update(auto, {"purchase_mode": "ask"})["purchase_mode"], "ask")
        changed = apply_update(self.current, {"purpose": "new goal", "preferences": {"profile": "quality_first", "limits": {"max_price_minor": 99999}}})
        self.assertEqual((changed["purpose"], changed["preferences"]["profile"]), ("new goal", "quality_first"))
        self.assertEqual(changed["spending"], self.current["spending"])

    def test_invalid_updates_are_rejected(self):
        for update in ({"spending": {}}, {"unknown": 1}, {"preferences": {"surprise": 1}}, {"max_per_transaction_minor": -5},
                       {"allowed_kinds": ["service"]}, {"purchase_mode": "never"}):
            with self.subTest(update=update), self.assertRaises(AgentConfigError):
                apply_update(self.current, update)

    def test_update_does_not_mutate_the_current_config(self):
        before = json.loads(json.dumps(self.current))
        apply_update(self.current, {"max_per_transaction_minor": 1, "allowed_kinds": ["product"]})
        self.assertEqual(self.current, before)
