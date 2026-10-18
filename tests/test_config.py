import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openbox_monitor.runner import load_config


class DeploymentConfigTests(unittest.TestCase):
    def test_environment_overrides_file_and_preserves_nested_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({
                "database": "local.db",
                "zip_code": "11111",
                "thresholds": {"alert_pct": 30},
            }), encoding="utf-8")
            with patch.dict(os.environ, {
                "OPENBOX_DATABASE": "/app/data/openbox.db",
                "BESTBUY_ZIP_CODE": "01234",
                "DISCORD_WEBHOOK_URL": "https://example.invalid/test-hook",
                "DISCORD_MENTION": "",
            }, clear=True):
                cfg = load_config(str(path))
        self.assertEqual(cfg["database"], "/app/data/openbox.db")
        self.assertEqual(cfg["zip_code"], "01234")
        self.assertEqual(cfg["discord_webhook"], "https://example.invalid/test-hook")
        self.assertEqual(cfg["thresholds"]["alert_pct"], 30)
        self.assertEqual(cfg["thresholds"]["strong_pct"], 45)

    def test_empty_environment_value_can_disable_file_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"discord_webhook": "https://example.invalid/hook"}))
            with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": ""}, clear=True):
                self.assertEqual(load_config(str(path))["discord_webhook"], "")

    def test_empty_database_path_is_rejected(self):
        with patch.dict(os.environ, {"OPENBOX_DATABASE": ""}, clear=True):
            with self.assertRaisesRegex(ValueError, "database path"):
                load_config(None)

    def test_loading_profile_does_not_mutate_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config("config.railway.json")
            cfg["thresholds"]["alert_pct"] = 99
            self.assertEqual(load_config(None)["thresholds"]["alert_pct"], 35)
            self.assertEqual(cfg["workers"], 4)


if __name__ == "__main__":
    unittest.main()
