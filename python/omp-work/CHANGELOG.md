# Changelog

## [Unreleased]

### Added

- Add research component identity registration, campaign compatibility manifest binding, and decision record 0011 (R02-S3b, migration 0038).
- Research campaign lifecycle, canonical trial action vocabulary, policy-fingerprint compatibility, and authority decision record (R02-S2, migration 0037). Added commands `set_research_campaign_state` and `conclude_research_campaign`, widened campaign state machine (`running`, `paused`, `evaluating`, `blocked`, `concluded`), added blocked dependency tracking with resumability to exact prior state, enforced closed trial proposal actions (`retrieve`, `draft`, `repair`, `refine`, `challenge`, `combine`, `evaluate`, `replicate`, `deepen`, `prune`, `synthesize`, `escalate`, `conclude`), and enforced policy-fingerprint compatibility on every lifecycle transition.
- Add bounded R02-S1 record-only research contract core: durable campaigns, trials, untrusted observations, and deliverable bindings (migration 0036).
- Stage budget resource classification and immutable quote binding (OMP-233): migration 0035 adds nullable resource and scope_id columns on budget_quotes preserving historical readability, fail-closed non-null resource and scope enforcement on reservation, deterministic byte-equivalent reservation replay identity, and structured constraint conflict detection.
- Immutable provider budget-quote authority with `quote_budget` command, append-only `budget_quotes` table, and optional `quote_id` reservation binding on `reserve_budget` (OMP-233).

- Immutable rate-card authority with `register_rate_card` command, workspace rate-cards query endpoints, and provider-account rate card version qualification and compatibility enforcement.
- Exact historical `WorkRevision` resolution by work item key + revision selector (revision number or UUID) via `GET /v1/work-items/{key}/revisions/{revision_selector}`, and full revision list via `GET /v1/work-items/{key}/revisions`.
- Exact immutable `EvidenceReceiptView` resolution by UUID via `GET /v1/receipts/{receipt_id}` enforcing `work.read` scope and workspace RLS.
- Keyset enumeration for work items via `GET /v1/workspaces/{workspace_id}/work-items` beyond 1000 items, ordered deterministically by `(created_at, work_id)` with bound base64url cursor, limit validated 1..500, and explicit exhaustion flag.
- Bounded domain events read via `GET /v1/workspaces/{workspace_id}/events` supporting cursor-based pagination with exclusive `next_sequence` resume value, bounded sequence windows (`after_sequence`, `through_sequence`), visible workspace head capture, and transaction-scoped advisory locks on event recording (`pg_advisory_xact_lock(hashtextextended('omp_audit:events:' || workspace_id, 0))`) guaranteeing no out-of-order native same-workspace commits (integer gaps from rollbacks or other workspaces remain legitimate).
- Keyset enumeration for repositories via `GET /v1/workspaces/{workspace_id}/repositories` ordered by `(created_at, repository_id)` with opaque workspace-bound cursor, limit validated 1..500, and explicit `next_cursor`/`exhausted` metadata, plus optional `repository_id` on `WorkItemView`.
- PostgreSQL operational bootstrap, migrations, health checks, and encrypted backup commands for the Work Ledger.
- Authenticated loopback WorkService, typed clients, immutable work history, idempotent command handling, and closeout projections.
- Idempotent Linear importer with hash-verified staging, restartable relation/focus validation, dry-run reconciliation with encrypted parity artifacts, and atomic promotion that preserves local edits, retires import-owned label joins, and fails closed on conflicts or canonical drift (`ops linear-import stage|reconcile|promote`).
- HOME-147 pre-cutover contract amendment: rich atomic `create_work_batch` (client refs, full revision fields, same-request relations, project binding) with full rollback, bounded evidence payload bodies with store-verified `payload_sha256`, `handoff` evidence that never satisfies completion blockers, and `finalize_candidate` binding an exact full-length commit to a named planned attempt with derived plan receipt.
- Candidate-bounded `work.candidate.read` capabilities with explicit non-empty `candidate_ids` allowlists; such principals can only read the allowlisted current candidate's workflow and can never mutate.
- Typed workflow reads returning current candidate, closeout intents, project, and full receipt projections; bounded tree reads (1000 items / 5000 relations).
- `ops capabilities init|candidate-reader` provisioning (mode-0700 directory, mode-0600 capability and loopback client-config files) and a loopback `omp-work-service` systemd unit in the work-ledger installer.
- Controlled Linear-to-WorkService cutover with source-watermarked imports, exact-backup receipts, immutable rehearsal/final evidence, fenced Linear writes, atomic authority/selector and managed-service activation, bounded pre-write rollback, post-write repair recovery, and read-only OAuth recovery exports.
- OMP-25 `/center`: bounded read-only recent-activity projection — `GET /v1/workspaces/{workspace_id}/activity?project_id=&limit=` returns newest-first applied receipt/close-proposal/completion event metadata (work key/title, project, normalized kind, timestamp; never payload bodies), `work.read` only, limit validated 1–20.

### Changed

- PostgreSQL backups now use the dedicated backup role to export every RLS-protected ledger table, and restore drills verify the selected completed backup in an isolated native PostgreSQL 18 instance.
- Linear exports now use static read-only stream queries, encrypted immutable artifacts, redacted reconciliation summaries, and explicit scoped-OAuth or owner-managed personal-key authentication.
- Completion now requires a finalized candidate with a non-null full object ID, closeout review evidence, and a push receipt resolving to that exact commit; a negative latest audit permits a new planned-candidate attempt on the same revision, and revision changes still invalidate all prior candidates and receipts.
- Receipt storage now keeps the canonical caller payload body in `payload` with issuer/verdict/binding metadata in dedicated columns (migration 0008, additive).
- Restored the app role's readiness-gate grants (migration 0009, additive): 0005's omp_control revocation broke `python -m omp_work serve` startup — the health gate needs `schema_migrations`/`runtime_compatibility`/`operations_evidence` reads and the `readiness_probe` upsert (including SELECT for the ON CONFLICT arbiter).
- Contract approval moved to HOME-147 (`approval.json`); `work.omp.dev/v1` remains pre-cutover and non-authoritative until HOME-148.

### Fixed

- Cutover status now reports the persisted first-mutation request, the database rejects unpaired first-mutation stamps on every write path, and the recovery runbook covers deadline overruns and failed Linear credential revocation.
- OMP-123: Normalized `{"raw": report}` as a direct auditor transport envelope at the WorkService settle boundary, supporting task tool terminal yield payloads without loosening validation rules.
