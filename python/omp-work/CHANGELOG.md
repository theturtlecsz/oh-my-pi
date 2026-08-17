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

### Changed

- PostgreSQL backups now use the dedicated backup role to export every RLS-protected ledger table, and restore drills verify the latest completed backup in an isolated PostgreSQL 18.3 instance.
- Linear exports now use static read-only stream queries, encrypted immutable artifacts, redacted reconciliation summaries, and explicit scoped-OAuth or owner-managed personal-key authentication.
- Completion now requires a finalized candidate with a non-null full object ID, closeout review evidence, and a push receipt resolving to that exact commit; a negative latest audit permits a new planned-candidate attempt on the same revision, and revision changes still invalidate all prior candidates and receipts.
- Receipt storage now keeps the canonical caller payload body in `payload` with issuer/verdict/binding metadata in dedicated columns (migration 0008, additive).
- Restored the app role's readiness-gate grants (migration 0009, additive): 0005's omp_control revocation broke `python -m omp_work serve` startup — the health gate needs `schema_migrations`/`runtime_compatibility`/`operations_evidence` reads and the `readiness_probe` upsert (including SELECT for the ON CONFLICT arbiter).
- Contract approval moved to HOME-147 (`approval.json`); `work.omp.dev/v1` remains pre-cutover and non-authoritative until HOME-148.
