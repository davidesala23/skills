# Ingest configuration

Copy `config.example.json` to `$SKILLS_CONFIG_DIR/ingest/config.json` and replace the
placeholder values. Set `SKILLS_CONFIG_DIR` to the external configuration directory;
keep the real file in a private workspace. Missing or invalid configuration stops
before provider access or state writes.

The example enables no providers. Add only the sources configured for the host to
`enabled_sources`. Provider settings are required when their source is enabled;
disabled sources may omit their settings or keep the example's empty/null values.

## Core settings

| Key | Meaning |
|---|---|
| `workspace_root` | Absolute workspace directory; `~` expands to the current user's home |
| `timezone` | Installed IANA time zone used for date windows |
| `runtime_dir` | Directory for ingest runtime state |
| `kb_roots` | Array of knowledge-base roots searched for existing owners |
| `area_kb_paths` | Object containing `work`, `personal` and `workspace` KB destinations |
| `rulebook_path` | Private source-routing rulebook |
| `queue_path` | Private capture/reading queue |
| `run_log_path` | Private ingest-run log |
| `record_writer_command` | Non-empty argv array for the host's record writer; invoke from the workspace |
| `enabled_sources` | Unique source names: `gemini`, `hex`, `x`, `work-mail`, `personal-mail`, `clipper`, `discord` |
| `lookback_days` | Nonnegative integer defining the source lookback window |
| `source_deadline_seconds` | Positive finite time limit for a source operation |
| `legacy_state_path` | Optional prior state file to reconcile during migration; null disables this input |

Relative paths resolve under `workspace_root`, including KB paths and provider path
fields. Absolute paths stay absolute; `~` expands. Command argv, URLs, labels,
filenames and account values remain literal. Validation does not check that files,
credentials, providers or models are available, and creates no directories.

## Provider settings

| Source | Key | Meaning |
|---|---|---|
| `gemini` | `gemini_accounts` | Non-empty array of account objects with `label` and `email` |
| `gemini` | `gemini_senders` | Non-empty array of meeting-notification sender strings |
| `work-mail` | `work_email_accounts` | Non-empty array of account objects with `label` and `email` |
| `work-mail` | `work_email_queries` | Non-empty array of mail-search query strings |
| `personal-mail` | `gmail_keychain_service` | Name of the existing credential entry; no credential value belongs here |
| `personal-mail` | `gmail_imap_host` | IMAP server hostname |
| `x` | `x_bookmarks_url` | Absolute HTTP/HTTPS bookmarks URL |
| `x` | `browser_profile` | Existing browser profile identifier, kept literal |
| `clipper` | `clipper_dir` | Directory holding incoming clips |
| `discord` | `discord_config_path` | Existing account/server/channel configuration file |
| `discord` | `discord_runtime_dir` | Existing cursors and raw-harvest directory |
| `discord` | `discord_retention_days` | Positive integer defining raw-harvest retention |
| `discord` | `discord_deadline_seconds` | Positive finite time limit for Discord collection |
| `hex` | `hex_recordings_dir` | Directory containing the recording folders |
| `hex` | `hex_metadata_filename` | Recording metadata filename, normally `manifest.json` |
| `hex` | `hex_transcript_filename` | Recording transcript filename, normally `transcript.md` |
| `hex` | `hex_microphone_filename` | Microphone-track filename, normally `microphone.wav` |
| `hex` | `hex_system_filename` | System-track filename, normally `system.wav` |
| `hex` | `hex_whisper_python` | Filesystem path to the interpreter with the transcription dependency installed |
| `hex` | `hex_whisper_model` | Filesystem path to an already available local model; no model download is implied |
| `hex` | `hex_whisper_language` | Optional language code; null permits automatic language detection |
| `hex` | `hex_transcription_timeout_seconds` | Positive finite time limit for local transcription |

The Hex helper decides whether a populated transcript can be reused before invoking
the configured transcription runtime. Configuration validation requires the Hex
settings when Hex is enabled but does not import that runtime or load a model.

Run `python3 scripts/ingest_config.py` from this skill directory to inspect the
resolved configuration. It writes no files and prints configured account/path
values, so keep its output private. An error exits with code `2` and an
`ingest_config_error` JSON message.

## Discord perimeter file

`discord_config_path` points to private input data with the expected account,
server and source IDs. Use real IDs only in that external file. A generic shape is:

```json
{
  "expected_user_id": "1",
  "t3_profile_name": "example-profile",
  "guild": {"id": "100", "name": "Example Space"},
  "channels": [{"id": "200", "name": "general"}],
  "dm": {"include": true, "include_groups": true, "active_within_days": 7},
  "seed_days": 7,
  "max_messages_per_channel": 100,
  "include_threads": true,
  "thread_search_max_pages": 10
}
```

Account/server IDs are required, channel IDs must be unique numeric strings,
enabled DM collection needs its group/lookback settings, and seed days/message
caps must be positive. Browser profile names and channel names remain private
input values. Keep the existing perimeter file when migrating; changing its path
does not authorize widening its scope.
