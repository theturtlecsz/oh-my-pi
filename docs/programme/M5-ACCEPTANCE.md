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

## Outcome behaviours

Three behaviours decide the autoresearch outcome. Presence checks (`code_presence`, `fixture_only`, `self_score`) and `unknown_as_zero` never satisfy one.

```json
{
  "item": "OMP-319",
  "satisfied": false,
  "behaviours": [
    {
      "id": "adaptivity-vs-baseline",
      "observation": "B2 versus B3 on a matched budget. Equivalent proposals terminate; equivalent repeats terminate.",
      "pass": "B3 changes its next selection when a trusted observation changes, on B2's budget.",
      "fail": "Untrusted score or equivalent repeat.",
      "inconclusive": "Matched B2 and B3 arms are missing.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    },
    {
      "id": "held-out-transfer",
      "observation": "Held-out confirmation outside development, content disjoint; outside domain transfers.",
      "pass": "Every outside-development confirmation delta is positive and the content is disjoint.",
      "fail": "Not disjoint, or an outside delta is non-positive.",
      "inconclusive": "No outside-development delta is recorded.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    },
    {
      "id": "measured-improvement",
      "observation": "Fresh downstream interval, known costs with unknown staying unknown, continuing R18. Unsupported 10x is rejected.",
      "pass": "The interval lies entirely above zero and every cost axis is known.",
      "fail": "Untrusted, repeated, unknown-as-zero, or below zero.",
      "inconclusive": "Zero is inside the interval, a bound or cost is unknown, or drift has no fresh window.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    }
  ],
  "rules_text": [
    "Unknown safety or authority is not permission.",
    "Noncritical uncertainty is a bounded investigation with the assumption recorded.",
    "Only a consequential unresolved decision goes to Chris.",
    "A null or negative comparison is not measured improvement.",
    "Five seeds are not programme qualification.",
    "No useful benefit leaves the capability off and offers Chris revise, defer, or retire.",
    "Technical delivery at merge is not product acceptance."
  ],
  "rules": {
    "q1_unknown_authority_is_permission": false,
    "q2_null_is_improvement": false,
    "q2_five_seeds_are_programme_qualification": false,
    "q3_merge_is_product_acceptance": false
  },
  "examples": [
    {
      "id": "matched-gain",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5
      },
      "decisions": ["pass", "pass", "pass"],
      "programme": false
    },
    {
      "id": "null-interval",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": -0.01,
        "hi": 0.05,
        "seeds": 5
      },
      "decisions": ["pass", "pass", "inconclusive"],
      "programme": false
    },
    {
      "id": "transfer-loss",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": -0.1},
        "lo": -0.2,
        "hi": -0.05,
        "seeds": 5
      },
      "decisions": ["pass", "fail", "fail"],
      "programme": false
    },
    {
      "id": "unknown-cost",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "costs_known": false
      },
      "decisions": ["pass", "pass", "inconclusive"],
      "programme": false
    },
    {
      "id": "code-presence",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "code_presence": true,
        "fixture_only": true
      },
      "decisions": ["fail", "fail", "fail"],
      "programme": false
    },
    {
      "id": "untrusted",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "trusted": false
      },
      "decisions": ["fail", "pass", "fail"],
      "programme": false
    },
    {
      "id": "cycle",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "equivalent_repeat": true
      },
      "decisions": ["fail", "pass", "fail"],
      "programme": false
    },
    {
      "id": "drift",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "drift": true,
        "new_window": false
      },
      "decisions": ["pass", "pass", "inconclusive"],
      "programme": false
    },
    {
      "id": "same-domain",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"ml": 0.2},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "disjoint": false
      },
      "decisions": ["pass", "fail", "pass"],
      "programme": false
    },
    {
      "id": "unknown-as-zero",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "unknown_as_zero": true,
        "costs_known": false
      },
      "decisions": ["pass", "pass", "fail"],
      "programme": false
    },
    {
      "id": "self-score",
      "record": {
        "arm_b2": true,
        "arm_b3": true,
        "same_budget": true,
        "observation_changes_next": true,
        "development": ["ml"],
        "deltas": {"engineering": 0.1},
        "lo": 0.02,
        "hi": 0.2,
        "seeds": 5,
        "self_score": true
      },
      "decisions": ["fail", "fail", "fail"],
      "programme": false
    }
  ]
}
```

## Oracle

The oracle reads one record and returns one decision per behaviour. Defaults: every boolean is `false` except `trusted`, `disjoint`, `costs_known`, and `new_window`, which are `true`; `deltas` is `{}`; `development` is `[]`; `bounds` is `null`; every count is `0`.

A delta is **outside** when its key is absent from `development`.

Presence never decides. When `code_presence`, `fixture_only`, or `self_score` is set, all three behaviours fail.

**Adaptivity.** Pass when `arm_b2`, `arm_b3`, `same_budget`, and `observation_changes_next` are true, `trusted` is true, and `equivalent_repeat` is false. Fail when `trusted` is false or `equivalent_repeat` is true. Otherwise inconclusive.

**Transfer.** Fail when `disjoint` is false, or when any outside delta is `<= 0`. Pass when at least one outside delta exists and every one is `> 0`. Otherwise inconclusive.

**Improvement.** Fail before any inconclusive: fail when `trusted` is false, `equivalent_repeat` is true, `unknown_as_zero` is true, or `costs_known` is true while `hi < 0`. Inconclusive when (`drift` and not `new_window`), or `costs_known` is false, or `lo` is null, or `hi` is null, or `lo <= 0 <= hi`. Pass when `lo > 0`.

**Programme.** Qualifies only when `seeds >= 20`, `hours >= 72`, `items >= 30`, and `attempts >= 100`.
