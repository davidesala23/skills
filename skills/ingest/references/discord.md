# Native Discord collection

The public helper preserves the prepared-job/native-client contract. It reads configured channels, threads, forums and DMs through authenticated GET operations, without exporting credentials, sending messages or changing read marks. A changed internal client shape fails explicitly.

1. Read `discord_config_path`: expected account, server and browser profile belong there. Use the host's authorized browser surface. In a host exposing `preview_*`, start with `preview_status` and open a run-owned tab in the configured profile. Do not select an arbitrary existing tab or another account's browser.
2. Prepare a job with the configured runtime and deadline:

   ```text
   python3 <skill_dir>/scripts/discord-fetch.py prepare-t3 --json
   ```

   Retain `run_id` and returned expressions. Preparation persists an intent but does not advance cursors. Use global `--state-dir <isolated-dir>` and `prepare-t3 --dry-run` for non-consuming fixtures.
3. Execute `start_expression` once on the run-owned tab. Poll using the returned expression inside its deadline while doing independent work; a tool timeout does not justify starting another job. The client verifies account/server and reports skipped sources.
4. Read the result with `read_expression`, or use `read_chunk_expression` with progressive offsets/lengths when output can be truncated. Reassemble the exact text, then parse JSON; partial transport is not a valid result. Persist large bodies to a private runtime file using structured arguments, not shell interpolation.
5. Commit the result through the helper:

   ```text
   python3 <skill_dir>/scripts/discord-fetch.py ingest-t3 <run_id> --input <result.json> --json
   ```

   The helper verifies run and account identity, saves bodies before advancing cursors, merges cursors monotonically under a local lock and makes a repeated ingest of the same run idempotent. Use the returned `harvest` path; do not reconstruct it from the day.
6. Read the full harvest and each unresolved prior recovery source. Compare actual source headings/counts with the reader's manifest. Fetch coverage and reading coverage are separate; the diagnostic “not read” block is not a message source. Deduplicate overlaps on actual message IDs/permalinks.
7. After durable handoff, run `cleanup_expression` and close only the run-owned tab. Retain files required by unresolved receipts before deleting disposable scratch material.

For another caller's fixed date window, use global `--state-dir <isolated-dir>`, then `prepare-t3 --since <RFC3339> --until <RFC3339> --config <scope.json>`. Fixed-window runs neither consume nor advance normal cursors. `--include-archived-threads` is limited to that mode.

## Honest outcomes

- `down`: collection failed; preserve the actual reason. An access/login problem, transport failure and validation error are different diagnoses.
- `partial`: retain successful bodies and show skips, caps or deadline limits. Read all retained material without claiming complete server coverage.
- `up` with zero messages: no new material in the verified scope. Prior recovery items can still be open.

When the browser surface is unavailable, report a blocked source or submit the prepared job's explicit failure result through the helper. Do not fall back to a retired browser extension or retrieve session tokens. Collection success does not prove persistence of login across an application restart.
