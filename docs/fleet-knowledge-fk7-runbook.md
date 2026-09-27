# Fleet Knowledge FK-7 Local Runbook and Qualification Guide

This runbook defines the operator procedures, CLI interfaces, recovery workflows, and candidate qualification protocol for Fleet Knowledge v1 (FK-7).

> **Deployment Notice**: Activation follows the existing deployment procedure. This runbook performs local qualification and recovery operations only and executes **no live cutover** or service restarts.

---

## 1. State-Root Layout

A Fleet Knowledge state root contains the local SQLite stores and retained source artifacts that comprise the system's persistent state.

```
<state-root>/
├── learning.sqlite                    # Learning capture, procedures, evidence, and cleanup queue
├── context-bundles.sqlite             # Compiled context bundles and exclusion records
├── structural-publications.sqlite     # Structural code snapshot publications
├── publications.sqlite                # Repository snapshot publication metadata
└── sources/                           # Retained raw source trees and Enola inputs
    └── <repository_id>/
        └── <snapshot_id>/
            ├── facts.jsonl            # Ingested Enola structural facts
            ├── receipt.json           # Ingestion receipt and execution metadata
            └── manifest.json          # Git code snapshot file manifest
```

### Store Schemas and Table Breakdown

- **`learning.sqlite`** (owned by `LearningStore`):
  - `capture_cursor`: per-workspace domain event commit watermark cursor (`workspace_id`, `last_sequence`, `updated_at`).
  - `units`: capture units for completed work (`unit_id`, `workspace_id`, `event_id`, `state`, `attempts`, `retryable`, `error_code`, `lease_until`, ...).
  - `proposals`: generated lesson proposals pending or accepted (`proposal_id`, `unit_id`, `status`, `reason`, `lesson_json`, ...).
  - `procedures`: active or retired procedures (`procedure_id`, `fingerprint`, `status`, `current_version`, `title`, ...).
  - `procedure_versions`: immutable version history (`procedure_id`, `version`, `title`, `steps_json`, `preconditions_json`, `model`, `profile`, `source_json`, ...).
  - `uses`: records of procedure application to candidate revisions (`use_id`, `procedure_id`, `version`, `candidate_id`, `supply_id`, ...).
  - `outcomes`: verification verdicts bound to uses (`outcome_id`, `use_id`, `receipt_id`, `verdict`, ...).
  - `corrections`: narrowing or withdrawal records bound to counterevidence (`correction_id`, `procedure_id`, `action`, `from_version`, `to_version`, `receipt_id`, ...).
  - `cleanup_queue`: pending asynchronous cleanup actions resulting from corrections (`queue_id`, `procedure_id`, `action`, `attempts`, `done_at`, `last_error`, ...).

- **`context-bundles.sqlite`** (owned by `ContextBundleStore`):
  - `context_bundles`: deterministic compiled context packages (`bundle_id`, `work_id`, `work_key`, `stage`, `bundle_text`, `bundle_sha256`, ...).
  - `context_exclusions`: audit trail of dropped or excluded items (`bundle_id`, `ordinal`, `section`, `ref`, `reason`, `detail`).

- **`structural-publications.sqlite`** (owned by `StructuralPublicationStore`):
  - `structural_snapshots`: published code graph projections and coverage metadata (`workspace_id`, `repository_id`, `snapshot_id`, `namespace`, `state`, `projection_json`, `published_at`, ...).

- **`publications.sqlite`** (owned by `PublicationManager`):
  - `snapshot_publications`: high-level snapshot lifecycle records (`workspace_id`, `repository_id`, `snapshot_id`, `status`, `published_at`, ...).

- **`sources/` Tree**:
  - Contains immutable byte strings for each repository snapshot. Retained files are referenced by SHA-256 and checked for bit-for-bit identity across repeated imports.

### WAL Mode and Sidecar Management

All SQLite databases operate with Write-Ahead Logging (`PRAGMA journal_mode = WAL`). During active execution, `-wal` and `-shm` sidecar files exist beside each database file. Offline maintenance operations (`backup`, `rebuild`, `rollback`) ensure clean checkpoints, verify zero sidecars remain in snapshot copies, and preserve transaction atomicity.

---

## 2. Source Import CLI

The source import tool captures repository snapshots from retained Enola analysis artifacts without querying external databases.

```sh
python -m omp_knowledge.source_import \
  --state-root <path> \
  --workspace <uuid> \
  --repository-id <uuid> \
  --snapshot-id <sha256> \
  --checkout <path> \
  --enola-dir <path> \
  [--json]
```

### Options

