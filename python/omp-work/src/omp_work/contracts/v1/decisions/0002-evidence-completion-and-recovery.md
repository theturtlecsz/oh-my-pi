# 0002 — Evidence, completion, and recovery

## Decision

Plan approval allocates a candidate. Every receipt binds work, revision, candidate, immutable payload/artifact hashes, issuer, and time. Verification and independent audit receipts additionally bind the finalized candidate hash and commit. A revision or candidate mismatch is historical and stale.

`pushed_branch` completion requires current-candidate plan evidence, verification, explicit independent `PASS` audit, and a remote-observed push receipt resolving to the candidate commit, or to a same-branch tip that provably contains it (OMP-99).

Both `complete_work` and `complete_execution_item` commands require a typed `CompletionEvidence` claim and validate every field against immutable service-owned rows (receipts, audit manifests, auditor launches, candidates, attempts, and grants) before any completion mutation (OMP-247):
- `runner`: `CompletionRunnerIdentity` (`issuer: "work-service/auditor-settle"`, `launch_id: UUID`, `tool_call_id: str`, `task_sha256: 64-hex`, optional `judge_sha256: 64-hex | None`)
- `subject`: `CompletionSubject` (`work_id: UUID`, `revision_id: UUID`, `candidate_id: UUID`, `candidate_sha256: 64-hex`, `candidate_commit: hex`)
- `check`: `CompletionCheckDefinition` (`definition: "sealed_audit_manifest"`, `version: 1 | 2 | 3`, `manifest_id: UUID`, `task_sha256: 64-hex`)
- `result`: `"PASS"`
- `artifacts`: tuple of `CompletionArtifactReference` (`receipt_id: UUID`, `kind: "verification" | "audit" | "push"`, `payload_sha256: 64-hex`, optional `artifact_sha256: 64-hex | None`), containing exactly one reference of each kind and no duplicate receipt IDs
- `delivery`: `CompletionDeliveryBinding` (`repository: str`, `remote_url: str`, `remote_ref: str`, `candidate_commit: hex`, `remote_commit: hex`)

Completion is recorded only after those service-side checks pass. Missing, malformed, stale, foreign, or caller-invented evidence leaves closeout intent recoverable and the work non-DONE.

In-Postgres receipts are capped at 1 MiB; larger content is content-addressed externally. Operational requirements: 24h RPO, 4h RTO, 90-day retention, encrypted off-host backups, and a monthly isolated restore drill.
