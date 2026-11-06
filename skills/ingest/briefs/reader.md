# Read-only source reader

The parent supplies:

- source IDs and exact assigned representations;
- local source manifest/body paths, or one designated collector for missing bodies;
- relevant source/routing references and current target pointers;
- a private staging directory, dispatch timestamp and deadline.

Read the full assigned scope before distilling. Do not fetch another worker's account or mutate KB records, capture channels, shared state or receipts. Only the assigned staging directory is writable.

Stage complete source bodies and a cumulative JSON result atomically. Include:

```text
sources: [{id, representation, body_path, body_hash, event_time,
           acquisition_time, coverage, missing_scope}]
items: [{id, source_ids, thesis, evidence, attribution, uncertainty,
         candidate_owner, owner_dependencies}]
blocked: [{source_or_item_id, failed_operation, reason, recovery}]
```

One source can yield several items; preserve all their content, not only counts or target names. Distinguish missing source coverage from material retained but not yet read. ASR text is not verified speaker identity; captions do not verify visuals.

Return the result path/hash, actual source/item counts and completion status. If the deadline ends, preserve completed items and exact recovery for the rest. Do not claim closure to satisfy the parent's timing. The parent rereads the complete staged result, resolves routing and owns every record write.
