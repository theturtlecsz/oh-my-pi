# omp-knowledge

Fleet Knowledge service foundation powered by Cognee.

## Learning capture CLI (FK-6)

`python -m omp_knowledge.learning` exposes the learning lifecycle subcommands
`drain`, `retry`, `supply`, `use`, `outcome`, `correct`, and `cleanup`.

```
python -m omp_knowledge.learning drain \
  --state-dir <dir> --work-url <url> --workspace <uuid> --bearer-file <path> \
  --generator-url <loopback url> --model <model> --profile <profile> [--limit N] [--json]
```

`--limit` bounds the number of **domain events scanned from the cursor in this
drain**, not the number of units processed. Capture queues a unit only for the
events whose type is captured (`complete_work`, `complete_execution_item`),
so a drain over a store whose first matching event is far past the cursor can
queue zero units at `--limit 5`. Page further by calling `drain` again: the
cursor advances to the watermark of each scanned page.

Each `complete_work` or `complete_execution_item` unit's trace carries the
finished item's own execution record (title, acceptance criteria, receipts, and
close history), read from the WorkService over the reads the frozen contract
already declares. When the item cannot be read the trace carries
`"work_record": null` and the unit still drains — an unavailable record
degrades one unit's evidence, never the capture.

`--json` output lists, per unit, `unit_id`, `event_id`, `state`, `attempts`,
`retryable`, `error_code`, `proposal_ids`, `model`, `profile`, and
`event_sequence` (the source domain-event sequence the unit came from).

## Cleanup processor (FK-7)

`cleanup` drains pending rows from `cleanup_queue` using `NativeCommittedTarget`,
which confirms that each procedure's native correction or withdrawal is committed
before marking the row done.

```
python -m omp_knowledge.learning cleanup \
  --state-dir <dir> [--limit N] [--json]
```

Each pending row is processed in its own transaction:
- On success: marks the row done (`done_at` timestamp), clears `last_error`, and increments `attempts`.
- On failure: stores `"<ExcType>: <msg>"` in `last_error`, increments `attempts`, and leaves the row pending for retry.

Exits 0 when every processed row is done, and 1 if any row failed.

## Runbook (FK-7)

For local qualification, offline maintenance, failure-mode recovery, and the runnable exercise block, see [docs/fleet-knowledge-fk7-runbook.md](../../docs/fleet-knowledge-fk7-runbook.md).

