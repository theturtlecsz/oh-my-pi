# Changelog

## [Unreleased]

### Added

- PostgreSQL operational bootstrap, migrations, health checks, and encrypted backup commands for the Work Ledger.
- Authenticated loopback WorkService, typed clients, immutable work history, idempotent command handling, and closeout projections.
- Idempotent Linear importer with hash-verified staging, restartable relation/focus validation, dry-run reconciliation with encrypted parity artifacts, and atomic promotion that preserves local edits, retires import-owned label joins, and fails closed on conflicts or canonical drift (`ops linear-import stage|reconcile|promote`).

### Changed

- PostgreSQL backups now use the dedicated backup role to export every RLS-protected ledger table, and restore drills verify the latest completed backup in an isolated PostgreSQL 18.3 instance.
- Linear exports now use static read-only stream queries, encrypted immutable artifacts, redacted reconciliation summaries, and explicit scoped-OAuth or owner-managed personal-key authentication.
