# Model review and qualification

Qualification precedes routing. A change to model, provider, reasoning effort,
role assignment, or offering revision requires fresh candidate-call evidence on
the fixed task set. New listings trigger evaluation, never automatic replacement.
The deterministic supervisor (ADR 0006 D33) is code, not a model offering.

## Authority and scope

For Work Ledger execution routing, the governing file is
[`routing-policy.v1.json`](../python/omp-work/src/omp_work/routing/routing-policy.v1.json).
`routing/policy.py:load_policy` loads that packaged file by default; stages,
escalation tiers, alternates, and reviewer declarations are its policy facts.
[`docs/models.md`](models.md) documents coding-agent model configuration and
runtime selection, not a competing Work Ledger assignment table. Coding-agent
catalog compatibility rules govern that separate subsystem. This review document
is a procedure and policy snapshot, not another routing source.

Current policy stages cover plan, implement, verify, research, audit and acceptance
review. Judge and operator session have qualification tasks, but no stage assignment
in this policy; qualify their actual offering before use. End-to-end caller/lane
verification and reconciliation of any other role files remain separate ledger
work: this offline grader neither proves runtime routing nor changes it. Preserve
current model identifiers; do not restore historical O2 assignments mechanically.
Where governing policy settles an assignment, follow it; unresolved disagreements
require owner decision, recorded with evidence before activation.

## Owner schedule

Ledger OMP-615 r3, Chris, 2026-10-13:

- Check offerings from every provider in use daily, including reviewer providers.
  OMP-615-s09 implements the read-only check of listings, published retirement or
  deprecation notices, and provider status. Findings open evaluations.
- Qualify relevant changes weekly on the fixed task set before routing them.
- Review an urgent retirement, deprecation notice, or service failure of a model
  in use immediately.
- A new listing triggers evaluation, never automatic replacement.