- `--state-root`: Path to target Fleet Knowledge state root.
- `--workspace`: Target workspace UUID.
- `--repository-id`: Repository UUID matching the checkout remote.
- `--snapshot-id`: 64-character lowercase hex SHA-256 identifier.
- `--checkout`: Path to local git checkout root (must be git toplevel with remote `origin`).
- `--enola-dir`: Path to directory containing Enola outputs (`facts.jsonl` and `receipt.json`).
- `--json`: (Optional) Emit structured JSON output containing retained file SHA-256 digests.

### Semantics

- Captures git tree file list and commit ancestry using local `git` commands only.
- Atomically writes `facts.jsonl`, `receipt.json`, and `manifest.json` into `<state-root>/sources/<repository_id>/<snapshot_id>/`.
- Stages and publishes the snapshot into `structural-publications.sqlite` and `publications.sqlite`.
- **Resumable and Idempotent**: Rerunning against identical bytes finishes the snapshot without duplicate database rows. Differing bytes for the same snapshot ID trigger refusal with exit code `2`.

---

## 3. Learning Loop CLI

The learning CLI manages the event capture, lesson extraction, procedure supply, usage tracking, correction, and cleanup lifecycle.

```sh
python -m omp_knowledge.learning <subcommand> [options]
```

### Subcommands

#### `drain`
Scans Work Ledger domain events from the cursor, creates capture units for `complete_work` events, invokes the lesson generator, and records proposals.

```sh
python -m omp_knowledge.learning drain \
  --state-dir <dir> \
  --work-url <url> \
  --workspace <uuid> \
  --bearer-file <path> \
  --generator-url <url> \
  --model <model> \
  --profile <profile> \
  [--limit <limit>] \
  [--json]
```

- `--limit`: Maximum domain events scanned from the cursor (default: 50).
- `--json`: Emit full JSON details including units, states, errors, and proposal IDs.

#### `retry`
Retries failed capture units that are marked `retryable` (e.g. following a generator outage or recovered process crash).

```sh
python -m omp_knowledge.learning retry \
  --state-dir <dir> \
  --work-url <url> \
  --workspace <uuid> \
  --bearer-file <path> \
  --generator-url <url> \
  --model <model> \
  --profile <profile> \
  [--limit <limit>] \
  [--json]
```

#### `supply`
Retrieves active procedures matching candidate context (repository remote URL, project, work key).

```sh
python -m omp_knowledge.learning supply \
  --state-dir <dir> \
  --workspace <uuid> \
  --work-key <key> \
  [--project-id <id>] \
  [--cwd <path>] \
  [--limit <limit>] \
  [--json]
```

#### `use`
Records the application of supplied procedures to a candidate revision.

```sh
python -m omp_knowledge.learning use \
  --state-dir <dir> \
  --work-url <url> \
  --workspace <uuid> \
  --bearer-file <path> \
  --supply-id <id> \
  --candidate-id <id> \
  --receipt-id <id> \
  [--json]
```

#### `outcome`
Binds an independent verification outcome (PASS or failure verdict) to a recorded procedure use.

```sh
python -m omp_knowledge.learning outcome \
  --state-dir <dir> \
  --work-url <url> \
  --workspace <uuid> \
  --bearer-file <path> \
  --use-id <id> \
  --receipt-id <id> \
  [--json]
```

#### `correct`
Applies corrections to a procedure upon observing counterevidence. Supports narrowing preconditions or withdrawing the procedure completely.

```sh
python -m omp_knowledge.learning correct \
  --state-dir <dir> \
  --work-url <url> \
  --workspace <uuid> \
  --bearer-file <path> \
  --procedure <id> \
  --receipt <id> \
  --action <action> \
  [--precondition <precondition>] \
  --model <model> \
  --profile <profile> \
  [--json]
```

- `--action`: Either `narrow` or `withdraw`.
- `--precondition`: Precondition spec in `key:op:value` format (e.g. `repository:ne:example/regressed-repo`).

#### `cleanup`
Drains pending items from `cleanup_queue`, validating that procedure changes are committed before marking rows done.

```sh
python -m omp_knowledge.learning cleanup \
  --state-dir <dir> \
  [--limit <limit>] \
  [--json]
```

---

## 4. Inspection CLI & Web Server

Provides human-readable and JSON reporting across all stores in a state root, plus a local read-only web dashboard.

```sh
python -m omp_knowledge.inspection <subcommand> [options]
```

### Subcommands

#### `show`
Prints human-readable summaries or raw JSON for specific or all inspection sections.

```sh
python -m omp_knowledge.inspection show \
  --state-root <path> \
  [--section <section>] \
  [--json]
```

