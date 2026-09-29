# OMP-445 — Jev measurement through OpenRouter's Jev Router

Owner ruling 2026-09-29: measure Jev through OpenRouter's Jev Router (`typesafe/jev-router`) with the owner's existing OpenRouter key, instead of waiting for a TypeSafe key. This document is the plan and the report skeleton: what Jev Router can and cannot measure for each OMP-298 decision, how it is measured, what stays comparable, and what the report must say is not measured. It also records the WP5 verdict decision.

## Why the OMP-298 design does not carry over

The OMP-298 harness and `packages/coding-agent/src/tiny/jev-client.ts` call TypeSafe's typed decision API:

```
POST https://api.typesafe.ai/v1/systemone
{ "model": "jev-latest", "state": "...", "questions": { ...choice/noul... } }
-> { "id": "...", "answers": { "difficulty": { "probabilities": { "low": .., ... } }, ... } }
```

That endpoint answers HTTP 401 to the owner's OpenRouter key. `TYPESAFE_API_KEY` in `~/.config/omp/jev.env` is only a reference to `OPENROUTER_API_KEY`, and no TypeSafe key is being sought. OpenRouter's public model list carries one TypeSafe model, `typesafe/jev-router`, which is a **chat-completions router**: it forwards the request to a routed model and returns that model's completion. There are no probability maps, no Choice/Noul question types, and no `supported_parameters` entry.

Everything downstream of "probability" therefore does not survive the move. What does survive is a single-label classification whose answer is the router's own text, plus the transport facts OpenRouter reports (routed model, token usage, and — via the generation record — actual charged cost and reasoning metadata).

## Per-decision plan

### 1. Auto-thinking effort — `classifyDifficulty`

**What Jev Router measures.** The routed label for "how much reasoning effort does this request need?", chosen from `low | medium | high | xhigh`, scored against the same dataset effort label the OMP-298 harness scores. With the generation record enabled, also the routed model and the routed reasoning effort the router picked for that request.

**How.** One chat completion per prompt, pinned to `typesafe/jev-router`, with `docs/reports/jev-measurement/prompts/router-effort.md` as the instruction. The prompt is the dataset prompt. The routed model id is read from the response `model` field; the reasoning effort and actual cost are read from `GET /api/v1/generation?id=<response id>`. `parseFirstLabel(text, ROUTER_EFFORT_LABELS)` reads the first allowed label; anything else counts as unparseable.

**Comparable metrics.** Effort accuracy vs the current smol classifier; p50/p95 latency; cost per 1000 calls from actual OpenRouter usage (`total_cost` on the generation record); the distribution of routed models and routed reasoning efforts.

**Reported as not measured.** The typed API's Choice probabilities and the `no_repro` / `irreversible` / `live_cutover` Noul questions that combine into the `max` tier. The router answers the effort question only, so the `max`-tier composite is unmeasurable here.

### 2. Unexpected-stop at 0.70 — `classifyUnexpectedStop`

**What Jev Router measures.** Whether the routed label for "is this message an unexpected stop?" is `yes` or `no`, scored against the dataset `continue`/`stop` label, with precision and recall reported at the router's own decision.

**How.** One chat completion per turn-end using `docs/reports/jev-measurement/prompts/router-stop.md`. `parseYesNo` reads the label; the statement asks whether the message **is** an unexpected stop, so a routed `yes` maps to the `continue` label and `no` to `stop`.

**Comparable metrics.** Accuracy, precision, recall, p50/p95 latency, cost per 1000 calls, routed model and routed effort histograms.

**Reported as not measured.** The 0.70 threshold itself. The threshold operates on the typed API's probability for the statement; the router emits a label, not a probability, so the report states that precision/recall are for the router's own decision point and that no threshold sweep is possible. The off-list/off-options buckets also have no router equivalent: a routed answer with no allowed label lands in the single unparseable bucket.

### 3. Robomp issue pre-gate — `run_prefilter`

**What Jev Router measures.** The routed primary classification of an issue, from the same label set the prefilter's `primary_type` question offers (`bug, enhancement, question, proposal, documentation, wontfix, invalid, duplicate`), scored against the dataset label. Also the derived **skip-session share**: the fraction of issues the router's label would route away from a full session (`invalid`, `question`).

