#!/usr/bin/env python3
"""Load external ingest settings without contacting providers or writing files."""

import json
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SOURCES = {"gemini", "hex", "x", "work-mail", "personal-mail", "clipper", "discord"}
CORE_PATHS = ("runtime_dir", "rulebook_path", "queue_path", "run_log_path")
PROVIDER_STRINGS = {
    "gemini": (),
    "hex": ("hex_recordings_dir", "hex_metadata_filename", "hex_transcript_filename",
            "hex_microphone_filename", "hex_system_filename", "hex_whisper_python",
            "hex_whisper_model"),
    "x": ("x_bookmarks_url", "browser_profile"),
    "work-mail": (),
    "personal-mail": ("gmail_keychain_service", "gmail_imap_host"),
    "clipper": ("clipper_dir",),
    "discord": ("discord_config_path", "discord_runtime_dir"),
}
PROVIDER_LISTS = {
    "gemini": ("gemini_senders",),
    "work-mail": ("work_email_queries",),
}
PROVIDER_ACCOUNTS = {"gemini": "gemini_accounts", "work-mail": "work_email_accounts"}
PROVIDER_PATHS = ("hex_recordings_dir", "hex_whisper_python", "hex_whisper_model",
                  "clipper_dir", "discord_config_path", "discord_runtime_dir")
DEADLINES = {"hex": "hex_transcription_timeout_seconds",
             "discord": "discord_deadline_seconds"}


class ConfigError(ValueError):
    pass


def _string(value):
    return isinstance(value, str) and bool(value.strip())


def _require_string(config, key):
    if not _string(config.get(key)):
        raise ConfigError(f"{key} must be a non-empty string")


def _strings(config, key, nonempty=False):
    value = config.get(key)
    if (not isinstance(value, list) or (nonempty and not value)
            or not all(_string(item) for item in value)):
        raise ConfigError(f"{key} must be an array of non-empty strings")


def _accounts(config, key):
    value = config.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{key} must be a non-empty array of account objects")
    for account in value:
        if not isinstance(account, dict) or not all(
                _string(account.get(field)) for field in ("label", "email")):
            raise ConfigError(f"{key} entries need non-empty label and email values")


def _positive(config, key):
    value = config.get(key)
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{key} must be a positive finite number")


def load_config():
    base = os.environ.get("SKILLS_CONFIG_DIR")
    if not _string(base):
        raise ConfigError("Set SKILLS_CONFIG_DIR before running ingest")
    path = Path(base).expanduser() / "ingest" / "config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise ConfigError(
            "Cannot read $SKILLS_CONFIG_DIR/ingest/config.json; "
            "create valid settings from config.example.json"
        ) from error
    if not isinstance(config, dict):
        raise ConfigError("ingest configuration must be a JSON object")

    for key in ("workspace_root", "timezone", *CORE_PATHS):
        _require_string(config, key)
    workspace = Path(config["workspace_root"]).expanduser()
    if not workspace.is_absolute():
        raise ConfigError("workspace_root must be an absolute path")
    try:
        ZoneInfo(config["timezone"])
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError("timezone must name an installed IANA time zone") from error
    if type(config.get("lookback_days")) is not int or config["lookback_days"] < 0:
        raise ConfigError("lookback_days must be a nonnegative integer")
    _positive(config, "source_deadline_seconds")
    _strings(config, "record_writer_command", nonempty=True)
    _strings(config, "kb_roots")
    _strings(config, "enabled_sources")
    enabled = set(config["enabled_sources"])
    if not enabled <= SOURCES:
        raise ConfigError("enabled_sources contains an unsupported source")
    if len(enabled) != len(config["enabled_sources"]):
        raise ConfigError("enabled_sources must not contain duplicates")
    areas = config.get("area_kb_paths")
    if (not isinstance(areas, dict) or not {"work", "personal", "workspace"} <= areas.keys()
            or not all(_string(key) and _string(value) for key, value in areas.items())):
        raise ConfigError("area_kb_paths needs work, personal and workspace path values")

    for source in enabled:
        for key in PROVIDER_STRINGS[source]:
            _require_string(config, key)
        for key in PROVIDER_LISTS.get(source, ()):
            _strings(config, key, nonempty=True)
        if source in PROVIDER_ACCOUNTS:
            _accounts(config, PROVIDER_ACCOUNTS[source])
        if source in DEADLINES:
            _positive(config, DEADLINES[source])
    if "discord" in enabled:
        value = config.get("discord_retention_days")
        if type(value) is not int or value <= 0:
            raise ConfigError("discord_retention_days must be a positive integer")
    if "x" in enabled:
        try:
            url = urlsplit(config["x_bookmarks_url"])
        except ValueError as error:
            raise ConfigError("x_bookmarks_url must be an absolute HTTP or HTTPS URL") from error
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ConfigError("x_bookmarks_url must be an absolute HTTP or HTTPS URL")
    language = config.get("hex_whisper_language")
    if language is not None and not _string(language):
        raise ConfigError("hex_whisper_language must be a non-empty string or null")

    def resolve(value):
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else workspace / path)

    config["workspace_root"] = str(workspace)
    for key in (*CORE_PATHS, "legacy_state_path", *PROVIDER_PATHS):
        if config.get(key) is not None:
            _require_string(config, key)
            config[key] = resolve(config[key])
    config["kb_roots"] = [resolve(value) for value in config["kb_roots"]]
    config["area_kb_paths"] = {key: resolve(value) for key, value in areas.items()}
    return config


def main():
    try:
        config = load_config()
    except ConfigError as error:
        print(json.dumps({"error": "ingest_config_error", "detail": str(error)}),
              file=sys.stderr)
        return 2
    print(json.dumps(config, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
