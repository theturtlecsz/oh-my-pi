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

`bun scripts/approval-provenance.ts --base <rev>` runs in the CI `check` job on pull requests (`--base` is the PR's target branch). It fails when any commit in `<base>..<head>` adds or changes `approval.json` unless that commit is authored by `flood-owner` with a subject containing `owner step by flood` or `flood rebase_repair`; for a merge commit it fails only when the merged blob matches no parent (a conflict resolution that rewrote the attestation).

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
   git commit --author="flood-owner <flood @localhost>" -m "chore(contract): owner step by flood — approve v1 digest <D> (<issue>)"
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

## Authority and sealed epoch

The Work Ledger is the sole workflow authority (`work.omp.dev/v1`), operating locally on PostgreSQL. The cutover epoch is sealed; live operations run directly against the loopback WorkService. Linear history is preserved offline as static immutable exports, encrypted reports, and provenance mappings. Linear is never a fallback or recovery authority.
