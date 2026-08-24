# 0005 — Close-attempt authority (OMP-47)

Status: accepted (owner-approved plan, 2026-08-20). Clean breaking update
inside work.omp.dev/v1: OMP-47 explicitly replaces the closeout-intent path.

## Decision

One durable, ledger-owned **close attempt** replaces every piece of close
state previously spread across `omp_evidence.closeout_intents`, the session
gate booleans, the process-global audit bridge, the stop-hook reminder, and
`/summary` prompt prose.

1. **Table move, not copy.** `omp_evidence.closeout_intents` moves by OID to
   `omp_work.close_attempts` (`intent_id` → `attempt_id`); completed records
   survive verbatim; every pending owner decision maps to
   `closeout_requested` with `authorization_kind="legacy"`. No pending row is
   silently superseded by migration — only a new literal owner `/summary` or
   an owner-approved `/plan` (OMP-124) supersedes a non-terminal attempt.
2. **Immutable identity.** An attempt binds work, revision, plan receipt,
   finalized candidate (id/SHA-256/commit), owner session (id/start
   time/start commit), repository, range-diff SHA-256, starting dirty paths,
   and the authorization reference. A trigger enforces identity immutability,
   the legal transition table, monotonic counters, and terminal-row
   immutability. One partial unique index keeps exactly one live attempt per
   work item across `active`, `audit_ready`, `auditor_in_flight`, `audited`,
   and `closeout_requested`.
3. **States.** Non-terminal: active → audit_ready → auditor_in_flight →
   audited → closeout_requested. A completed invocation with no accepted
   report burns its launch and returns auditor_in_flight → audit_ready. A host
   failure before the auditor starts uses `cancel_auditor_launch`, returns to
   audit_ready, increments the explicit cancellation counter, and consumes no
   launch budget. Terminal: remediation_required, blocked, budget_exhausted,
   superseded, completed. `blocked` only ever records an accepted
   `VERDICT: BLOCKED` report.
4. **Sealed audit manifest.** `seal_audit_manifest(attempt_id,
   verification_receipt_id)` composes the complete five-section auditor task
   from stored ledger bytes only (plan receipt, acceptance criteria, attempt
   starting state, git-range-sha256 diff manifest, exact verification
   receipt) and stores it immutably with section hashes and a task SHA-256.
   The command accepts no model-supplied task text; `work get_work` renders
   the exact body.
5. **Bounded launches.** Reservation (`reserve_auditor_launch`) requires the
   sealed task hash byte-for-byte and consumes one of three launches; a task
   mismatch or explicit pre-start cancellation consumes nothing. Immutable
   launch rows retain reservation history while `cancelled_launch_count`
   separates physical reservations from budget-consuming launches.
   Settlement (`settle_auditor_launch`) owns transport normalization — raw
   canonical text, one direct `{"report"}`/`{"text"}`/`{"raw"}` object
   (identifying `raw` as the task tool's terminal yield body rather than a general
   recursive envelope), one JSON string encoding that direct object, or one bare
   `<output>` wrapper; nested wrappers remain refused. The direct object may carry
   one optional string `"verdict"`
   key (OMP-67, incident 2026-08-21): it is decoration, never authority — a
   value contradicting the report's own VERDICT line refuses as
   `report_wrapper_verdict_mismatch` before section validation, and a present
   non-string value (including null) refuses as `report_wrapper_invalid`.
   VERDICT starts at canonical byte 0, and each attempt accepts
   at most two reports. Accepted reports mint the only legal `audit` evidence
   receipts (`independent=true`, issuer `work-service/auditor-settle`);
   external `append_evidence kind="audit"` is refused. Drift at settle
   supersedes the attempt and inserts no receipt.
6. **Typed refusals.** Every expected gate failure returns
   `status="refused"` plus an immutable `close_attempt_events` row carrying a
   stable reason code, legal next actions, remaining budgets, fresh-
   authorization requirement, and canonical rendered text. Refused results
   are stored idempotently like applied ones. Exceptions remain only for
   unidentifiable aggregates, envelope/scope failures, and post-mutation
   invariant violations (which roll back).
7. **Receipted delivery.** Events with `requires_delivery` block
   `request_closeout` and `complete_work` until an append-only
   `checkpoint_deliveries` row records `delivered` (or an owner-authorized
   `waived` over a `failed` one) — computed per work item, so superseded
   attempts cannot strand owner-visible debt.
8. **Completion.** `request_closeout` requires the audited live attempt;
   `complete_work` requires `attempt_id`, a fresh single-use `/done`
   authorization reference, and completes attempt + work (+ validated
   same-session children, decision OMP-52) atomically.

## Consequences

- `CloseoutIntentView`, the intent write path, and the TS audit bridge are
  removed with no alias or compatibility shim.
- The auditor gate in session-system reduces to reserve/settle transport
  around service authority.
- The contract digest changes; owner approval of the exact new digest is a
  mandatory second gate before `approval.json` is updated (plan §8).
