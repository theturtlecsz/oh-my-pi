# Jev decision-classifier review (2026-09-25, revision 2)

Report only. No code changed. Revision 2 extends the first pass with a full
roadmap review and adversarial verification of every candidate.

## Scope and method

Sources reviewed:

- Code: `packages/*/src` (coding-agent, agent, ai, mnemopi, metaharness),
  `session-system/`, `python/omp-work`, `python/robomp`, `packages/work-client`.
- Roadmap: `MASTER.md`, `docs/programme/*`, `docs/adr/*`, `docs/report/*`,
  `CONTEXT.md`, the untracked `OMP_*.md` files at the repo root,
  `cross-cutting-principles.md`, `docs/local-models.md`, `docs/advisor-watchdog.md`.
- Not reviewed: artifacts under `~/.codex` and `~/.local/state` that MASTER
  links to, and the v9.1 Fleet/WFM archive the audit inventory cites. Those are
  references, not repo content.

Method: inline reading of every decision-shaped section, plus a multi-agent
sweep (ten roadmap readers over line-bounded chunks, five code angles). The
sweeps produced 91 candidates. Each candidate was then attacked by two
independent verifiers with different lenses (decision shape; simpler rule or
Jev-blocked), and a completeness critic checked for missed sources. Five
candidates survived both lenses. The critic found one unswept package
(`python/robomp`) and one class of wrong refutation (claim-support judgments);
both are folded in below after I checked the cited lines myself.

Jev primitives, for reference: **Choice** (one of N fixed options, with
probabilities and confidence), **Score** (position on ordered levels), **Noul**
(probability a statement is true). Text-only state, many questions per call,
70–500 ms, input $0.042/MTok, output free, no string generation. Early access
as of 2026-09-15. Endpoint `POST https://api.typesafe.ai/v1/systemone`,
`model: "jev-latest"`.

## Verdict summary

| # | Site | Verdict |
| --- | --- | --- |
| 1 | Auto-thinking difficulty classifier (code) | **Replace** the online backend |
| 2 | Unexpected-stop classifier (code) | **Replace** the online backend |
| 3 | robomp issue triage (code, `python/robomp`) | **Add a pre-filter**; the in-session classification stays |
| 4 | Hypothesis tournament pairwise judge (roadmap, R11 §8.3) | **Design-time fit**; nothing built yet |
| 5 | Per-(claim, passage) semantic-support link (roadmap, §9.4, §13.1, R08, R17) | **Design-time fit**; nothing built yet |
| — | Conventional commit type/scope (code) | Withdrawn from revision 1 |
| — | Memory extraction `NO_FACTS` pre-gate (code) | Withdrawn from revision 1 |
| — | 84 other roadmap and code candidates | Rejected; reasons in the tables below |

## 1. Auto-thinking difficulty classifier — replace online backend

