# 0009 — Native foundation reads and commit-order event cursor (OMP-279)

## Problem

1. `WorkService` exposed work state solely through current-revision views and capped aggregate views, lacking exact historical revision resolution, immutable receipt retrieval, and keyset item enumeration beyond 1000 items.
2. Concurrent transaction commits on different aggregates within the same workspace using PostgreSQL sequence allocation could allocate sequence $N$ in $T1$ while $T2$ commits $N+1$ first. A naive reader advancing its cursor to $N+1$ permanently misses event $N$ once $T1$ commits.
3. Consumers required a bounded, read-only event log stream and workspace repository enumeration without generic persistence utilities or uncoordinated SQL writers.

## Decision

1. **Transaction-scoped workspace advisory lock for events.** Centralized `PostgresWorkStore._record_event` acquires a transaction-scoped advisory lock on `hashtextextended('omp_audit:events:' || workspace_id, 0)` before reading the previous event hash, allocating a sequence number, or inserting the row. Held through commit, this guarantees that no later native event for the same workspace becomes visible before an earlier allocated native event transaction settles. The lock guarantee is strictly no out-of-order native same-workspace commits, NOT the absence of integer gaps (transaction rollbacks or other-workspace events leave legitimate sequence gaps that consumers handle naturally). This guarantee holds only when all event write paths use this central lock; there is no ordering guarantee for arbitrary raw SQL writers bypassing the store.
2. **Bounded commit-order event stream.** `GET /v1/workspaces/{workspace_id}/events` provides paginated access to `omp_audit.domain_events` filtered by workspace and ordered by `sequence ASC`. The initial query captures a fixed `through_sequence` snapshot equal to the workspace-local visible head (never global MAX), ensuring an immutable prefix without long-held transactions. Keyset cursor tokens encode `(workspace_id, after_sequence, through_sequence)` via strict schema validation (`EventsCursorPayload`) rejecting boolean, floating-point, or malformed values. Malformed, cross-workspace, negative, or future-bound cursors are rejected. The response exposes `next_sequence` as an exclusive resume value: consumers advance by passing `after_sequence=next_sequence` directly without incrementing; on an empty page, `next_sequence` remains unchanged. `work.read` scope is required; candidate-readers are refused with HTTP 403.
3. **Native exact reads and repository projection.** Added canonical endpoints:
   - `GET /v1/work-items/{key}/revisions/{revision_selector}` (exact historical `WorkRevision` by int or UUID)
   - `GET /v1/work-items/{key}/revisions` (complete revision list)
   - `GET /v1/receipts/{receipt_id}` (immutable `EvidenceReceiptView` by UUID)
   - `GET /v1/workspaces/{workspace_id}/work-items` (keyset pagination on `(created_at, work_id)`)
   - `GET /v1/workspaces/{workspace_id}/repositories` (complete keyset pagination on `(created_at, repository_id)` with opaque workspace-bound strict `AwareDatetime` cursor token, limit validated 1..500, and `next_cursor`/`exhausted` metadata)
   - Optional `repository_id` on `WorkItemView` matching existing database column.
4. **Export Protocol and Watermark Scope.** Keyset pagination over `omp_audit.domain_events` captures visible observations bounded by the snapshot `through_sequence` watermark, not an atomic full-state export. Complete state export protocols must first record a commit-safe event watermark (`through_sequence`), page through exact historical revisions (`GET /v1/work-items/{key}/revisions`), and replay events occurring since the recorded watermark. Keyset enumeration serves current observations only; no new generic export engine is introduced.

## Consequences

- The contract digest changes; `approval.json` preserves prior owner approval bytes until explicit live qualification under issue OMP-279. Do not claim activation or qualification passed.
- Activation is pending owner interactive OMP-279 approval plus service restart. Prior to activation, operators must drain old writers and in-flight transactions, and verify all event write paths use the central lock (no arbitrary raw SQL writer guarantee).
- TypeScript client constant `WORK_CONTRACT_SHA256` in `packages/work-client/src/contract.ts` is synchronized from `python -m omp_work hash`.
