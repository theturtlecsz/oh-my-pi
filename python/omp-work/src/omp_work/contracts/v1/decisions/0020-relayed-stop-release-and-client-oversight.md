# 0020 — Relayed stop release, client oversight, and standing budget

## Problem

1. Decision 0014 restricted `release_stop` strictly to direct owner principal callers (`actor_kind == "owner"`), preventing external controllers and client agents from relaying a verified owner's signed release instruction.
2. When agent stop is engaged, operational monitoring signals (such as `owner_approval_attempt` and alarm notifications) and relayed release intents were previously refused by the ledger stop guard, preventing audit logging of owner interactions and legitimate cryptographic release relays.
3. Grok Bot and other external monitoring callers were historically described with product-specific wording rather than formalizing them as standard client principals operating under capability-scoped client contracts (`actor_kind "client"` with push subscriptions to `ops.alarm` and `ops.digest`).
4. Mission intake budget admission required a direct `budget_policy` on the mission draft; missions lacking explicit draft policy were held even when a valid project standing budget existed, contrary to standing policy authorization (A4/D39).

## Decision

1. **Relayed stop release.** WorkService admits a relayed `release_stop` intent via `relay_owner_intent`. Unlike routine controller-relayed intents, `release_stop` strictly requires an owner cryptographic signature (`_signed` is True); an unsigned relay raises `approval_required` with `owner_signature_required`, and an invalid signature raises `approval_required` with `owner_signature_invalid`. Upon successful verification, the workspace aggregate records an applied `release_stop` domain event with the instruction text as reason, clearing the stopped state in `read_stop_state`.
2. **Commands permitted while stopped.** While agent stop is engaged, `allowed_while_stopped` permits:
   - Stop control commands: `engage_stop`, direct `release_stop`, and `relay_owner_intent` with intent `release_stop`.
   - Operational alarm signals: `record_alarm_signal`, allowing notification and audit of `owner_approval_attempt` and other critical signals.
   - Terminal execution transitions: `set_execution_state` targeting `paused`, `stopped`, or `canceled`.
   All other mutations remain refused with `agent_stop_engaged`.
3. **Alarm signal extensions.** `RecordAlarmSignalPayload.signal` adds `owner_approval_attempt`, recording owner interaction attempts while stopped or during protected gates.
4. **Client oversight and bot formalization.** External monitoring agents and bot integrations (including Grok Bot) operate as standard client principals (`actor_kind "client"`, provisioned via `provision_client`). Stop oversight requires the `work.stop` capability scope. Event delivery uses push subscriptions (`ops.alarm`, `ops.digest`), and budget notices map directly to `cost_threshold` and `budget_exceeded` alarm signals. This supersedes previous Grok-specific wording in decisions 0001–0019.
5. **Admission under standing budget (A4/D39).** Mission budget admission (`admit_mission_budget`) evaluates draft `budget_policy` first. If `budget_policy` is present and non-None, it wins and is admitted. If absent or None, a non-None `standing_budget` (passed from project provenance) is validated as `ItemBudget` and admitted. Only when neither is present is the mission held with an OMP-414 decision record.
6. **Stop state and paused jobs.** When stop is engaged: no new jobs are claimed or renewed; active jobs pause and resume through lease expiration and observation; control-plane actions are refused; host units halted by the watcher restart after release.

## Consequences

- `RelayIntent` includes `release_stop`; `RecordAlarmSignalPayload.signal` includes `owner_approval_attempt`.
- The contract manifest adds `decisions/0020-relayed-stop-release-and-client-oversight.md`.
- `schema.json` and `api-schema.json` reflect updated models.
