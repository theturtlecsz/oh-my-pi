# 0016 — Mission intake confirmation

## Problem

1. New or materially changed mission scope requires the owner's confirmation (D29, OMP-426). Routine work inside an already approved mission or a standing project mandate does not. Nothing in the contract tells those two apart, so a caller can file an objective, acceptance criteria, and constraints as if they were already settled.
2. The owner's answer is not a free-form string. Confirm, reject, an edited draft, and a note are different acts. A rejection must abandon the mission, and a note must not be applied as a new draft.
3. An answer that is not tied to the message the owner actually sent cannot be audited, and automation must not be able to supply it.

## Decision

1. **Routes.** `POST /v1/commands` carries both commands. `draft_mission_intake` requires `work.mutate`. Its result `outcome` is `clarify`, `held`, `awaiting_owner`, or `proceeded`. `proceeded` may name a `basis` of `approved_mission` or `standing_mandate`: routine work inside an approved mission or a standing mandate continues without another confirmation. Anything else waits. `answer_mission_draft` requires `work.approve` and is forbidden (HTTP 403) unless `actor_kind == "owner"`.
2. **Draft shape.** `DraftMissionIntakePayload {mission_id, base_revision, intake, scope, instruction?}`. `base_revision` is an integer ≥ 1, or absent on the first draft. `intake` is a `BoundedIntakeDraft`. `scope` is `MissionIntakeScope`, a `MissionDraft` without `objective`, `acceptance_criteria`, and `constraints`. Sending any of those three on `scope` is a validation error (HTTP 400). `instruction` is an optional `OwnerInstruction`.
3. **Answers.** `AnswerMissionDraftPayload {decision_id, mission_id, revision, answer, instruction}`. `revision` is an integer ≥ 1. `instruction` is required; an answer without it is a validation error (HTTP 400). `answer` is a union on `kind`:
   - `option` — `option` is `confirm` or `reject`. Any other value, including `maybe`, is a validation error (HTTP 400). **Reject abandons** the mission. It does not approve the draft and it does not leave the draft waiting.
   - `edited_draft` — `draft` is a full `MissionDraft`, the owner's replacement, including the objective, acceptance criteria, and constraints that `scope` itself may not carry.
   - `note` — `text` is 1–8000 characters. **A note never applies.** It does not apply the intake, the scope, or a mission draft. The mission is not approved and not abandoned by the note.
4. **Provenance.** `OwnerInstruction {text, provenance}`. `text` is 1–8000 characters. `InstructionProvenance {channel, message_ref, received_at}`: `channel` is 1–100 characters, `message_ref` is 1–500 characters, and `received_at` is when that instruction was received. The answer's instruction is required and is the owner's words for this decision.
5. **Results.** `DraftMissionIntakeResult` returns the `outcome`, the blocking `questions`, an optional `mission` view, an optional `decision_id`, and the optional `basis`. `AnswerMissionDraftResult` returns `outcome` of `approved`, `rejected`, or `noted`, the `mission` view, an optional `next_decision_id`, and the `instruction`.

## Consequences

- The contract bundle declares `draft_mission_intake` and `answer_mission_draft`. `Approval.issue` includes `OMP-426`.
- No migration. The live store reports these commands `unavailable` until a later slice applies confirm, abandon-on-reject, and note-does-not-apply. The shapes and the rules above are the contract that slice must not violate.
