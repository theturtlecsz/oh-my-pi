# M5 acceptance: bounded recursive method study

OMP-319-s06 composes R19 s01–s05 and continuing R18 through
`omp_work.research.recursive_lab.run_study(input_path, output_dir,
chosen_method_sha256=None)`. Evidence here is **fixture source proof only**.
It is not real adoption, owner approval, or installed qualification.
Waiting for Chris: **s07/s08**. No installed default is activated.

## Clause-to-test map

All test names below are in `python/omp-work/tests/test_research_m5_study.py`.

| Clause | Observable proof | Test |
| --- | --- | --- |
| s01 standing admission and s02 changed recipe | Live caller context goes through ProgramCoordinator and run_method; both generated recipes retain study, lineage, spec, generator, source and bundle identities. Baseline chooses `b`; changed recipe chooses `a1`. | `test_two_domains_execute_changed_recipe_with_native_evidence` |
| s03 fresh downstream mechanisms and transfer | Development ML, fresh confirmation ML and engineering cohorts use API-registered datasets and separately sealed native trial references. Engineering remains a separate transfer domain. | `test_two_domains_execute_changed_recipe_with_native_evidence` |
| s04 costs and usage | Native usage IDs, generation/proposal compute and separate quality/cost axes persist. Unknown ownership/compute costs block eligibility instead of becoming zeros. | `test_two_domains_execute_changed_recipe_with_native_evidence`, `test_unknown_native_tokens_remain_unknown_in_cost_projection` |
| Failed attempts survive | Changed downstream source is rejected; admitted trial denominator and failure history remain, with both frozen methods. | `test_failed_downstream_attempt_retains_denominator_and_both_methods` |
| Regression and retirement | Negative transfer deltas propose rollback to `named-stable-method`, bound to baseline source/bundle; retired candidate bytes and observations stay in packet. Baseline can be chosen for this bounded run. | `test_transfer_regression_proposes_named_retirement_preserving_evidence` |
| Drift requires fresh admission | Without another window, maintenance emits pending QuestionItem. A supplied input with same decision stays pending and does not execute. | `test_drift_without_new_authorization_emits_pending_question`, `test_drift_reevaluation_requires_separate_decision_window` |
| Authorized reevaluation reuses pipeline | Separate fixture decision/window executes both recipes and fresh cohorts through same pipeline, sharing consumption history. Original drift evidence and child packet digest remain. | `test_separately_authorized_drift_runs_same_pipeline_and_retains_prior_packet` |
| Durable dossier | Shared writer renders report/table, verifies sources, rerenders and reloads packet and ProgramCoordinator. Changed JSON, report, candidate bytes or program snapshot fail verification. | `test_two_domains_execute_changed_recipe_with_native_evidence`, `test_tampered_packet_rejected` |

## Input and identity contract

Input is caller-supplied JSON, not an authority issuer. `baseline` and `candidate`
each contain a full `RecursiveStudy` and `development_traces` keyed by bound
TaskEvidence task ID. `controller` supplies explicit local OperationsConfig,
workspace/actor/work IDs, both original grants, lineage states, standing policies,
authorities, actions, repository records, and distinct generation/invocation
operation and request IDs. Neither dossier restoration nor notes create grants.

`cohorts` are s03 CohortTask records: dataset digests, phase/domain, protocol,
metric/direction, seed/width/budget and native baseline/candidate trial IDs.
They must already exist in local custody. Lab creates no experiments, workers,
scheduler or receipts and calls no paid models or hardware.

`maintenance_policy`, `quality_bound`, `ownership_costs`, `release` and
`upstream_check` bind preregistered bounds, measured or explicitly unknown cost
axes, candidate bundle and local upstream-check evidence. Maintenance decision
reference must match caller grant. Bounds must predate observation window.
Release ID names generated bundle digest; rollback target names stable baseline
and binds its bundle and generated source digest. Costs do not infer compute,
owner time or productivity from resource caps. Native usage timestamps remain
in packet; cost projection uses separately recorded observation time, not a
claim that native events occurred simultaneously.

`chosen_method_sha256` selects **generated source SHA-256**, because s02's
`method_sha256` identifies shared recipe executor implementation. Packet retains
both identities and recipe/spec/lineage pins. An ambiguous or absent requested
source digest is rejected. Omitted choice selects candidate. Return contains
`packet_sha256`, `used_method_sha256`, `eligible`; eligibility is a bounded
candidate evidence result, never permission to activate or deploy. Fixture
results remain ineligible where costs are unknown.

Optional `reevaluation_input_path` names another complete input, with separately
supplied decision and window active at invocation. Same decision/window or
inactive window leaves original QuestionItem pending. Authorized reruns share
consumption history, so new IDs cannot launder old cohort content. Parent packet
pins child packet digest; verification checks both. No scheduler is installed.

## Verified results and limits

Focused s01–s05 verification:

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_recursive*.py -q
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_m5_study.py python/omp-work/tests/test_research_recursive_transfer.py -q
```

Initial s01–s05 run: 590 passed, 9 service-stale setup errors after source changed
while its fixture service was alive. Source was then frozen; affected transfer
file rerun passed all 36 cases, together with the then-current 10 M5 cases
(46 passed). No existing test or source guard was changed to suppress errors.
This covers 599 distinct s01–s05 cases with passing results.

Final source-frozen M5 verification:

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_m5_study.py
```

Result: **11 passed**, one existing Starlette/httpx deprecation warning,
76.59 seconds. Source-proof observations: ML and engineering both select
baseline `b` versus candidate `a1`; positive confirmation delta is 0.1;
engineering alone transfers outside development domain. Native denominators
are 6 for ML and 8 for engineering, including rejected selected-source attempts.
Each of six proposals has a distinct native request ID; cumulative state has
five usage entries per arm (prior fixture generation, this generation, three
proposals). Unknown native tokens remain unknown in candidate operating costs.
Regression proposes named stable rollback; drift stays pending or runs another
fully admitted pipeline with fresh content and retained original evidence.

Inventory verification:

```sh
git checkout -- python/omp-work/src/omp_work.egg-info
bun scripts/upstream-inventory.ts --write
bun scripts/upstream-inventory.ts
```

Frozen egg-info path is not tracked in this checkout; requested checkout reports
no matching path, and no frozen metadata or inventory rows were changed.
Own inventory rows were described and deduplicated. Read-only inventory check
passes with 2380 diverging paths. No full suite or gate script was run.
Fixture trials exercise real service commands, native job settlement and artifact
custody; fixture scores and owner signatures are test inputs. They are not
measurements of deployed research productivity or evidence of Chris's approval.
Local upstream-check fixture pins prove interpretation and digest retention,
not a real upstream adoption decision. No approval, deployment or installed
qualification was performed. s07/s08 remain owner work.
