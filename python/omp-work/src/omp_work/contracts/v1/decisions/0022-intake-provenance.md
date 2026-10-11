# 0022 — NSI-2 intake provenance contract closure

## Problem

The NSI-2 intake wire must be included in the work contract closure. API response
schemas must also be bound by the contract digest (E0166).

## Decision

The command closure includes `start_intake`, `register_intake_evidence`,
`record_intake_claim`, `record_intake_decision`, `supersede_intake_claim`,
`record_intake_issue`, `link_intake_criterion`, and `assess_intake`.

The read closure includes `GET /v1/workspaces/{workspace_id}/intake/{run_id}`.
In `contract.json`, this read immediately precedes
`GET /v1/workspaces/{workspace_id}/missions/{mission_id}` to preserve the
existing mission, health, event, and FK read ordering.

`api-schema.json` is a manifest binding (E0166), alongside this decision.
Manifest paths remain sorted and unique.

## Consequences

The contract digest binds the intake commands, read, API schemas, and provenance
decision. `Approval.issue` accepts `OMP-522`; owner approval remains a separate
owner action.