- `--section`: Filter to one section (`sources`, `evidence`, `procedures`, `acceptance`).

#### `serve`
Launches a read-only HTTP server bound strictly to loopback (`127.0.0.1`) serving an HTML dashboard and JSON API.

```sh
python -m omp_knowledge.inspection serve \
  --state-root <path> \
  [--port <port>]
```

- `--port`: Local port to bind (default: 0 for ephemeral).

---

## 5. State-Root Maintenance

Offline backup, exact-record rebuild, and rollback operations for state-root recovery and disaster management.

```sh
python -m omp_knowledge.maintenance <subcommand> [options]
```

### Subcommands

#### `backup`
Takes a consistent, point-in-time backup of all databases and `sources/`. Generates `manifest.json` with file hashes and `exact_records.json` containing exact row records.

```sh
python -m omp_knowledge.maintenance backup \
  --state-root <path> \
  --backup-dir <path> \
  [--json]
```

#### `rebuild`
Recreates all stores from `exact_records.json` into a clean target root via their native schema owners, verifies `PRAGMA integrity_check`, and asserts per-table SHA-256 digests match the backup manifest.

```sh
python -m omp_knowledge.maintenance rebuild \
  --backup-dir <path> \
  --target-root <path> \
  [--json]
```

#### `rollback`
Verifies all backup manifest checksums before touching the live root. Atomically renames the live root aside to `<state-root>.aside.<timestamp>`, then restores the verified stores and sources from the backup.

```sh
python -m omp_knowledge.maintenance rollback \
  --backup-dir <path> \
  --state-root <path> \
  [--json]
```

---

## 6. Failure-Case Recovery Table

The table below maps verified failure modes to their corresponding recovery commands and defending contract tests in `python/omp-knowledge/tests/test_fk7_failure_modes.py`.

| Failure Case | Symptom / Error Code | Recovery Command | Contract Test | Recovery Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Generator outage** | `generator_unavailable` (unit failed, `retryable=1`) | `python -m omp_knowledge.learning retry` | `test_generator_outage` | Running `retry` with a restored generator reprocesses the unit, creates the proposal once, and clears retryable status. |
| **Malformed output** | `malformed_generator_output` (unit failed, `retryable=0`) | `python -m omp_knowledge.learning retry` / `drain` | `test_malformed_output` | Corrupt model envelope fails safely; no orphan proposal is committed to the store. |
| **Crash between store writes** | Process dies during multi-table update | Automatic atomic rollback; rerun `python -m omp_knowledge.learning correct` | `test_crash_between_store_writes` | Single SQLite transaction rolls back completely; `current_version` and status remain unchanged. |
| **Stale cache** | Procedure modified or withdrawn after bundle compilation | `python -m omp_knowledge.learning correct` (action `withdraw`) | `test_stale_cache` | New compilation excludes withdrawn procedure; previous bundles reproduce byte-for-byte from bundle store. |
| **Restart / replay** | Unit left in `running` state when process crashes | `python -m omp_knowledge.learning drain` then `retry` | `test_restart_replay` | Expired leases transition to `state='failed'`, `error_code='dropped'` on next drain; `retry` completes them idempotently. |
| **Cleanup failure and retry** | External cleanup error (`attempts=1`, `last_error` populated) | `python -m omp_knowledge.learning cleanup` | `test_cleanup_failure_and_retry` | Row remains pending in `cleanup_queue` with procedure update intact; subsequent `cleanup` run finishes successfully. |

---

## 7. Candidate Qualification Procedure

The two-repository qualification procedure validates that Fleet Knowledge correctly isolates and maintains procedures across independent repositories (`oh-my-pi` and `media-discovery`).

```
Repository A: ssh://git@github.com/theturtlecsz/oh-my-pi
Repository B: ssh://git@github.com/theturtlecsz/media-discovery
```

### Qualification Sequence

1. **Source Ingestion**:
   Import snapshot A for `oh-my-pi` and snapshot B for `media-discovery` using `source_import`. Verify that both snapshots publish and retain files independently under their respective repository UUIDs.
2. **Learning Ingestion & Proposal Policy**:
   Drain domain events from the Work Ledger for each repository. Confirm that proposals citing missing or unknown evidence receipts are rejected with `invalid_citation`, while valid receipts generate active procedures scoped to their respective repository URLs.
3. **Cross-Repository Isolation Verification**:
   Execute `supply` in context of `oh-my-pi`. Verify that only `oh-my-pi` procedures are returned and no `media-discovery` procedures leak across boundaries. Repeat for `media-discovery`.
4. **Use and Outcome Binding**:
   Record candidate usage of supplied procedures using `use`, followed by `outcome` logging verification verdicts.