`packages/coding-agent/src/auto-thinking/classifier.ts`, caller
`session/model-controls.ts:616`. Runs before every user turn when thinking
level is `auto`, under a 4000 ms abort timeout with fallback to the provisional
level. The online path sends the preprocessed prompt (text only, capped at
2000 chars by `tiny/message-preproc.ts`) to the `tiny`/`smol` chat model, asks
for one keyword, and scrapes it back with regexes (`parseDifficultyLevel`,
lines 186–205). It needs `maxTokens: 4096` because some backends emit a
thinking preamble despite `disableReasoning` (issues #4355, #8610). The local
ONNX path uses a coarser 3-bucket scheme because sub-2B models cannot do
4-way ordinal reliably (`docs/local-models.md`).

Both verifiers confirmed: pure ordinal decision, no prose output, evidence fits
in text, side call rather than agent turn, no deterministic rule exists (the
only heuristic, `resolveProvisionalAutoLevel`, is a static per-model default).

Proposed request, one call:

```json
{
  "state": "<preprocessed user prompt>",
  "questions": {
    "difficulty": { "type": "score",
      "instructions": "How much reasoning effort does this coding request need?",
      "criteria": [
        "Trivial or mechanical: rename, typo, one-line edit, direct factual question",
        "Localized change needing some reasoning: small feature, one-place bug fix",
        "Non-trivial: multiple files or callers, real debugging, moderate design decision",
        "Deep or open-ended: subtle concurrency/algorithmic problem, cross-system reasoning, ambiguous requirements, large risky refactor"
      ] },
    "no_repro":     { "type": "noul", "instructions": "There is no reproduction or failing test to work from" },
    "irreversible": { "type": "noul", "instructions": "The request involves an irreversible or data-loss operation" },
    "live_cutover": { "type": "noul", "instructions": "A live system must keep working correctly during the change" }
  }
}
```

Mapping in code: `score` rounds to `low/medium/high/xhigh`; `max` only when
score is at the top level, any of the three Nouls clears a threshold, and the
ceiling allows it. This reproduces the prompt's conjunctive `max` rule as code
instead of prose. Keep `clampAutoThinkingEffort`, the
`providers.autoThinkingMaxEffort` ceiling, the `ultrathink` bypass, the
timeout and the fallback. Low `confidence` falls back to the previous resolved
level. The current prompt's "if torn, choose lower" bias becomes a probability
threshold.

What goes away on the online path: both keyword parsers, two rendered prompt
variants, the 4096-token budget and its comment block, the
`retryTransientCompletion` wrapper. The local ONNX path stays unchanged as the
offline opt-in.

Residual risk: early access, so Jev can only be an additional
`providers.autoThinkingModel` value beside `online` and the local keys, not
the default. Score calibration on this taxonomy is untested; compare against
the current smol model on a small labeled set before switching.

## 2. Unexpected-stop classifier — replace online backend

`packages/coding-agent/src/session/unexpected-stop-classifier.ts`, caller
`session/turn-recovery.ts:787` in `smart` mode. Synchronous at turn end inside
`UNEXPECTED_STOP_TIMEOUT_MS`. Asks a chat model for YES/NO and takes
`startsWith("yes")`. Same 4096-token workaround. A `true` verdict costs a full
retry turn (capped by `UNEXPECTED_STOP_MAX_RETRIES`).

Both verifiers confirmed the fit. One Noul:

```json
{ "state": "<assistant text>",
  "questions": { "unexpected_stop": { "type": "noul",
    "instructions": "The assistant says it will act, continue working, or call a tool, and then ends without doing so. Messages that report completion or ask if anything else is needed are not unexpected stops." } } }
```

Retry when `noul` clears a threshold (start at 0.7). The probability is a
knob the binary parse never had: a false positive burns a retry turn, a false
negative silently drops recovery. `isUnexpectedStopCandidate` (mechanical
mode) and the retry cap stay. The local model path stays for offline users.

What goes away on the online path: `parseUnexpectedStopClassification`,
`ANSWER_MAX_TOKENS`, `ONLINE_REASONING_SAFE_MAX_TOKENS`.

Residual risk: assistant text egresses to a new third party. Accepted decision
Q24 ("decide by data class and existing provider permissions") means this is
opt-in per provider permission, never default.

## 3. robomp issue triage — add a text-only pre-filter

Found by the completeness critic; the sweeps had scoped Python to `omp-work`.
`python/robomp` is the self-hosted GitHub triage bot
(`docs/user-facing-packages.md:14–19`). On every `issues.opened` webhook,
`tasks.triage_issue` (`src/tasks.py:473`) provisions a worktree and starts a
full `omp --mode rpc` session; the agent then reads the thread, searches for
duplicates and calls the `classify_issue` host tool with one of eight primary
labels (`host_tools.py:1840`), a priority, functional tags and a rationale.
`src/prompts/system_append.md` defines the label table and a five-point yes/no
merit gate that must all pass before `bug`.

This is the repo's only production fixed-option classification pipeline, and
the sweeps' rule excludes it because the decision is made inside the agent
turn. It should stay there: duplicate detection needs the local FTS index and
changelog checks, and the merit gate needs repo inspection. What Jev can do is
a pre-screen over title plus body before the session is provisioned:

- Choice over the eight primary labels plus `unsure`, to pre-label and to
  short-circuit `invalid`, `question` and batch-audit bodies (the
  `system_append.md` "audit/batch" pattern) without a worktree and session.
- Nouls for the text-only merit-gate items: first-person failure report,
  wanted-different-behavior framing, upstream/provider cause claimed,
  non-default option plus exotic environment.

Low-confidence answers proceed to the full session as today. The gain is
cost and latency on the webhook hot path, not accuracy of the final label.
Anything Jev pre-labels must still be confirmable by the session, which keeps
`classify_issue` as the only writer of labels. `classify_pr` (rank
`review:p0..p3`, type from `_PR_TYPES`) needs the diff and stays as is.

## 4. Roadmap: hypothesis tournament pairwise judge — design-time fit

`MASTER.md:2022` and `AUTORESEARCH-IMPLEMENTATION-PLAN.md:352` (§8.3, R11):
"For hypothesis tournaments, randomize presentation order, hide irrelevant
model identities from evaluators, preserve ties and uncertainty, and
periodically compare ranking against external evidence. Elo or judge
preference is a search aid, never a scientific validity certificate."

The pairwise preference is a Choice over `{A, B, tie or insufficient
evidence}` with the two frozen write-ups as state and identities hidden. Jev
returns the probability distribution the doc asks to preserve, is cheap enough
to run the full round-robin, and cannot leak identities it never sees. The
doc already caps its authority at "search aid", which matches Jev's role.

Caveats from both verifiers: R11 is unbuilt, so nothing is replaced today;
candidate write-ups may exceed Jev's undocumented state size and need a frozen
summary form; a single judge family cannot satisfy R11's own order and
identity effect measurement, so a second evaluator stays; Q24 egress
classification must exist before hosted judging of dataset-derived
hypotheses. Elo aggregation stays in code.

## 5. Roadmap: per-(claim, passage) semantic-support link — design-time fit

Five sweep candidates around claim verification were refuted as "derivable
from typed record linkage". The critic flagged the refutations as wrong, and
the cited lines bear that out. `MASTER.md:2088` (also
`AUTORESEARCH-IMPLEMENTATION-PLAN.md:418`, §9.4): "Source existence checks and
DOI resolution verify metadata, not whether the cited source supports the
claim; semantic support needs separate review." The §13.2 evidence matrix
(`MASTER.md:2308`) has a "Support and contradiction: linked sources/trials
with scope" field; §13.1 step 9 (`MASTER.md:2296`) is "Verify the key claims
before reporting"; R08 acceptance (`AUTORESEARCH:1115`) requires "a report's
material claims resolve to inspected evidence"; R17 acceptance
(`AUTORESEARCH:1188`) requires "edited prose cannot silently alter source
measurements".

Split the composite:

- The six-way claim status (observed, independently reproduced,
  literature-supported, inferred, hypothesized, unresolved) stays derived in
  code from record kinds and receipt counts. The refutations were right about
  that half.
- The atomic link judgment "does this passage support, contradict, or neither
  bear on this claim" is a Choice (or two Nouls) per (claim, passage), text
  only, repeated per claim per source in R08 and R17 reporting. That is Jev's
  shape, and it is the input the code derivation depends on.
- R17's prose check ("does this sentence claim more than the receipt it cites")
  is the same Noul applied to generated prose.

Caveats: unbuilt; the doc says "an LLM confidence score is not a confidence
interval" (§9.3), so Jev output is review input, never validity; scholarly
review "returns concrete findings" (§13.2), which stays generative.

## Withdrawn from revision 1

- **Conventional commit type/scope.** Revision 1 proposed a Noul for breaking
  changes. No prompt under `commit/conventional/prompts/` asks about breaking
  changes; the only hits are the changelog-section mapper in `commit-types.ts`
  and `normalization.ts`. The type pick is real but is one grounded pass over
  the diff together with scope, summary and details; splitting it adds a
  request per commit off any hot path. Keep the LLM.
- **Memory extraction `NO_FACTS` pre-gate.** Revision 1 assumed extraction
  runs per user turn. It runs only on explicit retain
  (`packages/mnemopi/src/core/beam/store.ts:311–334` `runFactExtraction`,
  scheduled from `remember()`; coding-agent entry at `mnemopi/state.ts:452`).
  There is no hot path to gate. Keep as is.

## Roadmap decisions examined and rejected

Each row names the doc's own mechanism, which is why Jev does not apply.

| Roadmap item | Where | Why not Jev |
| --- | --- | --- |
| Best-of-N discriminator | MASTER WP5/WP6 (1388–1452), ECC spec 40, 401, 446–456 | Spec wants deterministic prefilters (harness receipts, digest checks, single-passer short-circuit) then one cheap LLM judge with schema-validated rationale, evidence and risk fields; one call per tournament, off the hot path |
| Selector role | MASTER §17 (2471), AUTORESEARCH 801 | Winner comes from the predeclared primary metric and statistical protocol over evaluation receipts (§9.3); the comparison record needs reasons, uncertainty and abstention |
| Controller next action (13-item vocabulary), cycle step 12 | AUTORESEARCH §8.1–8.2 (288–334), §7.2 (263, 268); MASTER 1938–1942 | Algorithmic search policies (greedy, bandit, MCTS, successive halving, Optuna); "These are state transitions and job types. They are not fourteen mandatory LLM calls per experiment" |
| Failure classification (infra vs candidate vs negative result) | AUTORESEARCH 310, 474, 669 | Each domain adapter emits a typed failure taxonomy from structured signals (exit code, timeout, worker loss, cancellation) |
| Six-way claim status | MASTER 2086, AUTORESEARCH 416 | Derived from record linkage and receipt counts; only the link judgment (item 5 above) is a judgment |
| Search disposition (continue/replicate/archive/prune/select) | MASTER 1899 | State projection of the §8.2 policy output |
| Novelty score for concept-loss detection | Audit row N270.4 | Preregistered NSI-E4 measurement; the 03B contract prescribes deterministic screening; no runtime classifier exists |
| Reviewer binding (G5) | MASTER 224, GOAL-PROMPT 74–84 | Static role table; "no silent provider, model, tier, or effort substitutions" |
| Effect-free inference (G6) | ADR 0003, MASTER 225 | Capability flags on the operation, not a text judgment |
| Independent audit verdict PASS/NEEDS_FIX/BLOCKED | `session-system/agents/auditor.md`, MASTER WP4 (1363–1384) | Needs diff and evidence reading plus findings prose; "Existing mandatory audit model policy is preserved. Qualifying a cheaper auditor is a separate decision" |
| TaskRisk tier | Audit row FLEET-4 | "Implement the deterministic versioned TaskRisk engine" |
| Effort tier E0–E4, reviewer_required | ADR 0004 §2–3 | Launch-gate script plus blast-radius path list |
| Model routing per request | MASTER 1416, ECC 420, audit row 150 | "Do not introduce a model router agent that spends a request deciding the model for every request" |
| Project health onTrack/atRisk/offTrack | `summary` skill, `record_health` | Owner-session judgment inside `/summary`; the service stores a caller-supplied enum |
| Recovery, status polling | MASTER gate 1 (964), audit D5 | "recovery cannot depend on a summary LLM"; "no routine LLM polling" |

## Roadmap constraints on any Jev adoption

- **WP5 router rule** (`MASTER.md:1416`): no per-request model-router agent.
  Item 1 already spends a request per prompt to pick effort, not model. Jev
  makes that request two orders of magnitude cheaper but does not remove it.
  If the rule is read strictly, item 1 needs a recorded reconciliation, not a
  silent swap.
- **Q24 model exposure** (`ACCEPTED-DECISIONS.md:30`): egress decided by data
  class and provider permission; local-only where required. Every Jev backend
  must be opt-in and must not touch data classes marked local-only.
- **WP4 audit policy** and **WorkService native authority**: Jev output can
  feed a gate as evidence; it can never be the gate.
- **§9.3**: "an LLM confidence score is not a confidence interval". Jev
  confidence is calibration for routing, not statistical evidence.

## Code sites confirmed as not decisions

Generation or infrastructure, keep the LLM or no LLM at all: session titles,
task labels (`<title/>` is the empty result, not a gate), commit messages,
changelog, speech rewrite, edit auto-repair, memories stage 1 and
consolidation, compaction summaries and handoff, image description (vision),
eval `completion()` bridge, auth-gateway and auditor transport probes,
if-bench (benchmark of the model under test), compress approve/rewrite (same
agent, typed tool choice), cleanse checkers (linters), session-system digest
and session-ledger (three-line prose), advisor severity (advisor's own turn).
Already deterministic and correctly so: thinking-loop and tool-call-loop
guards, TTSR rule matching (regex and ast-grep, `capability/rule.ts`),
mechanical unexpected-stop mode, `ultrathink` keyword, error-string regexes
in `turn-recovery.ts`, mnemopi query-intent scoring and veracity update,
robomp autoclose and closing-PR checks.

## Integration sketch

- One client, `packages/coding-agent/src/tiny/jev-client.ts`: a single
  `fetch` to `/v1/systemone` with `{ state, model: "jev-latest", questions }`
  and typed answers back. No SDK dependency.
- Key via `TYPESAFE_API_KEY` / `/login typesafe`, following the web-search
  provider pattern in `web/search/types.ts`.
- Add `"jev"` beside `online` and the local keys in
  `providers.autoThinkingModel` and `providers.unexpectedStopModel`
  (`config/settings-schema.ts` near lines 5521 and 5577). Both classifiers
  already dispatch on that setting.
- robomp: a `prefilter` step in `tasks.triage_issue` before
  `sandbox.ensure_workspace`, behind a `ROBOMP_PREFILTER` setting, writing
  its answer to the issue row for the session to confirm.
- Tests: threshold and score-mapping tests replace the keyword-parser tests;
  one wire-shape test for the request body.

## Not filed

No Work Ledger tool is available in this session, so no ledger item was
created. File one before acting on items 1–3.
