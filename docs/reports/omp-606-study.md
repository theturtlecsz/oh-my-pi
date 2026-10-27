# OMP-606 real mechanism study

Command:

```sh
OMP_WORK_POSTGRES_INTEGRATION=1 OMP_606_REPORT=docs/reports/omp-606-study.json uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_mechanism_real_study_606.py
```

Frozen before execution: `ae5cc8b0175b75d53f316031de58b5941495105bc13dee5003fa0e0cd0afe342` at 2026-10-16T05:33:34.245285+00:00.
Both generators emit thresholds 0.25, 0.5, 0.75. Matched two-node budgets
keep 180 trials within existing 200-trial read contract. This is a bounded local
comparison, not evidence about larger search budgets. Seeds 0..4 in each of development,
fresh confirmation, and held-out engineering. Simulation draws use separate cohort
namespaces; engineering compresses 40 sampled production files per seed. Task
manifests, source files and constraints are archived in companion JSON. No code or
threshold revised after confirmation or holdout. Caps: `{"node_budget": 2.0, "selection_attempts": 8, "selection_seconds": 30.0, "selection_timeout": 10.0, "width": 1}`.

L1: all actual next-node selections appear under `report.l1` and `runs`.
Controlled ties, gradients and traps pass actual jailed generated code plus protocol
validation; two distinct family fingerprints appear under `controlled`.
A separate intervention changes only trusted receipt metric and changes next choice.

L2: every member contributes; no best-member claim. Overall outcome: **null result**.
Source-domain claim: `{"outcome": "null result", "deltas": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], "inferred": false, "stats": null, "seeds": [{"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "slices": [{"seeds": [{"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "development", "domain": "simulation", "protocol_sha256": "137172c0d8ed410f789ef1b4c90897c2094634087f5d7e804ba925e029fd056d", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "development", "domain": "simulation", "protocol_sha256": "c82eb7da2c632ada94eb91e6c5d73c210ff38a567653e582ef2758aa946005b8", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "development", "domain": "simulation", "protocol_sha256": "4860b252523741811ddc500776776a6de780577d4113c713e9ddda957c2cb609", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "development", "domain": "simulation", "protocol_sha256": "22d62ecd593cf88f63f7d42070d97aa7f1ace17e19ebd73a05a83ff1b597fc59", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "development", "domain": "simulation", "protocol_sha256": "d3a8cc7964cb622eff33e58f733e0078f4e83de5b982ab10d8353bc200848b17", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "simulation", "protocol_sha256": "84f091a77b268ef0dbc19914fc0b4f902eab1090fac91d101d62c5e45f28d056", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "simulation", "protocol_sha256": "256b17651b994649281057ec9c3c7d891492687f876064054bf4904c51d3d66e", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "simulation", "protocol_sha256": "ae6b956a3d0db478ae10d804a3d93170b684211fd88b6261054ad91c99940ed1", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "simulation", "protocol_sha256": "d1b41a6b6d3bf59761086d596c3e66cc2438aed803f0b892da22f6da16f2e027", "metric": "score", "direction": "higher"}, {"seeds": [{"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "simulation", "protocol_sha256": "cd599fcf40c38f50ce2fa156d18bf8bd1d8db31d0c2418e19b962051c16f9ca5", "metric": "score", "direction": "higher"}]}`.
Held-out claim (no transfer inferred from source): `{"outcome": "null result", "deltas": [0.0, 0.0, 0.0, 0.0, 0.0], "inferred": false, "stats": null, "seeds": [{"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}, {"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "slices": [{"seeds": [{"seed": 0, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "engineering", "protocol_sha256": "189dad7c44be4816ba20a6fb70057f84be7cffa2fdd2a6a8798c6d64bd57fce7", "metric": "ratio", "direction": "higher"}, {"seeds": [{"seed": 1, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "engineering", "protocol_sha256": "23e6b1a13217ec4330873215abbbae27f022895a2d435ed0648e1298f3b5b62b", "metric": "ratio", "direction": "higher"}, {"seeds": [{"seed": 2, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "engineering", "protocol_sha256": "7b044588e5e71d5872ec290c57267add2c5dad324920c7113b6aa460d85294a4", "metric": "ratio", "direction": "higher"}, {"seeds": [{"seed": 3, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "engineering", "protocol_sha256": "04b8354d031359a28cdeab84a3334f098a81dba0975bc84cd3affaabd19871f4", "metric": "ratio", "direction": "higher"}, {"seeds": [{"seed": 4, "difference": 0.0, "per_threshold": [{"threshold": 0.25, "difference": 0.0}, {"threshold": 0.5, "difference": 0.0}, {"threshold": 0.75, "difference": 0.0}], "failures": []}], "deltas": [0.0], "outcome": "null result", "inferred": false, "stats": null, "programme_qualification": false, "phase": "confirmation", "domain": "engineering", "protocol_sha256": "4ea450959a40c40d9603af5bd39ff2591c49ff2c1d21dfe9ac3a211faf3034af", "metric": "ratio", "direction": "higher"}]}`.
Protocols differ per task, so pooled CI may be unavailable; per-cohort population
seed deltas and local 95% CI are recorded below. Nulls stay in denominators.

L3: proposed structural change `adaptive-restart.v1 -> root-first.v1`: prefer unopened
roots before descending, keeping threshold grid and caps fixed. Fresh confirmation
all-pairs-positive passed: False.
No standing-change admission requested; no default activated. Measurements cannot
substitute for recursive confirmation authority. Goal retained.

