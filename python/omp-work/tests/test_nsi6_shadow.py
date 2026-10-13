"""Shadow replay keeps unsafe readiness, failures and uncapped findings visible."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from omp_work.nsi6_corpus import evaluator_input, load_corpus
from omp_work import nsi_eval_postgres
from omp_work.v1.canonical import sha256
from omp_work.nsi_eval import (
    aggregate, evaluate_current, evaluate_typed, observe, ratio, replay, run, score,
)
from omp_work.nsi_eval_postgres import evaluate_fixture
from omp_work.v1.store_shared import WorkStoreError
from omp_work.v1.intake_provenance import helper_sources, referenced_schemas
from test_workflow_service import service  # noqa: F401

CORPUS = Path(__file__).resolve().parents[3] / "evals/nsi6/v1"


@pytest.fixture(scope="module")
def cases():
    return {row["id"]: row for row in load_corpus(CORPUS / "inputs", CORPUS / "nsi6-manifest.json")["cases"]}


@pytest.mark.parametrize("evaluator", [evaluate_current, evaluate_typed])
def test_false_ready_and_lost_criteria_are_visible(cases, evaluator):
    case = cases["silent-omitted-security"]
    observation = observe(evaluator, evaluator_input(case))
    metrics = score(observation, case["gold"])
    assert observation["status"] == "ok"
    assert observation["readiness"] == "READY_FOR_RATIFICATION"
    assert metrics["false_ready"] == ratio(1, 1)
    assert metrics["criterion_coverage"] == ratio(3, 4)
    assert metrics["undetected_critical_behaviors"] == ["security"]
    assert metrics["semantic_edit_count"] == 2  # Missing constraint and criterion.
    assert metrics["model_tokens"] == 0
    assert observation["wall_seconds"] >= 0


@pytest.mark.parametrize("evaluator", [evaluate_current, evaluate_typed])
def test_uncapped_findings_survive_two_question_limit(cases, evaluator):
    raw = evaluator_input(cases["ready-complete"])
    for criterion in raw["bounded"]["draft"]["acceptance_criteria"]:
        criterion["oracle"] = None
    observation = observe(evaluator, raw)
    assert observation["status"] == "ok"
    assert observation["readiness"] == "NEEDS_SPEC"
    assert len(observation["findings"]) == 4
    assert len(observation["questions"]) == 2
    assert {f["target"] for f in observation["findings"]} == {"export", "security", "rollback", "failure"}


def test_repeat_questions_count_per_target_without_inflating_material_coverage(cases):
    case = cases["omitted-security"]
    observation = observe(evaluate_current, evaluator_input(case))
    observation["questions"] = [{"target": "security"}, {"target": "security"}, {"target": "scope"}]
    metrics = score(observation, case["gold"])
    assert metrics["material_questions_by_target"] == {"security": 2}
    assert metrics["redundant_repeats_by_target"] == {"security": 1}
    assert metrics["question_counts_by_target"] == {"security": 2, "scope": 1}


@pytest.mark.parametrize("evaluator", [evaluate_current, evaluate_typed])
def test_invalid_hashes_are_not_readiness_or_coverage(cases, evaluator):
    case = cases["tampered-source-hash"]
    observation = observe(evaluator, evaluator_input(case))
    assert observation["status"] == "invalid"
    assert observation["diagnostics"]
    metrics = score(observation, case["gold"])
    assert metrics["false_ready"] == ratio(0, 1)
    assert metrics["criterion_coverage"] == ratio(0, 4)
    assert len(metrics["undetected_critical_behaviors"]) == 4


def test_refusal_crash_and_skip_remain_distinct_and_in_denominators(cases):
    case = cases["omitted-security"]
    def refused(_raw):
        raise WorkStoreError("invalid_request", ("inconsistent hash",))
    def crashed(_raw):
        raise RuntimeError("broken evaluator")
    observations = [observe(refused, evaluator_input(case)), observe(crashed, evaluator_input(case)),
                    {"status": "skipped", "model_tokens": 0, "wall_seconds": 0}]
    assert observations[0]["diagnostics"] == ["inconsistent hash"]
    assert [obs["status"] for obs in observations] == ["refused", "crashed", "skipped"]
    rows = [{"id": str(i), "arms": {"current": {"observation": obs, "metrics": score(obs, case["gold"])}}}
            for i, obs in enumerate(observations)]
    result = aggregate(rows, "current")
    assert result["false_ready"] == ratio(0, 3)
    assert result["criterion_coverage"] == ratio(0, 12)
    assert result["critical_detection"] == ratio(0, 12)
    assert rows[0]["arms"]["current"]["metrics"]["safety_evidence"] is True
    assert all(not row["arms"]["current"]["metrics"]["safety_evidence"] for row in rows[1:])
    assert ratio(0, 0) == {"numerator": 0, "denominator": 0, "value": None}


def test_replay_copies_each_arm_and_preserves_paraphrase_denominators(cases, monkeypatch):
    selected = [deepcopy(cases[name]) for name in ("ready-paraphrase-a", "ready-paraphrase-b")]
    original = deepcopy(selected)
    def poison(raw):
        raw["bounded"]["draft"]["source"]["sha256"] = "0" * 64
        raise RuntimeError("failed after mutation")
    monkeypatch.setattr("omp_work.nsi_eval.evaluate_current", poison)
    report = replay({"cases": selected}, lambda raw: {})
    assert selected == original
    assert all(row["arms"]["typed"]["observation"]["status"] == "ok" for row in report["cases"])
    assert report["paraphrase_agreement"]["current"]["agreement"] == ratio(0, 1)
    assert report["paraphrase_agreement"]["typed"]["agreement"] == ratio(1, 1)
    assert report["provenance"]["statuses"] == {"skipped": 2}


@pytest.mark.parametrize("name,status,diagnostic", [
    ("ready-current-proof", "ok", None),
    ("tampered-provenance", "refused", "inconsistent hash"),
    ("stale-source", "refused", "inconsistent source"),
    ("stale-revision", "ok", "stale revision"),
])
def test_nsi2_persisted_tampering_and_staleness_separate_from_bounded(cases, service, monkeypatch, name, status, diagnostic):
    raw = evaluator_input(cases[name])
    before = deepcopy(raw)
    bounded = observe(evaluate_typed, raw)
    transactions = []
    evaluator = nsi_eval_postgres.evaluate_provenance
    def checked(cur, body):
        transactions.append(cur.execute("SHOW transaction_read_only").fetchone()[0])
        result = evaluator(cur, body)
        cur.execute("SELECT count(*) FROM omp_work.intake_assessments WHERE workspace_id=%s AND run_id=%s",
                    (body.workspace_id, body.snapshot.run_id))
        assert cur.fetchone() == (0,)
        return result
    monkeypatch.setattr(nsi_eval_postgres, "evaluate_provenance", checked)
    proof = observe(lambda value: evaluate_fixture(service, value), raw)
    assert transactions == ["on", "on"]
    assert raw == before
    assert bounded["readiness"] == "READY_FOR_RATIFICATION"
    assert proof["status"] == status, proof
    if diagnostic:
        assert diagnostic in proof["diagnostics"]
    else:
        assert proof["provenance_valid"] is True
        assert proof["binding_matches_baseline"] is True
        assert proof["raw"]["receipts"]
    if name == "stale-revision":
        # Existing evaluator can analyze historical proof; freshness reported separately.
        assert proof["readiness"] == "READY_FOR_RATIFICATION"
        assert proof["current_revision"] == 1
        assert proof["pinned_revision"] == 0
        assert proof["provenance_valid"] is False


def test_cli_pins_replay_and_binds_rollback_before_sealing(tmp_path):
    proof = tmp_path / "rollback.json"
    proof.write_text('{"verified": true, "receipt": "offline rollback rehearsal"}\n')
    destination = tmp_path / "report.json"
    root = CORPUS.parents[2]
    result = subprocess.run([
        sys.executable, "-m", "omp_work.nsi_eval", "run", "--manifest",
        "python/omp-work/tests/fixtures/nsi6-manifest.json", "--out", str(destination),
        "--rollback-proof", str(proof),
    ], cwd=root, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    report = json.loads(destination.read_bytes())
    assert report["sealed"] is True
    assert report["rollback_proof"]["sha256"] == hashlib.sha256(proof.read_bytes()).hexdigest()
    seal = report.pop("report_sha256")
    assert seal == sha256(report)
    assert report["corpus"]["manifest_sha256"] == json.loads((CORPUS / "nsi6-manifest.json").read_bytes())["manifest_sha256"]
    assert report["pins"]["helper_sources"] == sha256(helper_sources())
    assert report["pins"]["referenced_schemas"] == sha256(referenced_schemas())
    assert report["pins"]["implementation_sources"]["omp_work.nsi_eval"] == hashlib.sha256((root / "python/omp-work/src/omp_work/nsi_eval.py").read_bytes()).hexdigest()
    assert report["model_tokens"] == 0
    assert report["aggregate"]["current"]["false_ready"]["numerator"] > 0
    assert report["aggregate"]["typed"]["false_ready"]["denominator"] == 19
    assert report["aggregate"]["typed"]["statuses"].get("crashed", 0) == 0
    assert report["provenance"]["statuses"] == {"skipped": 22, "refused": 2, "ok": 2}, report["provenance"]["outcomes"]
    assert report["provenance"]["valid_proofs"] == ratio(1, 4)
    assert report["wall_seconds"] > 0
    assert not list(root.glob(".nsi6-*"))


def test_report_cannot_write_corpus(cases):
    with pytest.raises(ValueError, match="overwrite frozen corpus"):
        run(CORPUS / "nsi6-manifest.json", CORPUS / "inputs" / "report.json")


def test_postgres_startup_failure_still_reports_bounded_and_proof_denominators(tmp_path, monkeypatch):
    @contextmanager
    def broken():
        raise RuntimeError("initdb unavailable")
        yield  # Context manager never enters.
    monkeypatch.setattr("omp_work.nsi_eval.disposable_provenance", broken)
    report = run(CORPUS / "nsi6-manifest.json", tmp_path / "failed.json")
    assert report["sealed"] is False
    assert "report_sha256" not in report
    assert report["aggregate"]["typed"]["false_ready"]["denominator"] == 19
    assert report["provenance"]["statuses"] == {"skipped": 22, "crashed": 4}
    assert report["provenance"]["valid_proofs"] == ratio(0, 4)
    for observation in report["provenance"]["outcomes"].values():
        assert observation["safety_evidence"] is False
        assert "initdb unavailable" in observation["diagnostics"][0]
