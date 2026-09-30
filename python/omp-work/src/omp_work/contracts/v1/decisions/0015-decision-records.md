# 0015 — Decision records

## Problem

1. A mission must never be permanently abandoned, canceled, or materially redefined without an explicit policy basis or the owner's approval (OMP-414, ADR 0005). The blocker is nearly always a question only the owner can answer, and progress stalls until it is answered.
2. That stall is invisible and unbounded: nothing records *what* is being asked, *why* it blocks the mission, *what* it costs to delay, and *which* bounded options exist. Without a durable record there is no audit trail and no way to resume exactly where the mission paused.
3. High-risk authorizations (ADR 0005 tiers D35/D40/D41) need more than a plain answer: the answer itself is the owner's authorization and must be attributable to the owner alone — automation must never supply it.

## Decision

1. **Decision record.** `CreateDecisionPayload` captures one owner-facing decision: `decision_id`, `project_id`, optional `mission_id`, `question`, `why_it_matters`, `risk_of_delay`, a bounded option set of 2–10 unique `options`, `evidence_refs`, an optional `default_if_any`, and `risk_of_each_choice` whose keys must match `options` exactly. The optional `action_class` (see 2) marks high-risk decisions, and the optional `resume_state` records where the mission resumes once the owner answers. Blank `question`, `why_it_matters`, `risk_of_delay`, or any blank per-option risk is a validation error.
2. **Action classes.** `DecisionActionClass` enumerates the tier-3 high-risk actions that require explicit owner authorization, using the `action_tiers.TIER3` ids: merging to a protected branch (`merge_protected_branch`), production deployment (`production_deploy`), destructive infrastructure (`destructive_infra`), credential or security policy (`credential_change`), deleting persistent data (`delete_persistent_data`), billing or account ownership (`billing_change`), publishing externally (`publish_as_owner`), broadening repository scope (`broaden_scope`), secrets outside the envelope (`outside_secrets`), and disabling a safety mechanism (`disable_safeguards`). D30 makes a contract-version change tier 3 (`contract_hash`) and D40 makes an action no tier lists tier 3 (`unlisted`). A non-null `action_class` therefore identifies a decision whose answer is itself the owner's high-risk authorization.
3. **Who answers.** `AnswerDecisionPayload {decision_id, answer, owner_signature?}`. `create_decision` requires `work.mutate`; `answer_decision` requires `work.approve` and is forbidden (HTTP 403) unless `actor_kind == "owner"` — only the owner answers, and the reserved `owner_signature` carries the owner's signature on a tier-3 authorization (installed in a later slice). `GET /v1/workspaces/{workspace_id}/decisions` requires `work.read` and returns `DecisionsPage` with `DecisionView` rows; a `status` filter accepts only `pending` or `answered` (anything else is a 400).

## Consequences

- The contract bundle declares the `GET /v1/workspaces/{workspace_id}/decisions` read and the `create_decision` / `answer_decision` commands.
- A mission with a pending decision is simply paused; answering the decision returns its saved `resume_state`.
- Decision records are domain events (no migration). The indexed read is stubbed as `unavailable` in the live store until the persistence slice lands.
- Tier-3 decisions carry a non-null `action_class`; their answer requires the owner's signature, enforced in a later slice.
