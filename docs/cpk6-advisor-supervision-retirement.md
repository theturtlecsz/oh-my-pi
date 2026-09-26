# CPK-6 Advisor supervision: legacy retirement record (OMP-208)

This is the retirement record for the stacked legacy advisor dedupe. It names
the retired path, the evidence that the replacement is no worse, the single
switch that restores it, the shadow/canary controls that sit between the two
paths, and the assumptions/deferrals the migration carries.

The default live Advisor path is now the **structured** supervisor
(`advisor.supervisionPath: "structured"`). The legacy path is not deleted — it
stays one setting away — because CPK-6 retires a duplicate path only after the
replacement passes parity and rollback evidence, and only the owner may sign off
deleting it (see **Deferred**).

## Retired path

The retired duplicate is the stacked dedupe pair that used to run inside
`SessionAdvisors#routeAdvice` on every live advisor note:

1. **`AdviseTool` severity-rank dedupe** — the whitespace-collapsed note key
   (`advisorNoteDedupeKey`) with a highest-seen severity rank
   (`advisorSeverityRank`), so a note is forwarded only when its rank strictly
   exceeds the recorded one (a real `nit → concern → blocker` escalation).
2. **`AdvisorEmissionGuard`** — the per-session emission stage that follows it:
   the case/punctuation-folded noise phrase filter, session-scoped exact-text
   dedupe with FIFO eviction, and the one-accepted-note-per-update budget.

Both stages are still implemented and still reachable, but now only with
`advisor.supervisionPath: "legacy"`. On the default path the structured
`AdvisorSupervisionGate` composes those same two decisions (rank map, then
proposal parse, then emission classify) plus CPK-6 proposal construction, in one
place, and the pipeline delivers only the structured arm.

There is **no duplicate live dispatcher** on the default path: a structured
pipeline builds no legacy arm, so legacy invocations stay at zero
(`AdvisorSupervisionPipeline.report().invocations.legacy === 0`).

## Evidence

| Evidence | Test | Proves |
| --- | --- | --- |
| Default flip + single dispatcher | `test/advisor/supervision-retirement.test.ts` | A session built with default settings reports `structured` authority and `{legacy: 0, structured: 1}` invocations; the rollback drill flips to `legacy` authority with `{legacy: 1, structured: 0}`; `createAdvisorReplayArm("legacy")` over the sentinel corpus delivers exactly the proposals a pipeline-free `AdviseTool` + `AdvisorEmissionGuard` pair delivers. |
| Structured/canary qualification | `test/advisor/supervision-qualification.test.ts` | `qualifyAdvisorSupervision` over the historical sentinel corpus is `qualified` for the structured and canary arms with `zeroRegression: true` and an empty canary divergence list, so the default flip stays CI-gated. |
| Live routing per path | `test/advisor-supervision-routing.test.ts` | Each path reports its authority, suppresses `Stop.` and a punctuation-variant duplicate, and a structured advisor rebuilt after `advisor.supervisionPath=legacy` reports legacy authority with zero structured invocations. |
| Legacy catching power (pre-flip baseline) | `test/advisor/supervision-history.test.ts` | The legacy pair, driven as `SessionAdvisors` drives it, still catches every labelled defect in the sentinel corpus while leaving the noise/deferral cases undelivered. |

The sentinel corpus is the CPK-6 labelled fixture set (one session per rule
class), loaded through the real `AdvisorTranscriptRecorder` format.

## Rollback

One switch restores the retired path:

```
omp config set advisor.supervisionPath legacy
```

An advisor rebuilt after that setting reports `legacy` authority and zero
structured invocations; `createAdvisorReplayArm("legacy")` reproduces the
pipeline-free `AdviseTool` + `AdvisorEmissionGuard` proposals exactly over the
sentinel corpus. Reverting to the default is:

```
omp config set advisor.supervisionPath structured
```

## Shadow and canary

`advisor.supervisionPath` accepts four values:

- `legacy` — the retired pair only; legacy authority. The rollback target.
- `structured` — the structured gate only; structured authority. The default.
- `shadow` — builds **both** arms, delivers the legacy buffer, and counts
  divergences without ever changing authority. Used to verify a candidate gate
  against live traffic before cutover.
- `canary` — builds both arms and delivers the structured buffer until the
  divergence count exceeds `advisor.supervisionCanaryMaxDivergences`; that call
  and every later call deliver the legacy buffer for the pipeline's life
  (including across a reset). Exceeding the budget logs
  `canary_budget_exceeded` and latches authority to legacy.

`advisor.supervisionCanaryMaxDivergences` (default `0`) is the canary's
divergence budget: with `0`, the first call whose buffers differ latches the
pipeline back to legacy. `qualifyAdvisorSupervision` runs the canary arm at
budget `0` over the sentinel corpus, so a divergence there fails the CI gate
before any default change lands.

## Assumptions (from OMP-208-s01)

- Semantic rule classes = the 5 Advisor categories (`AdvisorCategory`,
  `advise-tool.ts`). The Task Observer first-tool hook is a deterministic gate;
  it stays live, untouched.
- Retired duplicate path = the stacked dedupe pair (`AdviseTool` rank map +
  `AdvisorEmissionGuard` in `session-advisors #routeAdvice`). The structured
  path matches it decision for decision for schema-valid calls (category
  present; the tool schema requires it). Retirement = default
  `advisor.supervisionPath` becomes `structured`; legacy stays one setting away.
- Empty/whitespace notes are legacy noise: the structured gate suppresses them
  as `noise` before parsing, so the strict parser never sees them.
- Historical replay reads recorded `__advisor*.jsonl`; sentinels are committed
  labelled fixtures; canary = path `canary` with a divergence budget; the
  evidence gate is a CI test.
- Replay is async: arms may return a promise and the engine awaits it.

## Deferred (from OMP-208-s01)

Observer batched learning and hook migration; owner live canary on real
sessions; noise tuning beyond parity; owner-signed retirement record; deleting
legacy code; gate handling of category-less calls beyond suppression.
