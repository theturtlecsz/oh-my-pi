# 0013 — Alarm signals recorded in the domain event log (OMP-406)

## Problem

1. Alarms, digests, and operational notifications require a durable, hash-chained event source to avoid split notification stores or unrecorded out-of-band alerts.
2. Several operational signals have no domain event representation on the WorkService ledger today: budget thresholds, budget overruns, safety check failures evaluated outside the WorkService, and newly discovered credentials or SSH keys.
3. Without a ledger-backed command to record these external signals, monitoring processes would either need a separate database, send untracked alerts directly, or emit unbounded, unstructured records that risk leaking credentials or sensitive tokens into logs.

## Decision

1. **One event stream via `record_alarm_signal`.** External signals are ingested directly into the hash-chained domain event log (`omp_audit.domain_events`) via a typed `record_alarm_signal` command under scope `work.mutate`.
2. **Canonical signal types.** Allowed signal variants are strictly constrained to:
   - `cost_threshold`: Approaching budget limits (e.g., 50% or 80% budget consumption).
   - `budget_exceeded`: Strict budget overrun (e.g., 100% threshold reached).
   - `safety_check_failed`: External safety gate or verification check failure.
   - `credential_appeared`: Detection of a new credential or SSH key in watched host locations.
3. **Bounded payload and aggregation semantics.** Payloads carry:
   - `signal`: One of the four literal signal variants.
   - `work_id`: Optional UUID. When provided, the work item must exist in the workspace (unknown work items fail closed with `invalid_request`), and the domain event aggregates under `work_id`. When omitted, the domain event aggregates under the workspace ID.
   - `subject`: Stripped string between 1 and 200 characters. Blank or whitespace-only subjects are rejected.
   - `detail`: Optional diagnostic text up to 500 characters (defaulting to empty string).
4. **No secrets policy.** Alarm signals record only metadata, identifiers, statuses, and digests (such as SSH key fingerprints, relative file paths, and SHA-256 content hashes). Private keys, bearer tokens, passwords, and sensitive byte contents must never be recorded in alarm subjects or details.
5. **Callers and authority.** Callers include budget monitoring processes, external safety checks, and credential watchers authorized with `work.mutate` capability.

## Consequences

- Contract version remains `work.omp.dev/v1`; `command_types` adds `record_alarm_signal` under scope `work.mutate`.
- Domain event readers (`GET /v1/workspaces/{workspace_id}/events`) can read and dispatch alarm signals directly from the single hash-chained event log without a secondary store.
- Requires owner approval attestation for the updated contract digest (OMP-406).
