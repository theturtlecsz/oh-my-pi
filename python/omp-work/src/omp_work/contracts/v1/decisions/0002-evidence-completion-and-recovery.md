# 0002 — Evidence, completion, and recovery

## Decision

Plan approval allocates a candidate. Every receipt binds work, revision, candidate, immutable payload/artifact hashes, issuer, and time. Verification and independent audit receipts additionally bind the finalized candidate hash and commit. A revision or candidate mismatch is historical and stale.

`pushed_branch` completion requires current-candidate plan evidence, verification, explicit independent `PASS` audit, and a remote-observed push receipt resolving to the candidate commit. Completion is recorded only after those checks. Missing or mismatched evidence leaves closeout intent recoverable and the work non-DONE.

In-Postgres receipts are capped at 1 MiB; larger content is content-addressed externally. Operational requirements: 24h RPO, 4h RTO, 90-day retention, encrypted off-host backups, and a monthly isolated restore drill.
