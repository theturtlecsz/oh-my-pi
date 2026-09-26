# 0009 — Research campaign lifecycle, action vocabulary, and authority boundaries (R02-S2)

## Problem

1. `WorkService` durable research campaigns lacked lifecycle states beyond admission and cancellation, preventing expression of running experiments, pause/resume, dependency blocking, and evidence-backed outcomes.
2. Trial proposals omitted the canonical research action vocabulary, failing to capture the intended search semantics of policy outputs.
3. Policy fingerprints required explicit compatibility enforcement across lifecycle transitions to prevent arbitrary policy drift across paused, blocked, or concluding campaigns.
4. Authority boundaries between search policy, workflow controller, and owner-class approval required explicit specification to guarantee that untrusted execution status and exploratory observations cannot fabricate scientific validity or native candidate acceptance.

## Decision

1. **Campaign lifecycle and outcome semantics.** Campaigns support deterministic lifecycle transitions across `draft`, `admitted`, `running`, `paused`, `evaluating`, `blocked`, `concluded`, and `cancelled`. Concluded and cancelled are distinct, mutually exclusive terminal states; a concluded campaign cannot be cancelled. Concluded campaigns require owner-class authority (`work.approve`) and record an immutable outcome (`supported`, `refuted`, `inconclusive`, `resource_exhausted`, `externally_blocked`), a human-readable reason, and `concluded_at`.
2. **Blocked dependencies and resume integrity.** Nonterminal post-admission states (`admitted`, `running`, `paused`, `evaluating`) can transition to `blocked`, atomically capturing a structured dependency (`kind`, `ref`, `reason`) and the exact prior state (`blocked_from_state`). Blocked campaigns resume strictly to their prior state, or terminate via cancellation or narrow conclusion (`resource_exhausted` or `externally_blocked`). Cancellation of a blocked campaign atomically clears blocked-only fields.
3. **Canonical action vocabulary.** Trial proposals require a closed action vocabulary (`retrieve`, `draft`, `repair`, `refine`, `challenge`, `combine`, `evaluate`, `replicate`, `deepen`, `prune`, `synthesize`, `escalate`, `conclude`) and optional bounded reason. Proposals are accepted strictly when the parent campaign is in trial-accepting state (`admitted` or `running`).
4. **Policy-fingerprint compatibility.** The campaign's `policy_sha256` bound at admission serves as the campaign's compatibility anchor. Every lifecycle state transition, trial proposal, and conclusion must present the identical policy fingerprint; mismatched fingerprints are refused as `stale_evidence`.
5. **Authority boundaries and role contracts:**
   - **Search policy proposes:** Policy proposes exploratory actions via `propose_research_trial` (`work.execute`). Policy outputs carry no authority to launch jobs, grant execution leases, score results, or accept deliverables.
   - **Controller validates and drives lifecycle:** The workflow controller orchestrates non-approval transitions (`set_research_campaign_state`, `cancel_research_campaign`, `record_research_observation`, `bind_research_deliverable`) under `work.execute`.
   - **Owner-class admission and conclusion:** Campaign admission (`admit_research_campaign`) and conclusion (`conclude_research_campaign`) strictly require `work.approve`.
   - **Separation of execution status and scientific outcome:** Observations record raw, untrusted `execution_status` only (`completed`, `crashed`, `timed_out`, `canceled`, `unknown`). Untrusted observations cannot mutate campaign outcome. Scientific results, measurement validity, search disposition, and trusted evaluation receipts remain deferred until R06.
6. **Generic idempotency authority.** Generic `omp_control.idempotent_commands` and `omp_audit.domain_events` remain the sole replay identity. Retries under the same operation ID and request hash replay through the generic layer; mismatched payloads conflict; new operations with stale `expected_state` return `revision_conflict`.

## Consequences

- Database schema migration `0026_research_campaign_lifecycle.sql` widens state constraints, adds outcome and blocked dependency columns, enforces post-admission invariants, and maintains full backward readability for historical pre-0026 records.
- Contract digest changes; `approval.json` preserves prior owner approval bytes until separately authorized live qualification.
- Client bindings in `packages/work-client` reflect the extended vocabulary, commands, and results.
