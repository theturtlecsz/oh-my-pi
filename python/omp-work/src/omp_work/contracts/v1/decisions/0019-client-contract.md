# 0019 — Client contract

## Problem

1. External clients interacting with the workspace service need a stable, uniform client contract defining read and intent-relay operations without exposing internal store details or leaking sensitive diagnostics.
2. Owner intent must be securely relayable by automated controllers and external client agents while maintaining strict authority boundaries, designated controller privileges, and explicit cryptographic approvals.
3. Decision answering and scope broadening require unambiguous cryptographic signatures to prevent unauthorized actions and maintain end-to-end provenance.

## Decision

1. **Contract bundle.** Declare a `client_contract` block, version `client.omp.dev/v1`, within the `work.omp.dev/v1` contract bundle.
2. **Operations.** Expose exactly 17 client operations under `/v1/workspaces/{workspace_id}/client`:

| Name | Method | Path | Scope | Request | Response |
| --- | --- | --- | --- | --- | --- |
| project.list | GET | /v1/workspaces/{workspace_id}/client/projects | work.read, work.client | None | ClientResponse |
| project.context | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/context | work.read, work.client | None | ClientResponse |
| project.status | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/status | work.read, work.client | None | ClientResponse |
| project.decisions | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/decisions | work.read, work.client | None | ClientResponse |
| mission.status | GET | /v1/workspaces/{workspace_id}/client/missions/{mission_id} | work.read, work.client | None | ClientResponse |
| evidence.inspect | GET | /v1/workspaces/{workspace_id}/client/evidence/{receipt_id} | work.read, work.client | None | ClientResponse |
| stop.status | GET | /v1/workspaces/{workspace_id}/client/stop | work.read, work.client | None | ClientResponse |
| mission.submit | POST | /v1/workspaces/{workspace_id}/client/missions | work.client, work.mutate | SubmitMissionPayload | ClientResponse |
| mission.intake | POST | /v1/workspaces/{workspace_id}/client/mission-intake | work.client, work.mutate | DraftMissionIntakePayload | ClientResponse |
| stop.engage | POST | /v1/workspaces/{workspace_id}/client/stop | work.stop | StopReasonPayload | ClientResponse |
| mission.pause | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/pause | work.client | RelayOwnerIntentPayload | ClientResponse |
| mission.resume | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/resume | work.client | RelayOwnerIntentPayload | ClientResponse |
| mission.cancel | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/cancel | work.client | RelayOwnerIntentPayload | ClientResponse |
| mission.reprioritise | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/priority | work.client | RelayOwnerIntentPayload | ClientResponse |
| mission.scope.confirm | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/confirm | work.client | RelayOwnerIntentPayload | ClientResponse |
| mission.scope.edit | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/edit | work.client | RelayOwnerIntentPayload | ClientResponse |
| decision.answer | POST | /v1/workspaces/{workspace_id}/client/decisions/{decision_id}/answer | work.client | RelayOwnerIntentPayload | ClientResponse |

3. **Intents.** The seven relay operations correspond in order to seven distinct intents: `pause`, `resume`, `request_cancellation`, `change_priority`, `confirm_scope`, `edit_scope`, and `answer_decision`. A path id or intent that differs from the payload is invalid_request (HTTP 400).
4. **Principals and scopes.** Actor kind `client` uses dedicated capability files (never owner.json); `client_scopes` are `work.read`, `work.client`, and `work.stop`. The human owner calls operations with existing scopes; `owner_host_scopes` remain unchanged. A `work.stop`-only client reads the stop state through the existing `/stop` route, not `stop.status`.
5. **Mission kinds.** `MissionKind` is one of `research.run`, `engineering.execute`, `architecture.review`, or `change.review`, defaulting to `engineering.execute`, and is not a D29 material key.
6. **Transitions.** Reprioritise is a revision with a new priority (0–3); cancel sets status abandoned, pause sets paused, resume sets running.
7. **Designation.** The controller configuration `<config_dir>/owner-controller.json` stores `{workspace_id, controller_actor_id, owner_signature}`, owner-signed (namespace `omp-work-decision`, principal `owner`) over compact sort_keys JSON `{controller_actor_id, purpose: "designate_owner_controller", workspace_id}`. It is read per request; a missing or invalid file indicates no designated controller. Exactly one controller per file is supported.
8. **Instructions.** Every relayed intent requires a `RelayedInstruction` with `text` (1–8000 characters), `source_message_ref` (1–500 characters), `owner_authored: True`, and `received_at` timestamp. The service stamps the relaying actor.
9. **Relay authority.** The designated controller may relay unsigned intents. Any other client requires an owner signature over the canonical relay bytes. An unsigned relay from a non-controller is forbidden (HTTP 403) with diagnostic `not_designated_controller`. An invalid signature yields `approval_required` / `owner_signature_invalid`.
10. **Classified decisions.** For `answer_decision` on a decision with an `action_class` (DecisionActionClass, E11), every client, controller too, signs the OMP-403 `decision_signature_message`, not relay bytes. That signature covers the action_class, answer, decision_id, workspace_id, plus target_sha256 when recorded and expires_at when present. The controller may relay an answer to a decision without an action_class.
11. **Scope broadening.** When a decision broadens scope (with an approved scope, D29 material case c or undecidable; without one, the mission's project differs from the decision's, or a repository is not registered to that project), the owner signature over relay bytes is required even from the controller. Unsigned returns `approval_required` with diagnostic `broaden_scope`.
12. **State mismatches.** Stale revision or already-answered decision returns `revision_conflict`. Disallowed status transitions return `mission_transition_refused`. No new decision record is raised.
13. **Diagnostic keys and detail filtering.** Diagnostic keys (containing token, worker, or worktree, or named operation_id, request_id, or correlation_id, including budget token limits) are dropped from bodies and errors at every depth unless requested with `detail=true`, which returns them under the `detail` object.
14. **Stop operations.** `stop.engage` and `stop.status` execute the OMP-405 `engage_stop` command and stop read unchanged; pre-existing routes remain functional.
15. **Payload fields and relay bytes.** Relay operations accept only their defined fields besides intent, instruction, and optional owner_signature: `pause`, `resume`, and `request_cancellation` take `mission_id`; `change_priority` takes `mission_id`, `revision`, `priority`; `confirm_scope` takes `mission_id`, `decision_id`, `revision`; `edit_scope` takes those plus `draft`; `answer_decision` takes `decision_id`, `answer`, and optional `expires_at`. Any other non-null field is refused. Canonical relay bytes are UTF-8 `json.dumps({**payload.model_dump(mode="json", exclude_none=True, exclude={"owner_signature"}), "workspace_id": str(ws), "purpose": "relay_owner_intent"}, sort_keys=True, separators=(",", ":"))`.
16. **Client response.** Every client operation returns a `ClientResponse` object with `outcome` (`read`, `applied`, `replayed`, or `pending_approval`), optional `state`, `evidence`, `blockers`, `decisions`, `artifacts`, `operation`, `contract` (`client.omp.dev/v1`), optional `result`, and optional `detail`.

## Consequences

- Clients have a uniform, high-level interface conforming to `client.omp.dev/v1` for workspace interaction.
- The designated controller defined in `owner-controller.json` can execute routine relayed operations without per-request human signatures, while high-risk actions (action_class decisions and broaden_scope) retain mandatory owner cryptographic verification.
- Sensitive execution diagnostics remain redacted by default and are only exposed when explicitly requested via `detail=true`.
