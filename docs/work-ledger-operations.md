# Work Ledger operations

Initialize mode-0600 credentials, render the local unit, then start the isolated loopback-only PostgreSQL instance:

```sh
uv run --project python/omp-work omp-work ops credentials init
infra/work-ledger/install.sh
systemctl --user daemon-reload
systemctl --user enable --now omp-work-postgres.service
uv run --project python/omp-work omp-work ops bootstrap
uv run --project python/omp-work omp-work ops check
uv run --project python/omp-work omp-work ops health --json
```

## Contract changes

Contract approval requires explicit owner attestation via the interactive CLI:

```sh
uv run --project python/omp-work omp-work approve --issue <work-key>
```

This must be run by the owner in an interactive terminal after reviewing the printed digest and exact prospective JSON payload. Redirected or non-interactive input is refused, and agents must never mint `approval.json` from chat scope.

`omp-work approve` writes an `attestation` field into `approval.json`: a SHA-256 over the literal `omp-work approve` marker, the contract version, the contract digest, the issue, and `approved_at`. `omp-work validate --require-approval` recomputes it from those stored fields and rejects the file with `approval attestation missing or invalid` when the field is absent or does not match, so a hand-written or partially rewritten file fails even when every other field parses.

The attestation is not an unforgeable proof of owner intent, and one is not possible here: the owner, flood, and implementers all run as the same local user on one host, so no secret exists that an implementer cannot read, and CI could not verify a secret-keyed signature because it would have to hold the same key. The marker only proves the file matches what `omp-work approve` writes, not who ran the command. The enforcing control is provenance:

