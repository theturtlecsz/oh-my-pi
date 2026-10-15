# NSI-6 bounded drafting task

You are running the existing-plan seam of the intake skill above, once, non-interactively. The owner is not available. No tools exist in this session: do not publish, write to the ledger, ask questions, or invent an owner answer. Anything the source text does not settle is an unresolved finding, never a guess.

The user message carries one source text. Turn it into a bounded intake draft and list what is still unresolved.

Reply with ONE JSON object and nothing else (no prose, no code fence):

{
  "draft": {
    "archetype": "small_code_change",
    "spans": [{"id": "span-1", "quote": "<exact substring of the source text>"}],
    "goal": {"id": "goal-1", "statement": "...", "source_span_ids": ["span-1"]},
    "constraints": [{"id": "c-1", "statement": "...", "source_span_ids": ["span-1"], "key": "<snake_case subject>", "value": {"kind": "known", "value": "<string|number|boolean>"} or {"kind": "unknown"}, "polarity": "positive" or "negative"}],
    "unknowns": [{"id": "u-1", "statement": "...", "source_span_ids": [], "kind": "authority_or_dependency" or "routine_choice", "material": true or false}],
    "acceptance_criteria": [{"id": "ac-1", "statement": "...", "source_span_ids": ["span-1"], "observable_outcome": "...", "oracle": "automated_test" or "static_check" or "manual_inspection" or "external_receipt" or null}]
  },
  "findings": [{"kind": "contradiction" or "missing_oracle" or "unresolved_authority" or "ambiguity" or "other", "statement": "...", "claim_ids": ["c-1"]}]
}

Rules:
- Every span quote must be copied verbatim from the source text. Do not compute offsets or hashes; the runner derives them.
- Claim ids are unique across goal, constraints, unknowns and acceptance criteria. Every source_span_ids entry names a span you declared.
- Record each requirement the source states, including security, rollback and failure behavior. Do not drop a stated requirement and do not add one the source does not support.
- A value the source does not fix is {"kind": "unknown"} plus an unknown entry; set material true when the choice changes scope, authorization or product behavior.
- Conflicting statements stay as separate constraints with opposite polarity or values, plus a "contradiction" finding. Do not pick a side.
- An acceptance criterion with no stated way to check it keeps "oracle": null plus a "missing_oracle" finding.
- Never claim the draft is ready. Readiness is decided by the evaluator, not by you.
