# R2-J — Jev claim-support classifier measurement (owner slice)

Owner-slice measurement plan and report skeleton for the R2 claim-support
evidence-matrix classifier implemented in
`python/omp-work/src/omp_work/report_evidence.py` (`JevClassifier`).

The classifier itself is validated by the implementer slice over a stub Jev
server: `python/omp-work/tests/test_report_evidence_jev.py` with the
`httpx.MockTransport` stub in `python/omp-work/tests/jev_stub.py`. Nothing in
that slice reaches a real model or the real Jev endpoint. This document, the
harness beside it, and the numbers the harness writes are the **owner slice**:
they need the owner's Jev key and a real completion model, so they are run on
the owner's machine.

- Spec: `docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md` §13.2 "Evidence
  synthesis", §9.4 "Claim verification and reporting"; milestones R08 and R17.
- Fit analysis: `docs/report/jev-decision-classifier-review-2026-09-25.md` §5
  "per-(claim, passage) semantic-support link".
- Precedent for the owner-slice shape: `docs/reports/jev-measurement/ROUTER-MEASUREMENT.md`.

## What is measured

The atomic link judgment — "does this passage support, contradict, or neither
bear on this claim" — is a Jev `Choice` over `{supports, contradicts, neither}`
per (claim, passage) pair. The measurement asks three questions:

1. **Agreement with the chat classifier.** How often does the Jev choice label
   equal the label the shipped `ChatModelClassifier` returns for the same pair,
   over the same evaluation set? This is the drop-in question: R2 already runs
   the chat classifier, and the Jev path must not silently change verdicts.
2. **Agreement with a hand-labeled set.** How often does each classifier equal a
   human label, over **at least 100 hand-labeled (claim, passage) pairs**? The
   hand labels are the ground truth; the chat classifier's own labels are not.
3. **Cost per report.** What does one report's worth of classification cost on
   the Jev path, calculated from the input character token estimate
   (`(len(state)+3)//4`, as Jev `/v1/systemone` responses return no token
   usage metadata), priced at $0.042/MTok (output tokens free, per the §5
   review's Jev primitive table) and scaled to the pairs a single report links?

## Evaluation set

A report's matrix links each material claim to its top-k cited passages, so one
report's pairs are (claims × top-k). The evaluation set is built the same way,
then hand-labeled:

- Collect ≥100 (claim, passage) pairs from real drafts and cited sources, or
  from the R08/R17 reporting fixtures.
- Label each pair `supports` / `contradicts` / `neither` against the passage's
  own content only — never the citation's existence, author, venue, or DOI.
  That rule is the classifier's own rule; a label that leans on metadata is not
  a valid label.
- Record the label file as JSONL, one object per line:

  ```json
  {"claim": "<claim text>", "passage": "<passage text>", "source_id": "<id>", "locator": "<loc>", "label": "supports|contradicts|neither"}
  ```

The file is the owner's; it is not committed with fabricated labels.

## Protocol

For each pair, one Jev choice call and one chat-classifier call. The Jev side
uses `JevClassifier` (batched, text-only state, head-truncated to 8000 chars
with a `…[truncated N chars]` marker). The chat side uses `ChatModelClassifier`
with the model the owner selects.

Reported per classifier: label accuracy against the hand labels, the confusion
by label, and the transport/parse failure share. Reported across classifiers:
pairwise agreement (Jev vs chat). Reported for the Jev path: input tokens per
pair (character estimate, `(len(state)+3)//4`), p50/p95 latency, and cost per
pair and per report.

**Probabilities are routing only.** The Jev choice's probability map is stored
on `Classification.probabilities` and never used as a confidence interval, never
rendered into a report body, and never scored as if it were a calibrated
probability (§9.3, "an LLM confidence score is not a confidence interval"). This
measurement therefore reports the **label** the argmax picks, not a probability
threshold sweep. No probability calibration is measured.

## Owner slice — exact commands

The live run is the operator's. The key is read from the environment at run time
and never stored.

```bash
cd ~/flood-repos/oh-my-pi-wt/OMP-302     # or the merged-main checkout

# TYPESAFE_API_KEY is the owner's Jev key. JEV_CLAIM_SUPPORT_CHAT_CMD is a
# command that reads the rendered classifier prompt on stdin and writes one
# JSON object {"text":..., "input_tokens":..., "output_tokens":..., "model":...}
# on stdout — the owner's wiring for the comparison chat model.

TYPESAFE_API_KEY=... \
JEV_CLAIM_SUPPORT_CHAT_CMD='...your chat completion command...' \
uv run --project python/omp-work python docs/reports/jev-claim-support/run.py \
  --labels ~/.omp/jev-claim-support-labels.jsonl \
  --out docs/reports/jev-claim-support-measurement-report.md \
  --pairs-per-report 24
```

Safeguards the run exercises:

- **Owner key only.** The key comes from `TYPESAFE_API_KEY`; a missing key exits
  non-zero before any request.
- **Jev output never validity.** The harness stores the Jev probability map only
  to prove it is ignored for scoring; the report's label columns come from the
  argmax, and the report has no probability column.
- **Hand labels are ground truth.** The agreement columns are measured against
  the committed-by-the-owner label file, never against either classifier.

## Results

Not yet run. The live run requires the owner's Jev key and a comparison model,
neither available in the implementation worktree; this section is filled by the
owner slice.

| Metric | Chat classifier | Jev |
| --- | --- | --- |
| Hand-label accuracy | pending | pending |
| Supports precision / recall | pending | pending |
| Contradicts precision / recall | pending | pending |
| Pairwise agreement (Jev vs chat) | — | pending |
| Input tokens per pair | — | pending |
| p50 / p95 latency (ms) | pending | pending |
| Cost per pair ($) | — | pending |
| Cost per report ($, 24 pairs) | — | pending |
| Transport / parse failure share | pending | pending |

## What is not measured

- **Probability calibration.** The Jev choice returns a probability map; this
  measurement uses only its argmax and does not fit, sweep, or claim any
  calibration. A probability is not a confidence interval (§9.3).
- **Threshold behaviour.** No threshold is applied to any probability; the
  report has no threshold sweep because R2 stores probabilities as routing
  hints, not decisions.
- **Six-way claim status.** Only the atomic link judgment is measured. The
  composite status (observed, independently reproduced, literature-supported,
  inferred, hypothesized, unresolved) stays derived in code from record kinds
  and receipt counts, and is out of scope here.
- **R17 prose over-claim check.** The "does this sentence claim more than the
  receipt it cites" Noul is a separate application and is not measured.
- **Passage retrieval quality.** The evaluation set uses the lexical top-k
  `select_passages` produces; whether that retrieval is good is not measured.