`bun scripts/approval-provenance.ts --base <rev>` runs in the CI `check` job on pull requests (`--base` is the PR's target branch). It fails when any commit in `<base>..<head>` adds or changes `approval.json` unless that commit is authored by `flood-owner` with a subject containing `owner step by flood`, `flood rebase_repair`, or `owner step by owner session`; for a merge commit it fails only when the merged blob matches no parent (a conflict resolution that rewrote the attestation).

An owner session that commits the approval itself produces the `owner step by owner session` form with the helper:

```sh
bun scripts/commit-contract-approval.ts --issue <work-key> [--digest <sha256>]
```

The helper stages `approval.json` and `packages/work-client/src/contract.ts` and commits them with author and committer `flood-owner <flood@localhost>` and that subject marker, so a session commit passes the same check the flood-applied `owner step by flood` commit does. The commit identity comes from the helper, not from the session's git config. It never writes, rewrites, or regenerates the approval bytes — `omp-work approve` owns the file, and the helper only stages what that command already wrote. A bare `git commit` of the approval is rejected by CI; this helper is the required commit form.

Implementers never write `approval.json` or the `WORK_CONTRACT_SHA256` digest in `packages/work-client/src/contract.ts`, including when resolving a rebase conflict.

### Staged digest approval for autonomous flood slices

When autonomous flood tasks alter the Work contract (`python/omp-work`), execution tests (`session-system/tests/auditor-runner.test.ts`) enforce contract safety: prospective contract changes pause execution before candidate freeze at `prospective contract digest <D> is not approved` until owner approval lands in `approval.json`.

Plans that split a contract change (`schema.json`) into one slice and owner approval (`approval.json`) into a second dependent slice are unmergeable: the contract slice cannot pass tests on its own, and the approval slice cannot run before the contract slice merges.

To allow contract-changing slices to reach `main` cleanly with owner hash-attestation while preserving worktree isolation (without the owner editing a flood worktree), the approval commit must ride with the contract change into a single merge:

1. **Autonomous stage**: The flood slice (e.g. `OMP-219-s01` or `OMP-283-s01`) implements the contract modifications (`python/omp-work/src/omp_work/v1/models.py`, `schema.json`, `api-schema.json`, and focused contract tests).
2. **Parked gate**: During the test gate, `oh-my-pi-test.sh` pauses at the contract check (`prospective contract digest <D> is not approved`). Flood automatically parks the slice as `needs_human` with digest `<D>`, preserving the unmerged task branch `<branch>` without consuming further retry attempts.
3. **Isolated owner attestation**: Outside flood's worktree (e.g. in the primary repository clone), the owner creates a temporary detached worktree pointing at `<branch>`:
   ```sh
   git worktree add /tmp/approve-<issue> <branch> --detach
   cd /tmp/approve-<issue>
   ```
4. **Interactive hash approval**: In an interactive TTY, the owner verifies the printed digest matches `<D>` and confirms:
   ```sh
   uv run --project python/omp-work omp-work hash
   uv run --project python/omp-work omp-work approve --issue <issue>
   ```
5. **Client constant synchronization**: The owner updates `WORK_CONTRACT_SHA256` in `packages/work-client/src/contract.ts` to `<D>`.
6. **Fork inventory refresh**: The owner updates and curates the fork inventory descriptions for the modified rows:
   ```sh
   bun scripts/upstream-inventory.ts --write
   bun scripts/upstream-inventory.ts
   ```
7. **Approval commit**: The owner creates the approval commit on the detached tree:
   ```sh
   git add python/omp-work/src/omp_work/contracts/v1/approval.json packages/work-client/src/contract.ts docs/upstream-fork-inventory.tsv
   git commit --author="flood-owner <flood@localhost>" -m "chore(contract): owner step by flood — approve v1 digest <D> (<issue>)"
   ```
   The `--author` and the `owner step by flood` subject marker are what `bun scripts/approval-provenance.ts` accepts; without them CI's `check` job fails the PR.
8. **Fast-forward task branch**: The owner records the commit SHA, returns to the primary repo, fast-forwards the flood branch with the approval commit, and removes the temporary worktree:
   ```sh
   APPROVAL_SHA=$(git rev-parse HEAD)
   cd -
   git -C /home/thetu/flood-repos/oh-my-pi-wt/<branch> merge --ff-only "$APPROVAL_SHA"
   git worktree remove /tmp/approve-<issue>
   ```
   *(If the flood worktree is not checked out, `git branch -f <branch> "$APPROVAL_SHA"` updates the ref directly.)*
9. **Retest**: The owner queues a retest in flood:
   ```sh
   python3 flood.py retest <branch>
   ```
   Flood re-runs `oh-my-pi-test.sh` on the worktree. With `approval.json`, `contract.ts`, and `schema.json` in agreement, the test suite and auditor runner pass. Flood proceeds through code review and fast-forward merges `<branch>` into `main`. The approval commit and contract changes land together in one atomic merge.

### Contract planning rule

Autonomous implementation plans must never split a contract change from its owner approval. A contract change belongs in slice `s01`. Slice `s01` is allowed to park at the contract gate; the owner applies the approval commit directly to `s01`'s branch using the staged digest flow above, allowing `s01` to merge into `main` with approval present. All subsequent implementation slices depend directly on `s01` and execute against an approved contract on `main`.

### OMP-219 plan following the flow

OMP-219 (`--queue orders umbrella items before child deliverables; skip_active_item`) follows the staged digest flow:

- **OMP-219-s01 (contract change & approval ride)**: Adds `skip_active_item` command/result types (`v1/models.py`, `v1/api_models.py`, `v1/service.py`, `contract.json`, generated schemas, decision 0008, `test_skip_active_item_contract.py`). Parks at unapproved digest `31b2741a0629421e857d6b2d0367574e2f268dc120a1bb905a8f8706ddf0dd6b`. The owner approves via the staged digest flow, committing `approval.json`, `packages/work-client/src/contract.ts`, and inventory to `OMP-219-s01`. Merges to `main` as one unified contract change.
- **OMP-219-s02 (store implementation)**: Implements atomic `skip_active_item` in `v1/store.py` (superseding open close attempts, leaving work item open, CAS-clearing focus, completing grant on final item) and `test_skip_active_item_store.py`. Depends on `OMP-219-s01`.
- **OMP-219-s03 (TypeScript transport)**: Wires `skip_active_item` through `packages/work-client/src/index.ts`, `backend.ts`, and `work.ts` dispatch, with transport tests. Depends on `OMP-219-s01` and `OMP-219-s02`.
- **OMP-219-s04 (queue dependency ordering)**: Makes `snapshotQueue()` Kahn-ordered in `work.ts` so eligible child items precede parents, with `execution-queue-order.test.ts`. Depends on `OMP-219-s03`.
- **OMP-219-s05 (host skip command)**: Implements owner `/execute skip <key> [reason]` in `host.ts` with ownership validation, clean-workspace assertions, and `execution-skip.test.ts`. Depends on `OMP-219-s04`.
- **OMP-219-s06 (changelogs & inventory)**: Updates `python/omp-work/CHANGELOG.md` and `packages/coding-agent/CHANGELOG.md`. Depends on `OMP-219-s01` through `OMP-219-s05`.

### OMP-281 lost-response skip recovery

A lost `/execute skip` response is recovered at the next `session_start` by exact identity (grant, version transition, work ID, position, reason, and skipped phase), then the claim is removed; mismatches are refused and leave the claim for the owner.

### OMP-283 plan following the flow

OMP-283 (`Record externally delivered work as DONE`) follows the staged digest flow:

- **OMP-283-s01 (contract change & approval ride)**: Adds `record_external_delivery` command, `EXTERNAL_DELIVERY` receipt kind, and result types (`models.py`, `api_models.py`, `service.py`, `contract.json`, generated schemas, `test_external_delivery_contract.py`). Parks at unapproved digest `dcfcf3bb88035b286c1b231064b0707198efd22f1edb6e242b79d14d27fc98ef`. The owner approves via the staged digest flow, committing `approval.json`, `packages/work-client/src/contract.ts`, and inventory to `OMP-283-s01`. Merges to `main` as one unified contract change.
- **OMP-283-s02 (store implementation & migration)**: Implements migration `0024_external_delivery_receipts.sql` (nullable candidate for external delivery), atomic transition in `v1/store.py` (marking item `DONE`, persisting receipt and history), append refusal, and `test_external_delivery_smoke.py`. Depends on `OMP-283-s01`.
- **OMP-283-s03 (PostgreSQL refusal & authorization coverage)**: Comprehensive refusal tests (unknown item, already-DONE, CANCELED, archived, revision conflict, boundary/invalid evidence) and scope boundary tests (`work.read`/`work.mutate` 403) in `test_external_delivery.py`. Depends on `OMP-283-s02`.
- **OMP-283-s04 (changelogs & verification)**: Updates `python/omp-work/CHANGELOG.md` and `packages/work-client/CHANGELOG.md` with final required-approval and full integration verification. Depends on `OMP-283-s01` through `OMP-283-s03`.


After owner approval is present, register compatibility before service restart:

```sh
uv run --project python/omp-work omp-work ops migrate
systemctl --user restart omp-work-service.service
uv run --project python/omp-work omp-work ops health --json
```

The final health result must show `live:true`, `ready:true`, and no alerts.

Provision and prove the dedicated backup target before enabling backup automation:

```sh
uv run --project python/omp-work omp-work ops backup provision-target
uv run --project python/omp-work omp-work ops backup verify-target
uv run --project python/omp-work omp-work ops backup create
uv run --project python/omp-work omp-work ops backup wal
```
Run the isolated restore drill after the first clean backup and monthly thereafter. It restores the latest completed logical backup into a disposable native PostgreSQL 18 instance, verifies every applied migration, and records the drill outcome in the source ledger:

```sh
uv run --project python/omp-work omp-work ops restore drill --reason clean-instance
uv run --project python/omp-work omp-work ops restore drill --reason monthly
```

Rotate a runtime role without putting credentials on argv:

```sh
uv run --project python/omp-work omp-work ops credentials rotate omp_work_app
```

Loss of primary is manual fencing: stop the service, preserve its PostgreSQL and WAL directories read-only, restore a verified complete backup into a fresh data directory, run `ops check` and `ops health --mode ready`, then repoint the service. The monthly drill proves the backup content and schema compatibility; it does not alter the primary.

## Drain gate

Run the drain gate before a deploy, before `systemctl --user stop omp-work-service.service`, and before a host shutdown:

```sh
uv run --project python/omp-work omp-work drain check
```

It lists the workspace missions still in status `running`. Exit codes:

- `0` — nothing is in flight; safe to proceed.
- `1` — at least one mission is in flight; the printed JSON line has `drained: false` and the mission records under `in_flight`.
- `255` — the check itself failed (database unreachable, credentials missing); stderr carries `drain: <error>`.

To drain, wait for each listed mission to finish, or have the owner pause it. Engaging the agent stop does **not** drain: a frozen mission stays `running`, so the gate keeps exiting 1. Run `drain check` again and proceed only once it exits 0. The deploy wiring is in `flood deploy-omp.sh` (owner step OMP-519-s07).

## Agent stop guards

`stop install-guards` writes `50-omp-agent-stop.conf` into each target unit's
drop-in directory. For a unit whose own file or existing drop-ins set
`KillMode=process` or `KillMode=none`, it also writes
`zz-omp-agent-stop-killmode.conf` (`KillMode=control-group`, which sorts last)
and prints `stop: <U>: overriding ...`. The override is required because those
kill modes leave the unit's cgroup running: in the OMP-447 drill the stop killed
only `flock` and the worker kept the lock. `flood.service` is overridden too, so
a stop ends its jobs.

```sh
python -m omp_work stop install-guards --unit flood.service --unit flood-operator.service --unit continuous-admit.service --interval 5
```

The live stop-button drill is `docs/runbooks/agent-stop-drill.md` (OMP-447). It mints the WebUI client capability with `ops capabilities client --name client` (scopes `work.read`, `work.client`, `work.stop`) and leaves Grok Bot credentials out of the drill.

### continuous-admit

Economy continuous_admit (sole durable life-owner), as of 2026-10-02 16:05Z.
`Restart=always`, 60 s. Drop-ins: `50-omp-agent-stop.conf` (guard) and
`killmode-process.conf` (`KillMode=process`). `ExecStart`
`run-admit-systemd.sh` -> `flock` -> `continuous_admit.py` (2026-09-20,
pre-flood "FLOOD-LB"). Every 15 s it imports `omp_work.parallel_streams` from
`/home/thetu/oh-my-pi/python/omp-work/src`, enqueues implementer/review jobs
there and tries to launch them. Last real launch 2026-09-23T19:13Z; since then
every tick ends `launch refuse ... no small ledger item available` (334
refusals logged). It calls `ps.claim`/`ps.release`/`ps.complete`, which current
`parallel_streams` does not define. It does no work for OMP today; flood does
that work.

Keep or remove: decided in OMP-537-s04, recorded on OMP-537 in the Work Ledger.

## Research campaigns

Run a Work Ledger research campaign on an item through from draft to concluded:

```sh
uv run --project python/omp-work omp-work research run --key <KEY> --definition <FILE> --client-config <client.json>
```

### Campaign definition schema and example

The definition file specifies research objectives, evaluation domains, trial harnesses, criteria thresholds, and optional per-trial timeouts:

```json
{
  "objective": "Every trial reaches score one",
  "domain": "machine_learning",
  "timeout_seconds": 60,
  "criteria": [
    {
      "metric": "score",
      "op": "gte",
      "threshold": 1
    }
  ],
  "trials": [
    {
      "harness": "trial.sh"
    }
  ]
}
```

- `objective`: description of the campaign hypothesis or goal.
- `domain`: research domain identifier (`machine_learning`, `engineering`, etc.).
- `timeout_seconds`: optional positive integer timeout in seconds per trial execution.
- `criteria`: non-empty list of criteria objects with `metric` (string), `op` (`gte`, `gt`, `lte`, `lt`, `eq`), and `threshold` (number).
- `trials`: non-empty list of trial specs with `harness` pointing to the script file (relative to definition or absolute).

### Client configuration and scopes

Client configuration JSON identifies the WorkService endpoint, workspace, and bearer credentials:

```json
{
  "base_url": "http://127.0.0.1:8000",
  "workspace_id": "00000000-0000-0000-0000-000000000000",
  "bearer_file": "/path/to/bearer.json"
}
```

When `--client-config` is omitted, defaults to `$XDG_CONFIG_HOME/omp-work/client.json` (or `~/.config/omp-work/client.json`).

The bearer file (`mode 0600`) must contain `actor_id` and authorize the scopes:
- `work.read`: fetch item and campaign state via `GET /v1/work-items/<key>/research`.
- `work.execute`: record trial observations via `RecordResearchObservationCommand`.
- `work.approve`: transition campaigns through lifecycle admission and conclusion.

### Trial execution and METRIC scoring

Trial harnesses execute in an isolated directory via `bash`. The harness emits metrics to stdout:

```sh
#!/bin/bash
echo "METRIC score=1"
exit 0
```

- Lines matching `METRIC <name>=<value>` are parsed as finite numeric metrics.
- The harness must exit 0 for completion. Any non-zero exit code is recorded as `execution_status: "crashed"` and refutes the campaign even if printed metrics satisfy criteria.
- Extracted metrics are scored against the definition's criteria.

### Campaign lifecycle states and conclusion

Campaigns transition through five sequential states:
1. `draft`: initial campaign metadata created.
2. `admitted`: content-addressed specs and components registered.
3. `running`: trial jobs dispatched to the native job worker.
4. `evaluating`: trials finished, evaluator verifies observations against criteria.
5. `concluded`: campaign finalized with `outcome` and `outcome_reason`.

The terminal `outcome` is:
- `supported`: all trials completed successfully and satisfied all criteria thresholds.
- `refuted`: one or more trials crashed, timed out, or failed to meet criteria thresholds.

### Database flags and credentials

Subprocess jobs execution communicates with the PostgreSQL job store using explicit database flags:
- `--config-dir <DIR>`: configuration root containing `credentials/omp_work_app` and `credentials/omp_work_migrator`.
- `--state-dir <DIR>`: runtime state directory.
- `--data-dir <DIR>`: persistent data directory.
- `--db-port <PORT>`: PostgreSQL listening port.

These four flags must all be set together or all be omitted (falling back to `OperationsConfig.defaults()`). Only the `omp_work_app` and `omp_work_migrator` role files in `<config-dir>/credentials/` are read; no host credentials or ambient PostgreSQL environment variables are used.

An optional `--harness-dir <DIR>` specifies where harness files are unpacked. If omitted, a secure temporary directory is created and cleaned up on exit.

### Command output and execution semantics

On conclusion, `research run` writes a single JSON payload to stdout and exits `0` for both `supported` and `refuted` outcomes:

```json
{
  "campaign_id": "c6a66ec2-f1e1-4547-8fa7-fdfa404987f7",
  "state": "concluded",
  "outcome": "supported",
  "reason": "criteria satisfied across 1 trial",
  "trials": [
    {
      "trial_id": "4bf77dd6-0e10-4f51-b956-655d91e60058",
      "execution_status": "completed",
      "metrics": {
        "score": 1.0
      }
    }
  ]
}
```

Exit codes:
- `0`: campaign successfully executed and concluded (supported or refuted).
- `1`: execution failure, unsettled jobs, or service refusal.
- `2`: invalid CLI arguments, unreadable files, or malformed campaign definitions.

### No resume

`research run` always creates and admits a new content-addressed campaign on the targeted work item. It does not resume or pick up existing running or incomplete campaigns.

## Authority and sealed epoch

The Work Ledger is the sole workflow authority (`work.omp.dev/v1`), operating locally on PostgreSQL. The cutover epoch is sealed; live operations run directly against the loopback WorkService. Linear history is preserved offline as static immutable exports, encrypted reports, and provenance mappings. Linear is never a fallback or recovery authority.
