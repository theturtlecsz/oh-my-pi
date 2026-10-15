# Intake behaviour candidates — proposed rules (OMP-634 r1)

Status: preparation only. No labels/rules approval, frozen manifest, model calls,
benchmark results or qualification evidence exists here. Eight synthetic NSI-6
seeds do not constitute eight authentic requests or a completed reality check.
NSI-6 inputs and seals remain unchanged. No pass threshold is proposed.

## Provenance and import

`cases.json` retains each seed's exact input draft, request, source case and full
mutation provenance, including before/after values. `original_request` is the
unmutated ready-complete request, not a claim that a human submitted it. All
seeds are synthetic, including any upstream history-derived fixtures; never
relabel synthetic history as authentic. Stated omitted-* cases retain the source
sentence but omit its draft constraint/criterion. Silent-omitted-* cases remove
both source sentence and draft representation. Ambiguous-scope changes the draft
goal, not the original request. Complete control has no missing question target.

Only `input` (request plus evidence/draft) is model-visible. Never send case ID,
provenance, original pre-mutation text, gold, labels, rationale, challenge links
or adjudication to drafting agents. Claim-support judge challenges use only their
own claim/passage/context projection, with labels withheld.

Five explicit real-history slots await Chris. Import requires source_record
(immutable message/ticket/session reference), request_author, requested_at,
supplied_by=Chris, exact original_request, redactions (original/replacement and
reason, kept privately), mutations (path, before, after, reason and author),
model-visible request/evidence, and proposed gold using the seed fields. Preserve
unmodified original and distinguish any derived silent mutation from authentic
history. An empty slot, missing provenance or synthetic substitute cannot count.
Chris must adjudicate whether each never-mentioned target is materially required,
what question would resolve it, acceptable draft inclusion/exclusion, and readiness
rationale. Seed silent gold remains a proposal until then.

Each case reserves claim_support_challenges.source_path and IDs for OMP-631's
`evals/intake_support/v2/inputs/pairs.json`. That file is absent in this worktree:
IDs remain empty with blocked_missing_omp631_v2, not fabricated or borrowed from
v1. Before approval, link actual challenge IDs per case, verify referential
integrity and record why the claim/passage tests the relevant support boundary.
Unresolved references block completion.

## Proposed labelling

- Semantic question match requires an actual request for an owner decision or
  missing information about the gold target, with enough specificity to resolve
  it. A keyword, mention, generic “anything else?”, assertion or copied criterion
  does not count. For stated omissions, explicitly asking whether/how to restore
  the known requirement counts; restating it without a question does not.
- Record scan detection and draft inclusion separately. Scan detection needs an
  explicit identified gap/ambiguity with output evidence; it is not a question.
  Draft inclusion needs an actionable criterion with observable outcome preserving
  target scope, polarity and failure conditions. A mention in analysis or questions
  does not count as inclusion. Record initial and final draft separately, with
  exact output spans and owner answers; do not assume a question was answered.
- Proposed source coverage: every material request sentence must map to a criterion
  or an explicit, owner-approved exclusion. Otherwise refuse ready-for-ratification.
  Omitted-rollback/security/failure and ambiguous-scope are not-ready despite an
  empty unknowns list. Complete control is ready. Silent cases remain not-ready
  under proposed gold, pending Chris's materiality ruling; absence from source
  alone cannot prove that an unmentioned requirement exists.
- NSI-4 numerator: distinct eligible missing targets receiving a semantically
  matching question before ratification. Denominator: all Chris-approved eligible
  missing targets assigned to that route/family, including targets in failed or
  abstained runs. Multiple questions for one target count once. Complete controls
  have zero targets and are excluded; zero denominator is N/A. Report stated,
  silent and ambiguous strata separately, plus control unnecessary questions.
- NSI-5 false-ready: ready-for-ratification on a gold not-ready case. Report count
  over all assigned gold not-ready cases and separately over emitted readiness
  decisions. Abstention is not ready or a successful decision. Report ready control
  retention separately so blanket refusal cannot look like success.
- False-support: judge says supports for a linked challenge whose gold is
  contradicts or neither. Denominator is all assigned unsupported challenges;
  additionally report rate among emitted verdicts and supported-claim retention.
  Citation presence, topic overlap and draft inclusion do not establish support.
- Record abstentions, malformed output, timeouts, tool/provider failures and cap
  stops separately per dimension. Keep assigned cases in denominators; missing
  question/draft evidence earns no detection/inclusion credit. Never silently
  drop failures or treat them as correct support rejections. Report coverage and
  raw counts beside rates; unresolved adjudication is unscored, not a pass.

## Approval, freeze and later execution

Chris reviews case labels, challenge mappings and these rules before any freeze.
Record exact approved revisions, approver, timestamp and any disputes in a later
approval artifact; only then create a frozen manifest binding those bytes. There
is deliberately no such artifact now. Changes after freezing require a new
approved version; do not tune gold after seeing outputs.

Use the same frozen requests/evidence to score questions (NSI-4), false-ready
(NSI-5), false-support and paired current/typed (NSI-6). Per request and drafting
family, run both routes on identical inputs and owner-answer policy in separate
contexts on a disposable database. Keep route-specific adapters explicit; never
feed corrected gold drafts to typed route. Pair by request/family/repetition and
report both outcomes and paired differences, including one-sided failures.
Support challenges attach to the same case IDs rather than a separately selected
request cohort. Record model/provider/version, route, family, settings, prompt
revision, tokens (input/output/cached), cost, cap consumption and raw artifacts.

Reality-check report must cover at least eight requests including authentic and
silent-omitted cases across at least two drafting-agent families; show per request
whether a missing requirement was asked about and carried into the draft. Report
family × route and provenance/omission strata (complete, stated, silent, ambiguous,
authentic); do not pool away a failing family. Chris selects families and spend
cap before paid execution. Stop at cap and retain partial/failure records.

Waiting for Chris: authentic past requests; silent-omission adjudication; labels
and rules approval; credentials, at least two families and cap; final approach
for never-mentioned requirements based on benchmark numbers. Later qualification
evidence and Chris's decision belong in docs/programme/NSI-4-5-OWNERSHIP.md; this
candidate set grants neither NSI-4/NSI-5 qualification nor runtime readiness.
