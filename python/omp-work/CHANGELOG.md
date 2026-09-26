# Changelog

## [Unreleased]

### Added

- PostgreSQL operational bootstrap, migrations, health checks, and encrypted backup commands for the Work Ledger.
- Authenticated loopback WorkService, typed clients, immutable work history, idempotent command handling, and closeout projections.
- Idempotent Linear importer with hash-verified staging, restartable relation/focus validation, dry-run reconciliation with encrypted parity artifacts, and atomic promotion that preserves local edits, retires import-owned label joins, and fails closed on conflicts or canonical drift (`ops linear-import stage|reconcile|promote`).
- HOME-147 pre-cutover contract amendment: rich atomic `create_work_batch` (client refs, full revision fields, same-request relations, project binding) with full rollback, bounded evidence payload bodies with store-verified `payload_sha256`, `handoff` evidence that never satisfies completion blockers, and `finalize_candidate` binding an exact full-length commit to a named planned attempt with derived plan receipt.
- Candidate-bounded `work.candidate.read` capabilities with explicit non-empty `candidate_ids` allowlists; such principals can only read the allowlisted current candidate's workflow and can never mutate.
- Typed workflow reads returning current candidate, closeout intents, project, and full receipt projections; bounded tree reads (1000 items / 5000 relations).
- `ops capabilities init|candidate-reader` provisioning (mode-0700 directory, mode-0600 capability and loopback client-config files) and a loopback `omp-work-service` systemd unit in the work-ledger installer.
- Controlled Linear-to-WorkService cutover with source-watermarked imports, exact-backup receipts, immutable rehearsal/final evidence, fenced Linear writes, atomic authority/selector and managed-service activation, bounded pre-write rollback, post-write repair recovery, and read-only OAuth recovery exports.
- OMP-25 `/center`: bounded read-only recent-activity projection — `GET /v1/workspaces/{workspace_id}/activity?project_id=&limit=` returns newest-first applied receipt/close-proposal/completion event metadata (work key/title, project, normalized kind, timestamp; never payload bodies), `work.read` only, limit validated 1–20.
- `assess_bounded_intake` command (`work.approve`) evaluates a bounded intake draft and returns its semantic hash, rule bundle hash, readiness, and blocking questions without writing work items (OMP-266).
- Owner-initiated `skip_active_item` execution command defers one unsatisfiable queued grant item (superseding its open close attempts) without cancelling the work item or stopping the grant.
- Exact revision and receipt reads: `GET /v1/work-items/{key}/revisions/{selector}` (by number or revision id) and `GET /v1/receipts/{receipt_id}`, both `work.read`-only and workspace-scoped (OMP-279).
- Work-item enumeration: `GET /v1/workspaces/{workspace_id}/work-items` keyset-pages by `(created_at, work_id)` with a paired `after_created_at`/`after_work_id` cursor and 1–500 limit, past the 1,000-item `/tree` cap (OMP-279).
- Domain event cursor: `GET /v1/workspaces/{workspace_id}/events` pages `omp_audit.domain_events` by `sequence` under a per-workspace advisory-lock commit watermark, returning `watermark_sequence`, `next_after_sequence`, `has_more`; candidate-scoped principals refused (OMP-279).
- Knowledge contracts: strict `knowledge_contracts.py` models plus canonical code-snapshot and child-fact hash preimages, with golden vectors in `fixtures/knowledge_vectors.json` (OMP-279).
- Repository identity and manifest capture: read-only resolution of a checkout to an existing `omp_work.repositories` row (normalized URL, verified root commits) and capture of base/modified/untracked/deleted file bytes as a code snapshot manifest (OMP-279).
- Versioned structural code graph with candidate snapshot isolation: engine-neutral Enola extraction (`facts.jsonl`/`receipt.json`), a repository/snapshot namespace adapter over the complete `(repository, enola_repo, kind, name, file)` fact identity, staged publication with retained active snapshots, and a per-snapshot coverage manifest of extracted and skipped files (OMP-309).
- Claim-support evidence matrix (`report_evidence.py`): extracts material claims from a report draft with stable ids and locations, links each claim to top-k passages from the sources it cites, classifies every pair as supports/contradicts/neither through a stub or batched chat-model classifier, and stores a digest-verified matrix the report and its auditor read (OMP-300).
- Added record_external_delivery command closing externally delivered work with an evidence receipt (OMP-283).
- `record_fable_advice` command (`work.approve`) records advisor verification evidence bound to candidate and bounded intake semantic and rule bundle hashes (OMP-266).
- `publish_bounded_intake` command (`work.approve`, owner-only) ratifies one ready bounded intake draft into a work item, related OMP-249 edge, planned candidate, and `intake_publication` receipt in one serializable transaction (OMP-266).

### Changed

- PostgreSQL backups now use the dedicated backup role to export every RLS-protected ledger table, and restore drills verify the selected completed backup in an isolated native PostgreSQL 18 instance.
- Linear exports now use static read-only stream queries, encrypted immutable artifacts, redacted reconciliation summaries, and explicit scoped-OAuth or owner-managed personal-key authentication.
- Completion now requires a finalized candidate with a non-null full object ID, closeout review evidence, and a push receipt resolving to that exact commit; a negative latest audit permits a new planned-candidate attempt on the same revision, and revision changes still invalidate all prior candidates and receipts.
- Receipt storage now keeps the canonical caller payload body in `payload` with issuer/verdict/binding metadata in dedicated columns (migration 0008, additive).
- Restored the app role's readiness-gate grants (migration 0009, additive): 0005's omp_control revocation broke `python -m omp_work serve` startup — the health gate needs `schema_migrations`/`runtime_compatibility`/`operations_evidence` reads and the `readiness_probe` upsert (including SELECT for the ON CONFLICT arbiter).
- Contract approval moved to HOME-147 (`approval.json`); `work.omp.dev/v1` remains pre-cutover and non-authoritative until HOME-148.

### Fixed

- Parallel job completion now releases leases and reservations in the terminal transaction, preserves a winning terminal result, and can persist a bounded downstream closeout obligation for safe retry after daemon interruption. Cancellation waits for verified worker termination; checkpoint outcomes remain supported.
- Expired execution grants fence fresh grant-linked commands and continuations while preserving idempotent replay, delivery/auditor settlement, and explicit owner reconciliation before replacement admission.
- S2 job mirrors preserve `empty_soft`, reject foreign identities, retain namespace-scoped deletion tombstones, and compare row identities and resource contents. A separate additive jobs migration records mirror provenance; explicit legacy reconciliation requires a hash-pinned allowlist and current identity checks.
- WAL uploads retain the source spool until head-object sha256+size and evidence checks pass; restore drills reject a dump outside the backup prefix and record `passed:logical_restore:<reason>`.
- Cutover status now reports the persisted first-mutation request, the database rejects unpaired first-mutation stamps on every write path, and the recovery runbook covers deadline overruns and failed Linear credential revocation.
- OMP-123: Normalized `{"raw": report}` as a direct auditor transport envelope at the WorkService settle boundary, supporting task tool terminal yield payloads without loosening validation rules.
- `execution_grant_inactive` refusals are treated as not applied, so clients release the pending operation instead of reporting an unknown outcome.
- Generic `append_evidence` rejects reserved service issuers (`service`, `work-service/*`) before receipt lookup to prevent forged replays (OMP-266).
