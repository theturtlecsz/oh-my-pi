# 0017 — Decision target binding

## Problem

1. A tier-3 owner signature covers the workspace, the decision, the action class, and the answer. It does not name the target the owner approved, so the signature does not say which bytes were approved.
2. Nothing says when that approval stops being valid, or that it can be used only once. A signature recorded for one target could be presented again, or presented after the owner meant it to lapse.
3. Decisions already signed under the previous message must keep verifying. Adding fields must not change the bytes of a signature that did not set them.

## Decision

1. **Signed fields.** `decision_signature_message` remains sorted compact JSON of `action_class`, `answer`, `decision_id`, and `workspace_id`. It adds `target_sha256` only when the decision recorded one, and `expires_at` only when the answer carries one. `expires_at` is that instant in UTC, `isoformat`. Omitting an unset key leaves today's bytes, and today's signatures, valid. `CreateDecisionPayload.target_sha256` is an optional lowercase hex digest of 64 characters. `AnswerDecisionPayload.expires_at` is an optional timezone-aware datetime. The signature is over the `target_sha256` recorded on the decision, not one supplied with the answer.
2. **Visible fields.** `DecisionView` returns `target_sha256` and `expires_at`; `AnswerDecisionResult` returns `expires_at` when the answer set one. Both are optional and absent on a decision that never set them.
3. **Single use.** An answer is accepted once. A second answer is `revision_conflict` / `decision_already_answered`. The approval that answer creates authorizes one action and is spent when that action runs.
4. **Expiry.** When the record has an `action_class` and a `target_sha256`, the answer must include `expires_at` (`approval_required` / `expires_at_required`), and `expires_at` must be later than server time (`approval_required` / `authorization_expired`). A decision that lacks `target_sha256` or `expires_at` never authorizes an action.
5. **Target recheck.** A signature over another target, or over another expiry, is `approval_required` / `owner_signature_invalid`. When an action runs, its current target digest is compared with the recorded `target_sha256`; a difference refuses the action.

## Consequences

- A tier-3 decision that carries `target_sha256` cannot be answered without a still-future `expires_at` covered by the owner's signature.
- A decision with no `target_sha256` keeps the previous signature bytes and the previous answer rule.
- No migration. The decision fields are rebuilt from the applied `create_decision` and `answer_decision` domain events; the single-use, expiry, and target-recheck enforcement lands with the persistence slice.