5. **Procedure Correction & Narrowing**:
   Apply counterevidence via `correct` with action `narrow` to add preconditions (e.g. excluding a regressed project). Verify procedure advances to version 2 while version 1 remains in immutable history.
6. **Cleanup Queue Processing**:
   Run `cleanup` to confirm the procedure's native state change is committed, transitioning queue items from pending to done.
7. **Inspection Audit**:
   Execute `inspection show` (or inspect via web dashboard). Verify all four sections (`sources`, `evidence`, `procedures`, `acceptance`) report consistent states across both repositories.

---

## 8. FK-7 Exercise Block

The fenced block below executes against `$STATE` and `$BACKUP`: it imports an Enola snapshot, inspects the initial state, creates a backup, rebuilds into a new root, changes a row to simulate divergence, rolls back from backup, and inspects again.

```sh fk7-exercise
set -euo pipefail

: "${STATE:?STATE environment variable must be set}"
: "${BACKUP:?BACKUP environment variable must be set}"

REBUILD_DIR="${REBUILD:-${STATE}.rebuilt}"
PRE_INSPECT="${PRE_INSPECT:-${STATE}.pre-inspect.json}"
POST_INSPECT="${POST_INSPECT:-${STATE}.post-inspect.json}"
WORKSPACE_ID="${WORKSPACE:-00000000-0000-0000-0000-000000000001}"
REPO_ID="${REPO_ID:-00000000-0000-0000-0000-000000000002}"
SNAPSHOT_ID="${SNAPSHOT_ID:-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa}"
CHECKOUT_DIR="${CHECKOUT:-.}"
ENOLA_DIR="${ENOLA_DIR:-python/omp-knowledge/tests/fixtures/A}"

# 1. Import snapshot into state root
python -m omp_knowledge.source_import \
  --state-root "$STATE" \
  --workspace "$WORKSPACE_ID" \
  --repository-id "$REPO_ID" \
  --snapshot-id "$SNAPSHOT_ID" \
  --checkout "$CHECKOUT_DIR" \
  --enola-dir "$ENOLA_DIR" \
  --json

# 2. Inspect initial state root
python -m omp_knowledge.inspection show \
  --state-root "$STATE" \
  --json > "$PRE_INSPECT"

# 3. Create consistent backup
python -m omp_knowledge.maintenance backup \
  --state-root "$STATE" \
  --backup-dir "$BACKUP" \
  --json

# 4. Rebuild from exact records into new target root
python -m omp_knowledge.maintenance rebuild \
  --backup-dir "$BACKUP" \
  --target-root "$REBUILD_DIR" \
  --json

# 5. Mutate a row in the live state root
sqlite3 "$STATE/structural-publications.sqlite" "UPDATE structural_snapshots SET state = 'mutated';"

# 6. Roll back the live root from backup
python -m omp_knowledge.maintenance rollback \
  --backup-dir "$BACKUP" \
  --state-root "$STATE" \
  --json

# 7. Inspect post-rollback state root
python -m omp_knowledge.inspection show \
  --state-root "$STATE" \
  --json > "$POST_INSPECT"
```

---

## 9. Recorded Results

This section is reserved for recording qualification runs by the system owner. No simulated or synthetic values are recorded here prior to the actual owner run.

| Qualification Step | Target Repository | Expected Contract | Recorded Exit Code / Digest | Timestamp (UTC) | Operator Sign-Off |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Source Import | `oh-my-pi` | Snapshot published, exit 0 | | | |
| Source Import | `media-discovery` | Snapshot published, exit 0 | | | |
| Learning Drain | `oh-my-pi` | Valid proposals accepted | | | |
| Learning Drain | `media-discovery` | Valid proposals accepted | | | |
| Procedure Supply | Both | Cross-repo isolation enforced | | | |
| Procedure Use & Outcome | Both | Receipts bound, verdicts recorded | | | |
| Procedure Correction | Both | Version incremented, cleanup queued | | | |
| Cleanup Processor | Both | Queue drained, attempts=1, done | | | |
| State-Root Backup | All stores | Manifest & exact records generated | | | |
| Exact Rebuild | Target root | Per-table SHA-256 digests match | | | |
| Rollback Verification | Live root | Replaced root kept aside, restored | | | |
| Multi-Store Inspection | All stores | Pre-change and post-rollback match | | | |

---

## 10. Activation Boundary

Fleet Knowledge qualification is purely local and declarative. Production activation requires explicit coordination with the Work Ledger service migration windows. **This runbook performs no cutover, no process restarts, and no external state mutations.**
