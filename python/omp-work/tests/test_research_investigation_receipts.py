"""run_ml binds each experiment to its own receipt digest and admits the lesson through it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omp_work.research.investigation import run_literature, run_ml
from omp_work.research.lessons import LessonDraft, stage
from omp_work.research.mlsim import artifact_sha256
from omp_work.v1.canonical import canonical_json

_CLAIM = "Mean gain can mislead when median gain is negative"


def _run(tmp_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    run_literature(tmp_path / "lit")
    result = run_ml(tmp_path / "ml", tmp_path / "lit")
    dossier = json.loads((tmp_path / "ml" / "dossier.json").read_text(encoding="utf-8"))
    return result, dossier["experiments"], tmp_path / "ml"


def _draft(receipt_ids: list[str]) -> LessonDraft:
    # Binding values recomputed from the fixture's tree and search settings.
    tree = {
        "A1": {"parent_id": None, "metric": 0.4, "cost": 1, "children": ["A2"], "infra_error": None},
        "A2": {"parent_id": "A1", "metric": 0.9, "cost": 1, "children": [], "infra_error": None},
        "B1": {"parent_id": None, "metric": 0.7, "cost": 1, "children": [], "infra_error": None},
    }
    return LessonDraft(
        lesson_id="lesson-scientific-1",
        project_id="ml-project",
        kind="scientific",
        title="Scientific Lesson on Aggregate Metrics",
        claim=_CLAIM,
        receipt_ids=receipt_ids,
        binding={
            "source_span": f"1:{1 + len(_CLAIM)}",
            "revision": artifact_sha256(canonical_json(tree)),
            "work_item": "ml-project",
            "candidate": artifact_sha256(canonical_json({"budget": 3, "width": 2})),
            "evaluator": "winner",
        },
        outcome="PASS",
        behaviour={"metric": "metric", "op": "ge", "value": 0.7},
    )


def _records(experiments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    draft = _draft([])
    return {
        e["receipt_sha256"]: {
            **e,
            "project_id": "ml-project",
            "claim": _CLAIM,
            "outcome": "PASS",
            "binding": dict(draft.binding),
        }
        for e in experiments
    }


def test_each_experiment_hash_is_its_preimage_digest(tmp_path: Path) -> None:
    _, experiments, _ = _run(tmp_path)
    assert [e["name"] for e in experiments] == ["best_of_n", "beam"]
    for e in experiments:
        preimage = {"name": e["name"], "metrics": e["metrics"]}
        assert e["receipt_sha256"] == artifact_sha256(canonical_json(preimage))
    assert experiments[0]["receipt_sha256"] != experiments[1]["receipt_sha256"]
    assert "0" * 64 not in {e["receipt_sha256"] for e in experiments}


def test_resolver_records_admit_lesson_and_missing_receipt_rejects(tmp_path: Path) -> None:
    result, experiments, _ = _run(tmp_path)
    records = _records(experiments)
    ids = [e["receipt_sha256"] for e in experiments]
    assert [records.get(i)["name"] for i in ids] == ["best_of_n", "beam"]

    staged = stage(_draft(ids), tmp_path / "ok", resolve=records.get)
    assert staged["status"] == "staged"

    def missing(receipt_id: str) -> Any:
        raise LookupError(receipt_id)

    rejected = stage(_draft(ids), tmp_path / "none", resolve=missing)
    assert rejected["status"] == "rejected"
    assert not (tmp_path / "none").exists()

    assert result["staged_lessons"] == ["lesson-scientific-1"]
    assert result["rejected_lessons"] == ["lesson-narrative-1"]