The governing policy records this schedule under `review.schedule`, with
`review.task_set_digest`, `review.last_review` (date/outcome) and per-recipe
status in `review.recipes`. Current last review: 2026-10-14, `baseline_retained`
(Chris decision 5); no live qualification is claimed. The daily check is
installed and read as described under
[Daily offerings check](#daily-offerings-check).

Current grandfathered recipes (stages, tiers, alternate, every reviewer identity
with suffix, and concrete test-verified lane/role bindings) are frozen in
[`model-review-baseline.v1.json`](../python/omp-work/src/omp_work/routing/model-review-baseline.v1.json),
pinned by SHA256 in `routing/model_review.py`. `check_policy_review(policy)`
accepts a `baseline` entry only for the exact frozen recipe; any addition or
change needs a dated `qualified` entry whose outputs are re-graded with
`qualify`. Bindings without a resolved wire or verified caller stay `pending`.

## Daily offerings check

The owner schedule's daily check is installed as a systemd user timer; the
policy installs no live provider runner of its own. From a checkout:

```sh
omp-work offerings install-timer --sources "$HOME/offerings/offerings-sources.json"
systemctl --user daemon-reload
systemctl --user enable --now omp-offerings-check.timer
```

`install-timer` takes `--sources` (required), `--record-dir`, `--omp` and
`--systemd-dir`; they default, in order, to the sources file the line names,
`default_record_dir()`, the `omp` on `PATH`, and `default_systemd_dir()`
(`$XDG_CONFIG_HOME/systemd/user`, else `~/.config/systemd/user`). A relative
path in the sources file — the notices capture, or any `listing-file` lane — is
refused before anything is written, as a missing or malformed sources file and
an unfindable `omp` are. The two units are `omp-offerings-check.service` and
`omp-offerings-check.timer`: the service runs the CLI with no working directory
(so every path is absolute), `Type=oneshot`, `UMask=0077` and
`SuccessExitStatus=3`, and the timer is `OnCalendar=daily` with
`Persistent=true`. The command prints exactly the two `systemctl --user` lines
above and activates nothing; the owner enables the timer.

Records land directly in `--record-dir`, else directly in
`default_record_dir()` (`$XDG_STATE_HOME/omp/work-ledger/offerings`, else
`~/.local/state/omp/work-ledger/offerings`) — no per-run subfolder. Each run
appends one `evaluation-<created_ns>-<uuid4>.json`, plus an
`owner-report-<same stem>.json` alongside it whenever any finding is
`urgent_review` or `check_gap`. Exit `0`: nothing actionable. Exit `3`: an owner
report was written, which `SuccessExitStatus=3` keeps healthy for the unit.
Exit `2`: an input error — one diagnostic line on stderr, empty stdout, no
record written, and the unit fails. The run's stdout JSON
(`{evaluation, owner_report, counts}`) is what
`journalctl --user -u omp-offerings-check.service` shows. Every record keeps the
last successful listing per lane, so a current gap never erases a prior
snapshot.

Daily, the owner inspects the unit status and the newest owner report. An
`urgent_review` finding is reviewed immediately, against the Written rubric
below and the existing `review.last_review` integration. A
`pending_qualification` finding lives only in the evaluation records and waits
for the weekly qualification on the fixed task set. A `check_gap` finding means
the named capture inputs must be repaired.

## Written rubric

| Axis | Required evidence and decision |
| --- | --- |
| Capability | Candidate output artifacts, task digest, passed/failed IDs, and human assessment of task correctness. Every role task must pass. |
| Cost | USD/M input and USD/M output, source URL/artifact and observation date; distinguish subscription inclusion from metered pricing. Record cache costs when applicable. |
| Quota | Subscription entitlement, provider/account concurrency, RPM/TPM when published, and dated source. Policy caps alone do not prove available quota. |
| Latency | Median and p95 completion latency in ms, sample count, prompt/output sizes, effort, measurement window and failures. Compare against recorded role budget. |
| Role fit | Role, effort, revision, context/tool/output constraints, and evidence that the intended lane can meet them. Reviewer independence is part of review fit. |

Record acceptance thresholds before candidate calls. Missing measurements or
unmet thresholds prevent approval. A passing closed-check receipt is necessary,
but does not replace rubric review: substring checks cannot prove arbitrary code
correctness or the quality of a plan. Record rubric pass/fail separately, with
reason and reviewer. Only task pass plus rubric pass qualifies an offering.

## Providers and offerings to review

Snapshot of governing policy inspected 2026-10-14. All rows below have outcome
**pending evidence**, not qualified by this document. No provider calls, pricing
lookup, quota checks or latency measurements were made here. Cost (USD/M
input/output), subscription quota, median/p95 latency and capability results are
**unknown** for every row until sourced/measured. Concurrency numbers below are
local policy caps, not measured provider entitlements.

| Provider | Offering | Policy role/effort and role-fit target | Cap |
| --- | --- | --- | --- |
| `gemini_flash` | `gemini-3.8-flash-high` | Implement/verify E1; E1 tier; patch and verification tasks | 6 |
| `anthropic_fable` | `claude-opus-5-5` | E2 escalation tier; qualify whichever role is escalated | 2 |
| `kimi` | `kimi-code/k3-256k` | Audit/acceptance review E3; E3 tier; acceptance reviewer; defect identification | 2 |
| `chatgpt_astra` | `gpt-6-sol` | E4 tier; qualify escalated role and reserve authorization before use | 0 |
| `ollama_cloud` | `deepseek-v4.1-flash:cloud` | Research E1, best-of-2 fact coverage; Gemini overflow alternate; qualify each substituted role | 4 |
| `openai_codex` | `gpt-5.6-sol` | Plan E2; audit reviewer `openai-codex/gpt-5.6-sol:medium`; qualify plan and review separately | 1 |

Include new offerings from each of these providers in daily comparison, not only
listed models. Include actual judge/operator providers if used. Overflow must
queue or use an owner-approved alternate recorded as a substitution. Review this
snapshot against governing policy each time; it is not generated or authoritative.

## Fixed set and protected checks

[`qualification-tasks.v1.json`](../python/omp-work/src/omp_work/routing/qualification-tasks.v1.json)
freezes bounded planning, patch plus boundary behavior, verification synthesis,
source-based research, support verdicts, defect identification, and operator
instructions. Every role has at least one task. Judge pairs reuse
`intake_support_corpus.judge_input`; only id, claim_text, passage and context reach
the candidate. Gold verdicts, rationale and near-miss tags stay local. Training
labels use the corpus training-label API; held-out gold is never candidate input.

Load with `load_task_set()`. Send only `task_set.candidate_input(task_id)` to
candidate calls, with role instructions supplied by the caller (judge returns a
supports/contradicts/neither verdict). Never send the task object, expected checks,
or the entire JSON file. Checks are supervisor-side data, not access-controlled
secrets: callers must enforce separation. This module makes no provider calls.

The closed checks are:

- `exact_text`: string equals expected bytes, including whitespace.
- `contains_all`: string contains every required substring, case-sensitive.
- `verdict_equals`: string verdict, verdict dictionary, or receipt's verdict
  equals a corpus label exactly.

`qualify(role, outputs)` requires all role task IDs. Wrong answers, missing IDs,
and extra IDs fail. Unknown roles, duplicate task IDs, unknown check types, or
malformed expected values are refused at the file boundary. Judge/operator aliases
map to intake_support_judge/operator_session; audit maps to review.

`task_set_sha256` hashes UTF-8 canonical JSON (sorted keys, compact separators,
ASCII escaping); formatting and object-key order do not change the digest. Input,
check, task order, or ID changes do. Retain old fixed-set files and receipts;
future intentional revisions get a new versioned file. Compare candidates on the
same version/digest; changed content invalidates prior evidence for that set.

## Evidence record

Retain `QualificationResult.to_dict()` with digest, role, passed/failed IDs,
pass/fail outcome, UTC recorded_at, caller, model, provider and rubric_evidence.
Real review records must populate caller/model/provider; omitted metadata is
allowed only for offline grader tests, never for routing approval.

Rubric evidence records effort, offering revision, task-set version, each axis's
measurements/sources and thresholds, candidate-call IDs/output artifact locations,
call timestamps, rubric date/outcome/reason, and reviewer. Preserve raw outputs
from actual candidate calls, never copied gold. Synthetic test outputs exercise
the grader and are not model qualification evidence. The offline grader cannot
establish output provenance; reviewers must check the call artifacts.

Record each review's date/outcome in the governing policy through its designated
integration workflow, linking immutable evidence. Record failed evaluations too.
Routing still requires qualification and, where policy does not settle it, owner
approval. No policy loader, routing caller, or provider configuration changes are
made by this task.
