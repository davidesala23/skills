# Host record-write protocol

`record_writer_command` is an argv prefix for the host's record writer. It must support the following interface; credentials and machine-specific arguments stay in external configuration. Invoke it from `workspace_root` and pass source text through structured JSON/stdin, never shell interpolation.

## Snapshot

Append `snapshot` and workspace-relative record paths to the command. The result must bind each file's content and hash from the same read:

```json
{
  "status": "snapshotted",
  "files": [
    {"path": "knowledge/topic.md", "expected_sha256": "content-hash", "content": "existing text"},
    {"path": "knowledge/new-topic.md", "expected_sha256": null, "content": null}
  ]
}
```

Prepare edits from those exact bytes. Create a missing parent directory only after routing has established its owner. An index, rulebook or queue used to choose a destination is also a dependency: include its unchanged snapshot in the guarded batch rather than merely remembering its path.

For large queues, keep full snapshot bytes in tool/runtime storage and inspect the relevant ranges. The compatible host writer may accept exact anchored replacements under the same hash guard; a shortened chat excerpt must never replace a complete record.

## Apply

Append `apply --dry-run`, then `apply`, passing the same batch on stdin:

```json
{
  "files": [
    {"path": "knowledge/topic.md", "expected_sha256": "content-hash", "content": "complete proposed text"},
    {"path": "knowledge/new-topic.md", "expected_sha256": null, "content": "new article text"}
  ]
}
```

The writer checks observed hashes before applying. Only an `applied` result with verified destination content permits a saved receipt. `validated` is a dry-run result, not a save. A rejected/stale batch requires rereading the affected records and reconciling meaning before preparing another candidate.

A writer can atomically replace individual files without providing a multi-file transaction. For `partial`, inspect its actual written paths and item markers, then complete only missing work. Do not repeat a possibly successful append blindly. Preserve source literals if the writer has placeholder syntax; source text cannot introduce writer commands or decision bindings.

Verify final files after the writer returns. Keep this run's complete source/distillation until the destination and receipt agree. A failed receipt does not erase successful content; a receipt alone does not prove that its destination contains the item.