**How.** One chat completion per issue (`title\n\nbody`) using the `prefilter_questions.toml` primary-label options as the allowed set; `parseFirstLabel` reads the label.

**Comparable metrics.** Primary-label accuracy, skip-session share, p50/p95 latency, cost per 1000 calls, routed model and routed effort histograms.

**Reported as not measured.** The prefilter's five yes/no gates (`batch_audit`, `first_person_failure`, `wanted_different_behavior`, `upstream_cause`, `nondefault_exotic_env`), its probability threshold, and the exact answer/session route the typed prefilter computes. Only the primary label and the derived skip share are measured, and the report says so.

## Metrics that stay comparable to the OMP-298 report

- **Accuracy vs current.** Same datasets, same labels, same scoring; the current-side numbers come from the OMP-298 path (the configured classifier with the Jev decision path forced off), so the column comparison holds. The current side uses the model described in [Current-side smol model and harness resolution](#current-side-smol-model-and-harness-resolution).
- **p50 / p95 latency.** Per-call wall time, measured the same way on both sides.
- **Cost per 1000 calls.** Jev-side from actual OpenRouter usage (the generation record's `total_cost`); current-side from provider-reported completion usage. The report and `results.json` use the same cost-source label: `generation-record` or `tokens-x-catalogue-price` on the Jev side, and `provider-usage` on the current side when that side reported a non-zero completion cost (`unavailable` when it ran and reported none). A current-side cell is `not measured` only when that side did not run.

## Current-side smol model and harness resolution

The current side runs `classifyDifficulty` and `classifyUnexpectedStop` with the Jev decision path forced off.

- **Which model it uses:** The first model `resolveRoleSelection(["tiny", "smol"], settings, registry.getAvailable())` returns. A configured `modelRoles.tiny` is selected first. `modelRoles.smol` is used when tiny does not resolve to an available model.
- **Where it comes from:** `buildCurrentSmolHarness()` in `run-router.ts`:
  1. `Settings.loadReadOnly()` reads `config.yml` or `config.yaml` from the agent directory (`~/.omp/agent` by default) and merges project settings, including model roles from the project `.omp/config.yml`.
  2. `discoverAuthStorage()` opens the local credential store at `~/.omp/agent/agent.db`, or an auth broker when one is configured (`OMP_AUTH_BROKER_URL` / `OMP_AUTH_BROKER_TOKEN`, or `auth.broker.url` / `auth.broker.token` in the agent `config.yml`, with `~/.omp/auth-broker.token` when that file holds the broker token).
  3. `ModelRegistry` is built from that credential store and the read-only settings.
  4. `withoutJevSettings()` forces `jev.enabled`, `jev.autoThinking`, and `jev.unexpectedStop` to false.
  5. Both classifiers call `resolveRoleSelection(["tiny", "smol"], settings, registry.getAvailable())`. Roles are tried in that order. For each role, `resolveModelRoleValue(settings.getModelRole(role), ...)` returns no model when that role is unset, so an unset role does not consult `MODEL_PRIO`. The `MODEL_PRIO.smol` list in `packages/coding-agent/src/config/model-resolver.ts` is used only when a stored role value is an alias such as `@smol` and that alias has no concrete override.

## What the report must say is not measured

The report's "What is not measured" section (in `router-report-template.md`) states, at minimum:

- Probability maps and calibration — the router returns text, so no distribution, no 0.70 threshold on a probability, and no confidence value is available.
- Off-list / off-options answer-space validation — a routed answer with no allowed label is one unparseable bucket, not two.
- Robomp routed-decision equivalence — the five yes/no gates and the answer/session route are not reproduced.
- Router internals — Jev Router's own routing cost and its forwarded sub-request are invisible; only the routed model id, its reasoning effort, and its price are reported.
- Current-side model identity — the current side runs the model `resolveRoleSelection(["tiny", "smol"], settings, registry.getAvailable())` returns (configured `modelRoles.tiny` first, then `modelRoles.smol`; those roles are read from the agent directory `config.yml` / `config.yaml` and the project `.omp/config.yml`), not a matched model, so the latency/cost comparison is not a same-model comparison.

## WP5 verdict decision

**WP5 does not apply to this measurement.**

The WP5 router rule (`MASTER.md` line 1418, `docs/programme/ECC-IMPLEMENTATION-SPEC.md` line 420) reads: "Do not introduce a model router agent that spends a request deciding the model for every request. Use task classification already available in intake/plan/configuration." The rule constrains what the product **does**: it forbids inserting a per-request generative router into the execution path.

This item measures whether such a router would be worth adopting; it does not adopt one. Nothing in `packages/coding-agent/src` changes, no classifier calls a router at run time, and the measurement code lives under `docs/reports/` and runs only when the operator runs it. No per-request router is introduced, so the rule is not triggered and there is no gate to satisfy or amend. The report's single verdict line records that decision:

> WP5 does not apply: the router is measured, not adopted — no per-request generative router is introduced.

That is different from the OMP-298 verdict line, which judged the typed classifier pair against the amendment ("cheap calibrated classifiers permitted with recorded usage"). A router measurement cannot produce an amendment verdict: the amendment permits a cheap *calibrated classifier*, and the router exposes no calibration to judge. When the measurement is positive, the adoption decision is a separate item that does trigger WP5 and must argue the router rule then.

## Owner slice — exact commands

The live run is the operator's. The key is read on the machine at run time and never stored, and the transport may reach only `https://openrouter.ai`.

```bash
cd ~/flood-repos/oh-my-pi-wt/OMP-445     # or the merged-main checkout

# 1. Build the sets from session history (robomp history is absent on this host).
bun docs/reports/jev-measurement/build-sets.ts \
  --sessions ~/.omp/agent/sessions --no-robomp --out ~/.omp/jev-router-sets

# 2. Run the router measurement. The key is read from ~/.config/omp/jev.env
#    (OPENROUTER_API_KEY) at startup; the Jev side is limited to https://openrouter.ai.
bun docs/reports/jev-measurement/run-router.ts \
  --sets ~/.omp/jev-router-sets \
  --out docs/reports/jev-router-measurement-report.md \
  --json docs/reports/jev-router-results.json \
  --generation-records
```

`--generation-records` adds one `GET /api/v1/generation` per call so the cost column is actual OpenRouter usage (and the routed reasoning effort lands in the report). Without it, cost falls back to tokens times the routed model's catalogue price and the cost-source column says so.

Safeguards exercised by the run:

- **Jev side origin-locked.** `makeOpenRouterFetch()` throws on any origin but `https://openrouter.ai`; the transport also refuses any model but `typesafe/jev-router`.
- **Key read at run time only.** `readJevEnvKey()` parses `~/.config/omp/jev.env` when the run starts; on a missing key the run exits non-zero before any request. The key value is never logged; the run prints only the file path.
- **Flood workers never hold it.** Every test passes the fake transport and a literal test key; `readJevEnvKey()` is exercised only against a `TempDir`.
- **Aborts.** A 401/403 aborts the run immediately; more than 5% transport failures across a route aborts it.

## Supersession

OMP-298-s07 (the TypeSafe-key run) is superseded. No TypeSafe key is being sought; this item's live owner slice is the measurement.

## Where the pieces live

- `docs/reports/jev-measurement/router-transport.ts` — origin-locked OpenRouter transport, generation-record reader, label parsers, `readJevEnvKey`.
- `docs/reports/jev-measurement/router-harness.ts` — the three per-decision measurements and the WP5 verdict line.
- `docs/reports/jev-measurement/run-router.ts` — CLI and report renderer.
- `docs/reports/jev-measurement/router-report-template.md` — report template (method, per-decision tables, "What is not measured", verdict).
- `docs/reports/jev-measurement/prompts/router-{effort,stop}.md` — the router instructions.
- `packages/coding-agent/test/jev/router-measurement.test.ts`, `packages/coding-agent/test/jev/fake-openrouter.ts` — tests over a fake transport; no test reaches `openrouter.ai` or reads the env file.
