"""Required proof must affect readiness before the unchanged legacy scorer."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work.nsi6_observation import project_observation
from omp_work.nsi_eval import (
    aggregate, evaluate_current, evaluate_typed, observe, ratio, replay, score,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def corpus():
    directory = ROOT / "evals/nsi6/v1"
    return load_corpus(directory / "inputs", directory / "nsi6-manifest.json")


@pytest.fixture(scope="module")
def shadow():
    # Stored historical evidence: no credentials, model calls or database needed.
    return json.loads((ROOT / "docs/nsi6/shadow.json").read_bytes())


@pytest.mark.parametrize("evaluator", [evaluate_current, evaluate_typed])
@pytest.mark.parametrize("name", ["stale-source", "stale-revision", "tampered-provenance"])
def test_required_proof_blocks_legacy_false_ready_before_scoring(corpus, shadow, evaluator, name):
    case = next(case for case in corpus["cases"] if case["id"] == name)
    stored = next(row for row in shadow["cases"] if row["id"] == name)
    assessment = observe(evaluator, evaluator_input(case))
    proof = stored["provenance"]
    before = deepcopy((assessment, proof))
    # Characterize the historical bug, which existing replay tests preserve.
    legacy = replay({"cases": [case]}, lambda raw: deepcopy(proof))
    assert legacy["cases"][0]["arms"]["typed"]["observation"]["readiness"] == "READY_FOR_RATIFICATION"
    assert score(assessment, case["gold"])["false_ready"] == ratio(1, 1)

    projected = project_observation(assessment, proof, proof_required=True)
    # This correct-readiness assertion fails on the old replay observation.
    assert projected["readiness"] != "READY_FOR_RATIFICATION"
    assert projected["status"] == "refused"
    assert score(projected, case["gold"])["false_ready"] == ratio(0, 1)
    assert projected["assessment"] == assessment
    assert projected["proof"] == proof
    assert (assessment, proof) == before
    assert projected["projection_diagnostics"]


def test_current_valid_proof_is_eligible_without_inventing_requirements(corpus, shadow):
    case = next(case for case in corpus["cases"] if case["id"] == "ready-current-proof")
    proof = next(row["provenance"] for row in shadow["cases"] if row["id"] == case["id"])
    assessment = observe(evaluate_typed, evaluator_input(case))
    assessment["model_tokens"] = 17
    proof = {**proof, "model_tokens": 5}
    projected = project_observation(assessment, proof, proof_required=True)
    assert projected["status"] == "ok"
    assert projected["readiness"] == "READY_FOR_RATIFICATION"
    metrics = score(projected, case["gold"])
    assert metrics["criterion_coverage"] == ratio(4, 4)
    assert metrics["critical_detection"] == ratio(4, 4)
    assert metrics["model_tokens"] == 22

    no_proof = project_observation(assessment, None, proof_required=False)
    assert no_proof["readiness"] == "READY_FOR_RATIFICATION"
    assert no_proof["status"] == "ok"
    # A valid proof cannot promote an arm's actual non-ready assessment.
    assessment["readiness"] = "NEEDS_SPEC"
    assert project_observation(assessment, proof, proof_required=True)["readiness"] == "NEEDS_SPEC"


@pytest.mark.parametrize("patch", [
    {"provenance_valid": False},
    {"stale_revision": True},
    {"current_revision": 1},
    {"stale_source": True},
    {"binding_matches_baseline": False},
    {"readiness": "NEEDS_SPEC"},
])
def test_proof_verdict_cannot_leave_arm_ready(corpus, shadow, patch):
    case = next(case for case in corpus["cases"] if case["id"] == "ready-current-proof")
    proof = next(row["provenance"] for row in shadow["cases"] if row["id"] == case["id"])
    assessment = observe(evaluate_current, evaluator_input(case))
    projected = project_observation(assessment, {**proof, **patch}, proof_required=True)
    assert projected["readiness"] == "NEEDS_SPEC"
    assert projected["status"] == "refused"


@pytest.mark.parametrize("component", ["assessment", "proof"])
def test_failures_keep_denominators_and_crash_skip_are_not_safety(corpus, component):
    case = next(case for case in corpus["cases"] if case["id"] == "omitted-security")
    assessment = observe(evaluate_current, evaluator_input(case))
    proof = {"status": "ok", "readiness": "READY_FOR_RATIFICATION",
             "provenance_valid": True, "binding_matches_baseline": True,
             "current_revision": 0, "pinned_revision": 0,
             "model_tokens": 0, "wall_seconds": 0}
    rows = []
    for status in ("invalid", "refused", "crashed", "skipped"):
        failed = {"status": status, "diagnostics": ["component failed"],
                  "model_tokens": 0, "wall_seconds": 0}
        projected = project_observation(
            failed if component == "assessment" else assessment,
            failed if component == "proof" else proof, proof_required=True,
        )
        metrics = score(projected, case["gold"])
        assert projected["status"] == status
        assert projected["readiness"] != "READY_FOR_RATIFICATION"
        assert metrics["safety_evidence"] is (status in {"invalid", "refused"})
        assert projected["safety_evidence"] == metrics["safety_evidence"]
        rows.append({"id": status, "arms": {"pipeline": {"observation": projected, "metrics": metrics}}})
    metrics = aggregate(rows, "pipeline")
    assert metrics["false_ready"] == ratio(0, 4)
    assert metrics["criterion_coverage"] == ratio(0, 16)
    assert metrics["critical_detection"] == ratio(0, 16)
    missing = project_observation(assessment, None, proof_required=True)
    assert missing["status"] == "skipped"
    assert score(missing, case["gold"])["safety_evidence"] is False


def test_stored_equal_metrics_hide_distinct_raw_records(corpus, shadow):
    cases = {case["id"]: case for case in corpus["cases"]}
    for arm in ("current", "typed"):
        assert shadow["aggregate"][arm]["false_ready"] == ratio(13, 19)
    assert shadow["provenance"]["valid_proofs"] == ratio(1, 4)
    for row in shadow["cases"]:
        current = row["arms"]["current"]["observation"]
        typed = row["arms"]["typed"]["observation"]
        gold = cases[row["id"]]["gold"]
        current_metrics, typed_metrics = score(current, gold), score(typed, gold)
        # Runtime varies; score's behavioral projection does not.
        current_metrics.pop("wall_seconds")
        typed_metrics.pop("wall_seconds")
        assert current_metrics == typed_metrics
    ready = next(row for row in shadow["cases"] if row["id"] == "ready-complete")
    assert ready["arms"]["current"]["observation"]["raw"] != ready["arms"]["typed"]["observation"]["raw"]
