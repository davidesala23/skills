#!/usr/bin/env python3
"""Isolated configuration-boundary checks; no provider calls or runtime writes."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SKILL = Path(__file__).resolve().parents[1]
SCRIPT = SKILL / "scripts" / "ingest_config.py"
SPEC = importlib.util.spec_from_file_location("ingest_config", SCRIPT)
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


class IngestConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "settings"
        self.config_file = self.config_dir / "ingest" / "config.json"
        self.config_file.parent.mkdir(parents=True)
        self.example = json.loads((SKILL / "config.example.json").read_text())
        self.example["workspace_root"] = str(self.root / "workspace")
        self.env = {"HOME": str(self.root / "home"),
                    "SKILLS_CONFIG_DIR": str(self.config_dir)}

    def load(self, config):
        self.config_file.write_text(json.dumps(config))
        with patch.dict(os.environ, self.env):
            return CONFIG.load_config()

    def enabled(self, source):
        config = dict(self.example, enabled_sources=[source])
        fields = {
            "gemini": {"gemini_accounts": [{"label": "primary", "email": "reader@example.test"}],
                       "gemini_senders": ["meeting-source@example.test"]},
            "work-mail": {"work_email_accounts": [{"label": "work", "email": "reader@example.test"}],
                          "work_email_queries": ["in:inbox newer_than:2d"]},
            "personal-mail": {"gmail_keychain_service": "mail-reader", "gmail_imap_host": "imap.example.test"},
            "x": {"x_bookmarks_url": "https://example.test/bookmarks", "browser_profile": "bookmarks"},
            "clipper": {"clipper_dir": "incoming/clips"},
            "discord": {"discord_config_path": "providers/discord.json", "discord_runtime_dir": "runtime/discord",
                        "discord_retention_days": 90, "discord_deadline_seconds": 60},
            "hex": {"hex_recordings_dir": "recordings", "hex_whisper_python": "tools/venv/bin/python",
                    "hex_whisper_model": "models/transcription", "hex_transcription_timeout_seconds": 300},
        }
        config.update(fields[source])
        return config

    def test_missing_environment_is_explicit_and_writes_nothing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CONFIG.ConfigError, "SKILLS_CONFIG_DIR"):
                CONFIG.load_config()
        self.assertFalse(self.config_file.exists())

    def test_missing_or_invalid_file_is_explicit(self):
        for content in (None, "{broken", "[]"):
            with self.subTest(content=content):
                if content is not None:
                    self.config_file.write_text(content)
                with patch.dict(os.environ, self.env):
                    with self.assertRaises(CONFIG.ConfigError):
                        CONFIG.load_config()

    def test_example_resolves_paths_but_preserves_argv_literals(self):
        self.example["legacy_state_path"] = "old/state.json"
        self.example["record_writer_command"] = ["python3", "~/literal/writer.py", "--mode=import"]
        result = self.load(self.example)
        self.assertEqual(result["runtime_dir"], str(self.root / "home/.local/state/ingest"))
        self.assertEqual(result["queue_path"], str(self.root / "workspace/notes/queue.md"))
        self.assertEqual(result["kb_roots"], [str(self.root / "workspace/knowledge")])
        self.assertEqual(result["area_kb_paths"]["work"], str(self.root / "workspace/knowledge/work"))
        self.assertEqual(result["legacy_state_path"], str(self.root / "workspace/old/state.json"))
        self.assertEqual(result["record_writer_command"], self.example["record_writer_command"])
        self.assertFalse((self.root / "workspace").exists())

    def test_bad_core_settings_fail_before_runtime_write(self):
        for key, value in [("workspace_root", "relative"), ("timezone", "Unknown/Nowhere"),
                           ("lookback_days", True), ("lookback_days", -1),
                           ("source_deadline_seconds", 0), ("source_deadline_seconds", float("inf")),
                           ("record_writer_command", []), ("enabled_sources", ["unknown"]),
                           ("enabled_sources", ["hex", "hex"]), ("area_kb_paths", {"work": "knowledge"})]:
            with self.subTest(key=key, value=value):
                with self.assertRaises(CONFIG.ConfigError):
                    self.load(dict(self.example, **{key: value}))
                self.assertFalse((self.root / "workspace").exists())

    def test_each_enabled_provider_requires_its_settings(self):
        for source in CONFIG.SOURCES:
            with self.subTest(source=source):
                with self.assertRaises(CONFIG.ConfigError):
                    self.load(dict(self.example, enabled_sources=[source]))
                result = self.load(self.enabled(source))
                self.assertEqual(result["enabled_sources"], [source])

    def test_enabled_provider_account_shape_and_queries_are_checked(self):
        bad = [("gemini", "gemini_accounts", [{"label": "primary"}]),
               ("gemini", "gemini_senders", []),
               ("work-mail", "work_email_accounts", []),
               ("work-mail", "work_email_queries", "in:inbox"),
               ("x", "x_bookmarks_url", "bookmarks"),
               ("x", "x_bookmarks_url", "https://[broken"),
               ("discord", "discord_retention_days", 0),
               ("discord", "discord_deadline_seconds", False),
               ("hex", "hex_transcription_timeout_seconds", -1)]
        for source, key, value in bad:
            with self.subTest(source=source, key=key):
                config = self.enabled(source)
                config[key] = value
                with self.assertRaises(CONFIG.ConfigError):
                    self.load(config)

    def test_hex_paths_resolve_without_loading_a_model_or_interpreter(self):
        result = self.load(self.enabled("hex"))
        self.assertEqual(result["hex_whisper_model"], str(self.root / "workspace/models/transcription"))
        self.assertEqual(result["hex_whisper_python"], str(self.root / "workspace/tools/venv/bin/python"))
        self.assertEqual(result["hex_metadata_filename"], "manifest.json")
        self.assertIsNone(result["hex_whisper_language"])
        self.assertFalse((self.root / "workspace").exists())
        configured = self.enabled("hex")
        configured["hex_whisper_language"] = "en"
        self.assertEqual(self.load(configured)["hex_whisper_language"], "en")

    def test_cli_missing_config_has_json_error_and_no_side_effect(self):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)],
                                env={**os.environ, **self.env}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "ingest_config_error")
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.config_file.exists())


if __name__ == "__main__":
    unittest.main()
