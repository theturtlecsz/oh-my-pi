# OMP-301 — Jev tournament judge measurement (R1-J)

R1-J implements the Jev choice `{A, B, tie}` pairwise judge for hypothesis
tournaments and keeps the chat judge as the second family for bias checks. This
document is the plan and the report skeleton: what each surface measures, how it
is measured, what stays comparable, and — the part attempt 1 got wrong — exactly
what is and is not measured, and who runs it.

## Slice boundary

This item has two slices, and they are committed separately:

- **Implementer slice (this commit).** The judge, the injected-judge harness, the
  owner-run CLI, the report template, and stub-driven tests. Every test drives a
  stub Jev server or an injected judge. **No implementer number is a measurement.**
  Nothing here runs against a real model or the real Jev endpoint.
- **Owner slice (owner's machine, owner's key).** The owner runs the CLI against
  the real typed Jev endpoint and their configured smol model and commits the
  rendered report and `results.json`. Precedent: OMP-445 committed the harness and
  plan under `docs/reports/jev-measurement/` (implementer) and the measured report
  + results in a separate `flood-owner` commit (`5dff6ce5cf`).

Attempt 1 was rejected for committing fabricated owner numbers (agreement,
latency, cost, order-invariance) with a synthetic timestamp and no harness. This
commit removes those files and ships the harness, the CLI, and the template the
owner slice fills in.

## Acceptance mapping

- **R1's tests pass with the Jev judge selected.** `runTournament` requires 1–2
  judges of **distinct families**; the tool selects the Jev judge when
  `autoresearch.tournament.judgeModel` is `jev`/`@jev` or `jev.tournamentJudge` is
  true, and the chat second family stays available. Covered by
  `packages/coding-agent/test/jev/jev-tournament-judge.test.ts` and
  `packages/coding-agent/test/autoresearch-tournament-tool.test.ts`.
- **Off-list answers impossible.** The Jev client validates every choice option in
  the response against the question's `allowed` option set and returns
  `off_list` for anything outside it (`packages/coding-agent/src/tiny/jev-client.ts`,
  `validateAnswers`). The judge throws; no off-list option reaches
  `parseJevJudgeOutcome`. Covered by the "off-list answers are structurally
  impossible" test.
- **Measurement report committed under `docs/reports/`.** The owner slice commits
  `docs/reports/jev-tournament-measurement-report.md` and
  `docs/reports/jev-tournament-results.json`, rendered from
  `tournament-report-template.md`. This commit commits the template, not numbers.

## Surfaces and method

### 1. Labeled pair agreement

**What is measured.** Given a labeled pair set (each pair: two write-ups and a
reference preference in `{A, B, tie}`), both judges score each pair in both
presentation orders. Agreement is the share of pairs whose canonical
(order-collapsed) preference matches the reference; inter-judge agreement is the
share where the two judges agree with each other.

**Why both orders.** A judge with position bias answers differently when the same
two write-ups swap slots. Collapsing both orders to a canonical preference and
reporting **order invariance** exposes that bias instead of hiding it behind a
single order.

**How.** `measureLabeledPairs` in `tournament-harness.ts` drives both judges
through `JudgeRecorder`, which wraps each judge to record per-call latency, token
usage, and failures. A call that throws is counted as a failure (reported as the
off-list / failed rate) and dropped from that pair's denominator.

### 2. Twenty-hypothesis tournament — cost and wall time

**What is measured.** The same 20-hypothesis pool and seed are run through
`runTournament` twice, once with the chat judge and once with the Jev judge, so
each family gets its own cost and wall time. Per family the report gives
comparisons, judge calls attempted, failures, tokens, cost, wall time, and
p50/p95 judge-call latency.

**How.** `measureTournamentWith` instruments the judge, times the whole
`runTournament` call, and reads cost and tokens from the tournament's own usage
total (built from the verdicts the instrumented judge passed through). A failed
call is recorded as a tie so the tournament completes; the failure count is
reported separately.

### 3. Second family for bias checks

R11 asks to compare ranking against external evidence and warns that a single
judge family cannot satisfy the order/identity measurement. The chat judge
(`createModelJudge`, neutral-label blinded) is the second family. `runTournament`
enforces distinct families, so a Jev-vs-Jev or chat-vs-chat pair of judges is
rejected.

## What is not measured

- **Calibration.** The judge derives a deterministic outcome from Jev's
  probability distribution; this measures agreement, not whether the
  probabilities are calibrated.
- **Scientific validity.** §8.3: a judge preference is a search aid, never a
  validity certificate.
- **Off-list / off-options answer space.** Off-list is structurally impossible
  (the client rejects it); the harness counts a rejected answer as a failed call,
  not as a measured label.
- **State length.** Whether a real write-up exceeds Jev's undocumented state size
  is not measured; the harness receives the redacted, bounded `judgeText`.
- **Any number in the implementer slice.** Every test uses a stub server; no
  implementer number is a measurement.

## Owner slice — exact commands

The live run is the operator's. The typed Jev key is read on the machine at run
time from `~/.config/omp/jev.env` (`TYPESAFE_API_KEY`) and is never stored; the
chat judge uses the owner's configured smol model through the normal registry.

```bash
cd ~/flood-repos/oh-my-pi-wt/OMP-301     # or the merged-main checkout

# 1. Freeze the inputs (checked in with the owner slice):
#    docs/reports/jev-measurement/tournament-pairs.json       (labeled pairs)
#    docs/reports/jev-measurement/tournament-hypotheses.json  (20 hypotheses)

# 2. Run the measurement. The key is read from ~/.config/omp/jev.env at startup.
bun docs/reports/jev-measurement/run-tournament.ts \
  --pairs docs/reports/jev-measurement/tournament-pairs.json \
  --hypotheses docs/reports/jev-measurement/tournament-hypotheses.json \
  --question "Which mechanism best explains the observed cache behaviour?" \
  --out docs/reports/jev-tournament-measurement-report.md \
  --json docs/reports/jev-tournament-results.json
```

Outputs: `docs/reports/jev-tournament-measurement-report.md` (rendered from the
template) and, with `--json`, `docs/reports/jev-tournament-results.json`.

Safeguards exercised by the run:

- **Key read at run time only.** `readJevKey()` parses `~/.config/omp/jev.env`
  when the run starts; a missing key exits non-zero before any request. The value
  is never logged; the run prints only the file path.
- **Implementer slice never holds it.** Every test drives the injected-judge
  harness in `tournament-harness.ts`; no test calls `readJevKey()` against the
  real env file.

## Where the pieces live

- `packages/coding-agent/src/autoresearch/tournament/jev-judge.ts` — the Jev
  pairwise judge (`createJevJudge`, `parseJevJudgeOutcome`).
- `packages/coding-agent/src/autoresearch/tournament/jev-judge-state.md` +
  `packages/coding-agent/src/prompts/jev/tournament-preference.md` — the judge
  state and choice-question prompts.
- `packages/coding-agent/src/autoresearch/tools/hypothesis-tournament.ts` — tool
  that selects the Jev judge for a session.
- `docs/reports/jev-measurement/tournament-harness.ts` — injected-judge harness.
- `docs/reports/jev-measurement/run-tournament.ts` — owner-run CLI and renderer.
- `docs/reports/jev-measurement/tournament-report-template.md` — report template.
- `packages/coding-agent/test/jev/jev-tournament-judge.test.ts` — judge, parsing,
  off-list, tool integration, and 20-hypothesis Swiss schedule tests.
- `packages/coding-agent/test/jev/tournament-measurement.test.ts` — stub-driven
  harness tests.