Actual campaign costs: `{"selection_attempts": 180, "node_attempts": 180, "spent": 180.0, "selection_seconds": 141.86942802148405, "node_seconds": 448.2791115876753}`. USD/tokens are not metered by this
CPU-only harness; `spent` means unit node reservations, not dollars. Failed and
canceled attempts remain in `runs`/`report.costs`; no fixture scores used.
Local five-seed decision only; not programme qualification (20 consecutive accepted
trials, 72 hours/30 items across both repos, 100 actual attempts remain separate).

Owner options: revise, defer, retire. Next useful trial estimate:
`{"seeds": 5, "population_members": 6, "node_reservations": 60.0, "selection_attempt_cap": 240, "note": "Local comparison only. It does not count toward programme qualification."}`. Revisions require new frozen cohorts; these
held-out results cannot be reused to tune candidates.

Custody: compressed content-addressed artifacts, receipt bytes, native trial/job/
campaign IDs, evaluator descriptors, worker capabilities, settlements and observations
archived before service teardown. Offline receipt bindings and metrics reverified.
Run-manifest/output hashes are receipt attestations; raw ephemeral output files are
not retained by existing managed runner. Candidate archives, evaluator and sealed
inputs permit rerun. Archive is a local service attestation, not independently signed.
No credentials exported. HTTP and PostgreSQL fixtures stop at test teardown.

Population seed estimates (local only):

```json
{
  "development": {
    "seeds": [
      {
        "seed": 0,
        "difference": 0.0
      },
      {
        "seed": 1,
        "difference": 0.0
      },
      {
        "seed": 2,
        "difference": 0.0
      },
      {
        "seed": 3,
        "difference": 0.0
      },
      {
        "seed": 4,
        "difference": 0.0
      }
    ],
    "deltas": [
      0.0,
      0.0,
      0.0,
      0.0,
      0.0
    ],
    "outcome": "null result",
    "inferred": true,
    "stats": {
      "mean": 0.0,
      "sample_stdev": 0.0,
      "margin": 0.0,
      "confidence_interval_95": [
        0.0,
        0.0
      ],
      "ci_lower": 0.0,
      "ci_upper": 0.0,
      "null_result": true,
      "outcome": "null result",
      "conclusion": "null result"
    },
    "programme_qualification": false
  },
  "confirmation": {
    "seeds": [
      {
        "seed": 0,
        "difference": 0.0
      },
      {
        "seed": 1,
        "difference": 0.0
      },
      {
        "seed": 2,
        "difference": 0.0
      },
      {
        "seed": 3,
        "difference": 0.0
      },
      {
        "seed": 4,
        "difference": 0.0
      }
    ],
    "deltas": [
      0.0,
      0.0,
      0.0,
      0.0,
      0.0
    ],
    "outcome": "null result",
    "inferred": true,
    "stats": {
      "mean": 0.0,
      "sample_stdev": 0.0,
      "margin": 0.0,
      "confidence_interval_95": [
        0.0,
        0.0
      ],
      "ci_lower": 0.0,
      "ci_upper": 0.0,
      "null_result": true,
      "outcome": "null result",
      "conclusion": "null result"
    },
    "programme_qualification": false
  },
  "heldout": {
    "seeds": [
      {
        "seed": 0,
        "difference": 0.0
      },
      {
        "seed": 1,
        "difference": 0.0
      },
      {
        "seed": 2,
        "difference": 0.0
      },
      {
        "seed": 3,
        "difference": 0.0
      },
      {
        "seed": 4,
        "difference": 0.0
      }
    ],
    "deltas": [
      0.0,
      0.0,
      0.0,
      0.0,
      0.0
    ],
    "outcome": "null result",
    "inferred": true,
    "stats": {
      "mean": 0.0,
      "sample_stdev": 0.0,
      "margin": 0.0,
      "confidence_interval_95": [
        0.0,
        0.0
      ],
      "ci_lower": 0.0,
      "ci_upper": 0.0,
      "null_result": true,
      "outcome": "null result",
      "conclusion": "null result"
    },
    "programme_qualification": false
  }
}
```

Offline replay: archived candidate/evaluator/protocol/sealed inputs reproduced exact metrics and output hashes for one simulation and one engineering trial, without DB, HTTP, bearer or source-file access. Two extra candidate/evaluator/negative-control replays are verification costs, excluded from population inference.

Controlled probe costs: `{"selection_attempts": 50, "selection_seconds": 35.35991901403759, "scored_experiment_attempts": 0}`.

Execution history: 610 native jobs across this task: 180 scored in the final comparison, 180 in a preceding validation run, and 250 in the excluded five-node infrastructure attempt. Only final comparison contributes to reported deltas. No candidate source changed between attempts. Initial five-node design exceeded the existing 200-trial read limit; controller-visible cost counts understated actual native attempts. Its full cost ledger and native IDs are retained under `execution_history.excluded_infrastructure_attempt`; original custody archive remains at `40bdf4ece6:docs/reports/omp-606-study.json` with recorded SHA-256. Matched two-node study was frozen before execution; no candidate was tuned for a win.

Preceding validation completed all native comparisons, receipt checks and controlled probes, then exposed a missing required cancel argument in the offline replay helper. After that test-only call fix, archived simulation and engineering replay passed with local services already stopped. The required command was then rerun successfully end to end (3 passed). Additional validation costs, controlled probes and two offline replays are retained separately under `execution_history.validation_attempt_before_cancel_fix`. Native costs for that run: `{"selection_attempts": 180, "node_attempts": 180, "selection_seconds": 142.6176839885302, "node_seconds": 445.9082946877461, "spent": 180}`. None count toward programme qualification.
