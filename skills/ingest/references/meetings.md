# Meetings and local recordings

## Resume and identify

Resume incomplete meeting receipts and retained distillations before applying the new date window. Check the configured legacy state for earlier processed document IDs, without rewriting that state. Verify the owning article's item marker when a receipt and a destination disagree.

When `legacy_state_path` is configured, inspect retained recovery files under
`runtime_dir/pending/`, including `pending/meetings/*.json` and prior staged/adjudication
records. Reuse their source bodies, complete payloads and per-item outcomes even when
the original event is outside the discovery window or the provider is unavailable.
Keep disabled sources deferred. Historical task-side outcomes do not authorize new
task writes; this flow completes only the still-missing knowledge outcomes.

Group representations by actual meeting identity: normalized title plus compatible event interval, corroborated by the source metadata. A shared title alone does not merge recurring meetings. One notification, document and local recording can describe the same event; keep their IDs but do not count them as independent evidence.

Prefer a complete meeting-notes document containing its transcript. Use a local recording for a missing or unusable representation, or to fill a specific coverage gap. Retain which representation supports each claim; do not silently replace old coverage with stronger claims than the new evidence allows.

## Local recording helper

The recording root, manifest/transcript/track filenames, interpreter and model are configured. The helper reuses an actual populated transcript before initializing transcription. A heading-only file does not count as a transcript.

```text
python3 <skill_dir>/scripts/hex_transcribe.py list
python3 <skill_dir>/scripts/hex_transcribe.py transcribe <recording-id>
```

Filter the listed manifest event timestamps to the requested window, retaining older unfinished receipts for recovery. `list` inspects metadata/text availability and does not start transcription.

If no usable text exists, transcribe the two configured audio tracks with the configured local runtime and already available model. The helper is finite work: the parent can run it as an asynchronous child while collecting independent sources, retaining its job/result path. Do not create a scheduler or make a timed-out child disappear from the run's coverage.

Keep microphone/system labels and known relative timing. They identify recording channels, not authenticated speakers: echo and shared audio can cross tracks. Names, speaker turns and commitments need corroboration or explicit uncertainty. Preserve source recordings, metadata and existing transcript files on failures.

If transcription is unavailable, retain a blocked receipt with the recording identity and path. No transcript is not a quiet meeting, and no model download is implied by running ingestion.

## Distill and save

Read the entire retained representation or label the exact partial scope. Extract facts, decisions as reported, reasoning, open questions and attributed commitments. Mark a commitment in the article; never create a task or a waiting-for entry.

One meeting may extend several existing articles. Preserve an outcome per extracted item and a recoverable source until every item is saved or deliberately skipped. A document ID is processed only after all of its items have verified outcomes. A successful transcript operation alone does not mean that the meeting reached the KB.

Use the parent record-write contract and [evidence format](routing.md). Keep event time separate from collection/transcription time; ASR does not independently confirm spelling, speaker identity or the truth of a claim.
