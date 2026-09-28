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

## Inference routes

`python -m omp_knowledge.inference health --routes FILE` probes the declared
inference routes and reports, per role, which profile resolved and why.

A route file lists one entry per role (`embedding`, `reranker`, `generator`),
each with a `primary` profile and an optional `fallback`. A profile pins its
`provider` (`llama.cpp` or `order`), its `accelerator` (`gpu` or `cpu`), and — for
`llama.cpp` — its `model` and loopback `endpoint`. The `order` provider is a
local deterministic ordering and is only valid for the reranker; it declares no
model or endpoint. An `embedding` profile carries the pinned embedding identity
whose `generation_id` (the sha256 of its canonical JSON) must be identical
between a primary and its fallback, so a same-dimension model swap can never
silently serve a different generation.

Probing is one `GET {endpoint}/health`: a `llama.cpp` profile is healthy iff the
response is 200, and an `order` profile is always healthy without a network call.
Resolution returns the healthy primary, else the healthy fallback (recording the
primary's failure detail as the reason), else raises `route_unavailable` (503).
An invalid file raises `route_config_invalid` (400) — a duplicate role, an
`order` route outside the reranker, an embedding profile that is missing,
misplaced on a non-embedding route, or whose model disagrees with the route
model. A fallback whose embedding `generation_id` differs from its primary's
raises `route_incompatible` (400).

```json fk-routes
{
  "routes": [
    {
      "role": "embedding",
      "primary": {
        "name": "embed-gpu",
        "provider": "llama.cpp",
        "model": "Qwen3-Embedding-0.6B",
        "endpoint": "http://127.0.0.1:18081",
        "accelerator": "gpu",
        "embedding_profile": {
          "model": "Qwen3-Embedding-0.6B",
          "model_revision": "Q8_0",
          "dimensions": 1024,
          "pooling": "last",
          "query_prefix": "Instruct: Find code for this task\nQuery: ",
          "document_prefix": "",
          "normalize": true
        }
      },
      "fallback": {
        "name": "embed-cpu",
        "provider": "llama.cpp",
        "model": "Qwen3-Embedding-0.6B",
        "endpoint": "http://127.0.0.1:18084",
        "accelerator": "cpu",
        "embedding_profile": {
          "model": "Qwen3-Embedding-0.6B",
          "model_revision": "Q8_0",
          "dimensions": 1024,
          "pooling": "last",
          "query_prefix": "Instruct: Find code for this task\nQuery: ",
          "document_prefix": "",
          "normalize": true
        }
      }
    },
    {
      "role": "reranker",
      "primary": {
        "name": "rerank-gpu",
        "provider": "llama.cpp",
        "model": "Qwen3-Reranker-0.6B",
        "endpoint": "http://127.0.0.1:18082",
        "accelerator": "gpu"
      },
      "fallback": {
        "name": "rerank-order",
        "provider": "order",
        "accelerator": "cpu"
      }
    },
    {
      "role": "generator",
      "primary": {
        "name": "gen-gpu",
        "provider": "llama.cpp",
        "model": "Qwen3.8-27B",
        "endpoint": "http://127.0.0.1:18083",
        "accelerator": "gpu"
      },
      "fallback": null
    }
  ]
}
```

`health` prints one row per role (`role`, `primary`, `fallback`, `used`,
`reason`) and exits 0 when every declared role resolves, else 2.

```
python -m omp_knowledge.inference health --routes routes.json [--json]
```

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

