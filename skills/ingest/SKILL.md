---
name: ingest
description: Collect the previous day's meetings, reading queue and correspondence into an existing knowledge base. Use for a batch knowledge import or to resume an interrupted import; commitments are recorded as knowledge, without creating tasks.
---

# Ingest

Turn the configured sources into dated, attributable knowledge at one current owner. Save useful material automatically under this workflow's authorization. Keep source collection read-only; the parent owns knowledge-base writes and receipts.

## Configure and choose the window

1. Resolve this skill's directory and run `python3 <skill_dir>/scripts/ingest_config.py`. It reads `$SKILLS_CONFIG_DIR/ingest/config.json`. If the variable, file or required settings are missing or invalid, stop before collection or writes. [CONFIG.md](CONFIG.md) documents every key; `config.example.json` contains generic placeholders.
2. Work from `workspace_root`. Use `timezone` to calculate the previous local day, or the explicit date/window requested. `lookback_days` extends collection backward from today's local midnight. A source's event time and the time it was collected remain distinct.
3. Inspect open entries in `queue_path`, destination hints in `rulebook_path`, relevant KB indexes and any configured legacy recovery state. Use targeted reads/search for historical receipts instead of loading an entire run history into the parent. Resume unfinished items before applying the new window. A receipt's recovery path must actually exist; an old date does not discard unfinished work.
4. Run only `enabled_sources`. Source errors remain visible gaps; an unavailable account is not an empty inbox or a quiet day. Do not change account settings or install a new provider to make a run appear complete.

## Collect once and read fully

Use [sources](references/sources.md) for the selected collectors, [meetings](references/meetings.md) for meeting identities and transcription, and [Discord](references/discord.md) for the native browser reader. Load only the branches being used.

- Give each service/account one collector. Reuse its retained bodies for classification and reading instead of asking several workers to fetch the same thread.
- Apply the date window to newly discovered dated activity. Persistent capture queues such as bookmarks, starred mail and clips are processed by unprocessed identity; an old publication date does not discard a newly captured item.
- Retain source IDs, representation/revision, event date, acquisition time, body location/hash, pagination and coverage. Raw correspondence and media stay in `runtime_dir`, outside authored KB articles.
- Read-only workers may run in parallel on disjoint sources. Assign a private staging directory and a deadline from `source_deadline_seconds`; use the [reader brief](briefs/reader.md). Workers write only their staging outputs. The parent validates the full result before routing; a chat summary is not a complete handoff.
- Keep successful work if another source fails. A failed caption request does not establish that a video has no transcript; a snippet does not establish that a thread contains no useful content.
- Preserve incomplete items and their bodies/recovery pointers before a worker ends. Do not mark an enumerated or partially scanned source as fully read.

## Route every useful item

Apply [routing and evidence](references/routing.md) in this order:

1. Use a matching rule's destination hint when that destination still owns the topic.
2. Otherwise find the existing project/topic KB covering the subject, using the configured `kb_roots` and dated evidence.
3. If no project fits, use the configured `area_kb_paths` entry for work, personal or workspace/tooling knowledge. A new useful subject can become an article at that area level; it does not require a new project or a parking list.

An old rule's approval counter or ask/auto label is history, not a new approval gate. A retired destination is resolved through the same ladder. Update a routing hint when the user explicitly corrects the destination.

Save commitments and action-shaped statements at the relevant KB owner, attributed as commitments or proposals according to the source. Do not create tasks, events, waiting-for lists, daily priorities or a separate personal agenda. General planning, interactive discussion of an isolated link and weekly review are outside this batch workflow.

Skip only material positively identified as noise under the routing policy. Absence of a project is not noise. Record the source and reason for an actual skip.

## Commit one verifiable outcome

1. Give each source, representation and extracted item stable identities. Check both open queue entries and prior saved/skipped receipts; overlapping channels do not create independent corroboration.
2. Retain the complete distilled item and a recoverable source before writing its destination. Persist a pending queue entry before the KB write, so interruption cannot strand work in the conversation.
3. Read the owning article and its recent relevant evidence. Extend that article; create a new article only when no current owner covers the topic. Index rows remain short pointers. Keep a source's privacy boundary, uncertainty and event date.
4. Invoke `record_writer_command` from `workspace_root`, using the [record protocol](references/records.md) for article, index, queue and run-log records. Include routing dependencies as unchanged guarded entries. If the host has a different record-write contract, configure a compatible writer before running this skill.
5. Reconcile changed dependencies before retrying. A partially applied batch is not retried blindly: inspect actual destination markers and saved bytes, then complete only the missing outcome. Reuse a stable item marker such as `<!-- ingest:item:<stable-id> -->` to recognize a write that succeeded before its receipt.
6. Mark an item saved only after its content and destination are verified. A source with several extracted items is complete only when each item is saved or deliberately skipped. A missing body or failed write remains blocked with recovery, not processed.
7. Append the verified destination paths and source IDs to `run_log_path`. Keep the source untouched: this skill does not remove bookmarks, email labels/stars, messages, clips or recordings. Durable receipts prevent duplicate import without consuming the capture channel.

Queue receipts can use the host's existing format. Preserve historical rows and stable source IDs; new rows need identity, source/recovery pointer, target, outcome and verified save reference. Previously proposed rows are unfinished work, processed under the current automatic policy rather than re-presented as an approval queue.

## Close honestly

Report actual saved paths, skips and reasons, remaining recovery items and coverage by enabled source. Distinguish collection failures from unread retained material. There is no compulsory decision screen or score.

Do not announce a complete import while an assigned source or item is still incomplete. Retain staged results required for recovery; remove only this run's disposable scratch files and browser tabs after their durable copies are verified.
