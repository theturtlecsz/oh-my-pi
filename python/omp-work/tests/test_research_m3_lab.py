"""M3 acceptance proof: mechanism lab and qualified harness promotion (OMP-317).

Proves a generated search mechanism is compared with greedy, rejected when the
strategy contract fails, and confirmed on a fresh cohort (R13), then packaged
as evaluated bytes with independent gates, a canary rollback, a schema-aware
rollback, and an evaluator-change refusal (R14).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from omp_work.research import search
from omp_work.research.harness_release import (
    new_manifest,
    package_candidate,
    record_gate,
)
from omp_work.research.mechanism import generation_job
from omp_work.research.mechanism_lab import (
    BROKEN_SOURCE,
    CRASH_SOURCE,
    OVERSIZED_SOURCE,
    TAMPER_SOURCE,
    WRONG_INTERFACE_SOURCE,
    run_lab,
    run_promotion,
)
from omp_work.research.promotion import (
    CanaryObservation,
    EvaluatorChange,
    canary_verdict,
    check_promotion,
    confirmation_valid,
    plan_rollback,
    requires_fresh_confirmation,
)
from omp_work.research.strategy_protocol import (
    config_candidate,
    run_candidate,
    run_with_fallback,
)
from omp_work.v1.canonical import canonical_json, sha256

_REPO = Path(__file__).resolve().parents[3]
_ALLOWED = ("open",)
_CAPS = {"cpu_seconds": 120}
_OLD = "11" * 32
_OLD_ARTIFACT = "22" * 32
_NEW = "33" * 32
_NEW_ARTIFACT = "44" * 32


def _steps() -> dict[str, dict[str, object]]:
    return {
        "A1": {
            "parent_id": None,
            "metric": 0.9,
            "cost": 1,
            "children": ["A2"],
            "infra_error": None,
        },
        "A2": {
            "parent_id": "A1",
            "metric": 0.1,
            "cost": 1,
            "children": [],
            "infra_error": None,
        },
        "A3": {
            "parent_id": None,
            "metric": 0.8,
            "cost": 1,
            "children": ["A4"],
            "infra_error": None,
        },
        "A4": {
            "parent_id": "A3",
            "metric": 0.95,
            "cost": 1,
            "children": [],
            "infra_error": None,
        },
    }


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_rejection_sources_are_the_battery() -> None:
    """The four refused sources are broken code, over-cap resources, a missing width default, and verify_receipt."""
    assert BROKEN_SOURCE == "def propose("
    assert OVERSIZED_SOURCE.startswith('RESOURCES = {"cpu_seconds": 1000}\n')
    assert "cpu_seconds" in OVERSIZED_SOURCE and "1000" in OVERSIZED_SOURCE
    assert WRONG_INTERFACE_SOURCE == (
        "def propose(steps, opened, *, seed=0, width):\n    return None, seed\n"
    )
    assert "verify_receipt()" in TAMPER_SOURCE
    assert "return 1 / 0" in CRASH_SOURCE


def test_lab_confirms_generated_mechanism_on_fresh_cohort(tmp_path: Path) -> None:
    """Pattern plateau:greedy; dev comparison total 9.0; fresh cohort confirms with wins 2."""
    run_lab(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == {"lab.json"}
    lab = _load(tmp_path / "lab.json")

    assert lab["pattern"] == "plateau:greedy"
    assert isinstance(lab["hypothesis_id"], str) and len(lab["hypothesis_id"]) == 32
    spec = lab["spec"]
    assert spec["name"] == "adaptive_restart"
    assert spec["interface_version"] == 1
    assert spec["candidate_kind"] == "code"
    source = generation_job(spec, {"min_metric": 0.85})
    assert source == lab["source"]
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == lab["candidate_sha256"]
    assert lab["candidate_state"] == "confirmed"
    assert lab["lifecycle"] == [
        "proposed",
        "inspected",
        "contract_tested",
        "development_compared",
        "confirmed",
    ]
    assert lab["loaded"] == {"status": "ok", "step_id": "A2"}

    comparison = lab["comparison"]
    assert comparison["wins"] == 1
    assert comparison["tasks"] == 1
    dev = comparison["per_task"]["dev-1"]
    assert dev["candidate_metric"] == 0.95
    assert dev["baseline_metric"] == 0.9
    assert dev["candidate_cost"] == 4.0
    assert dev["baseline_cost"] == 2.0
    assert dev["win"] is True
    cost = comparison["cost"]
    assert cost["research"] == 2.0
    assert cost["comparison"] == 1.0
    assert cost["candidate_trials"] == 4.0
    assert cost["baseline_trials"] == 2.0
    assert cost["total"] == 9.0
    assert lab["dev_search"]["candidate"]["opened"] == ["A1", "A2", "A3", "A4"]
    assert lab["dev_search"]["candidate"]["winner_metric"] == 0.95
    assert lab["dev_search"]["baseline"]["opened"] == ["A1", "A2"]
    assert lab["dev_search"]["baseline"]["winner_metric"] == 0.9

    confirmation = lab["confirmation"]
    assert confirmation["status"] == "confirmed"
    assert confirmation["wins"] == 2
    assert confirmation["required_wins"] == 2
    assert confirmation["candidate_sha256"] == lab["candidate_sha256"]
    assert confirmation["cohort_sha256"] == sha256(["conf-1", "conf-2"])
    cohort = confirmation["comparison"]
    assert cohort["tasks"] == 2
    assert set(cohort["per_task"]) == {"conf-1", "conf-2"}
    for task_id in ("conf-1", "conf-2"):
        row = cohort["per_task"][task_id]
        assert row["win"] is True
        assert row["candidate_metric"] == 0.95
        assert row["baseline_metric"] == 0.9
        assert row["candidate_cost"] == 4.0
        assert row["baseline_cost"] == 2.0
    cohort_cost = cohort["cost"]
    assert cohort_cost["research"] == 4.0
    assert cohort_cost["comparison"] == 2.0
    assert cohort_cost["candidate_trials"] == 8.0
    assert cohort_cost["baseline_trials"] == 4.0
    assert cohort_cost["total"] == 18.0


def test_rejections_leave_the_fixture_and_fallback_matches_greedy(
    tmp_path: Path,
) -> None:
    """Each refused run keeps canonical steps and opened bytes; the crash falls back to greedy."""
    run_lab(tmp_path)
    lab = _load(tmp_path / "lab.json")
    rows = lab["refusal_reasons"]
    assert [row["reason"] for row in rows] == [
        "broken_code",
        "resource_exceeded",
        "wrong_interface",
        "evaluator_tampering",
    ]
    assert [row["status"] for row in rows] == ["failed", "rejected", "failed", "failed"]
    assert [row["source"] for row in rows] == [
        BROKEN_SOURCE,
        OVERSIZED_SOURCE,
        WRONG_INTERFACE_SOURCE,
        TAMPER_SOURCE,
    ]

    steps = _steps()
    opened = {"A1"}
    before_steps = canonical_json(steps)
    before_opened = canonical_json(sorted(opened))
    assert rows[0]["steps_before"] == before_steps
    assert rows[0]["opened_before"] == before_opened
    for row in rows:
        assert row["steps_before"] == row["steps_after"] == before_steps
        assert row["opened_before"] == row["opened_after"] == before_opened
        run_candidate(
            row["source"],
            steps,
            opened,
            allowed_actions=_ALLOWED,
            resource_caps=_CAPS,
        )
        assert canonical_json(steps) == before_steps
        assert canonical_json(sorted(opened)) == before_opened

    greedy_pick = search.greedy(steps, opened, seed=0, width=1)
    fallback = run_with_fallback(
        CRASH_SOURCE,
        steps,
        opened,
        fallback_fn=search.greedy,
        seed=0,
        width=1,
        allowed_actions=_ALLOWED,
        resource_caps=_CAPS,
    )
    assert canonical_json(steps) == before_steps
    assert canonical_json(sorted(opened)) == before_opened
    assert fallback["status"] == "fallback"
    assert list(fallback["proposal"]) == list(greedy_pick) == ["A2", 0]
    assert lab["fallback"]["proposal"] == lab["fallback"]["greedy"] == ["A2", 0]
    assert (
        lab["fallback"]["steps_before"]
        == lab["fallback"]["steps_after"]
        == before_steps
    )
    assert (
        lab["fallback"]["opened_before"]
        == lab["fallback"]["opened_after"]
        == before_opened
    )


def test_promotion_proposes_bound_bytes_and_refuses_lab_authority(
    tmp_path: Path,
) -> None:
    """proposed_release binds the packaged bytes; lab cannot activate or accept a core bundle."""
    run_lab(tmp_path)
    lab_bytes = (tmp_path / "lab.json").read_bytes()
    run_promotion(tmp_path)
    assert {path.name for path in tmp_path.iterdir()} == {"lab.json", "promotion.json"}
    assert (tmp_path / "lab.json").read_bytes() == lab_bytes

    lab = _load(tmp_path / "lab.json")
    promo = _load(tmp_path / "promotion.json")
    bundle = promo["bundle"]
    manifest = promo["manifest"]
    source = lab["source"].encode("utf-8")
    config = config_candidate({"min_metric": 0.85, "width": 1}, "greedy")
    assert bundle["files"] == {
        "research/strategies/adaptive_restart.py": sha256(source),
        "research/config/lab.json": sha256(canonical_json(config).encode("utf-8")),
    }
    assert bundle["parent_release"] == "stable-1"
    assert bundle["touches_core_authority"] is False
    assert manifest["status"] == "proposed_release"
    assert manifest["release_id"] == "candidate-1"
    assert (
        manifest["evaluated_bundle_sha256"]
        == manifest["bundle_sha256"]
        == bundle["bundle_sha256"]
    )
    assert manifest["evaluator"] == {
        "component_sha256": _OLD,
        "artifact_sha256": _OLD_ARTIFACT,
    }
    assert manifest["versions"] == {
        "policy": "adaptive-restart.v1",
        "model": "m1",
        "skill": "s1",
    }
    assert manifest["schema_read"] == [1, 2]
    assert manifest["schema_write"] == 2

    gates = promo["gates"]
    assert set(gates) == {"code_review", "audit", "ci", "native_acceptance"}
    assert gates["code_review"]["actor"] == "reviewer"
    assert gates["audit"]["actor"] == "auditor"
    assert gates["ci"]["actor"] == "ci"
    assert gates["native_acceptance"]["actor"] == "owner"
    assert len({gates[name]["evidence_sha256"] for name in gates}) == 4
    assert promo["gate_status"] == {
        "code_review": True,
        "audit": True,
        "ci": True,
        "native_acceptance": True,
        "live_activation": False,
    }
    assert "live_activation" not in manifest["gates"]

    refusals = promo["refusals"]
    assert refusals[0] == {
        "gate": "live_activation",
        "actor": "lab",
        "error": "activation_requires_owner",
    }
    assert refusals[1]["gate"] == "native_acceptance"
    assert refusals[1]["actor"] == "lab"
    assert refusals[1]["error"] == "core_authority_requires_owner"
    core = package_candidate({refusals[1]["core_path"]: b"core\n"})
    assert core["touches_core_authority"] is True
    core_manifest = new_manifest(
        "core-probe",
        core,
        evaluator=manifest["evaluator"],
        versions=manifest["versions"],
        schema_read=(1, 2),
        schema_write=2,
    )
    with pytest.raises(ValueError, match="^core_authority_requires_owner$"):
        record_gate(
            core_manifest,
            core,
            "native_acceptance",
            actor="lab",
            evidence_sha256="evidence",
        )
    with pytest.raises(ValueError, match="^activation_requires_owner$"):
        record_gate(
            manifest,
            bundle,
            "live_activation",
            actor="lab",
            evidence_sha256="evidence",
        )


def test_stable_run_canary_rollback_and_evaluator_change(tmp_path: Path) -> None:
    """Greedy still returns A1, A2; one bad canary rolls back; schema 1 and a mixed manifest are refused."""
    run_lab(tmp_path)
    run_promotion(tmp_path)
    promo = _load(tmp_path / "promotion.json")

    stable = promo["stable_run"]
    assert stable["research_records_present"] is True
    assert stable["budget"] == 3
    assert stable["opened"] == ["A1", "A2"]
    assert stable["winner"] == "A1"
    assert stable["winner_metric"] == 0.9
    assert stable["tree_before"] == stable["tree_after"]

    steps = _steps()
    before = canonical_json(steps)
    opened = search.run_policy("greedy", steps, budget=3)
    after = canonical_json(steps)
    winner = search.winner({step_id: steps[step_id] for step_id in opened})
    assert opened == ["A1", "A2"]
    assert winner == "A1"
    assert steps[winner]["metric"] == 0.9
    assert before == after
    assert (tmp_path / "lab.json").is_file()
    assert (tmp_path / "promotion.json").is_file()

    assert promo["canary_verdict"] == "rollback"
    canary = promo["canary"]
    assert canary["metric"] < canary["baseline_metric"]
    assert (
        canary_verdict(
            [
                CanaryObservation(
                    release_id=canary["release_id"],
                    task_id=canary["task_id"],
                    metric=canary["metric"],
                    baseline_metric=canary["baseline_metric"],
                    direction=canary["direction"],
                )
            ]
        )
        == "rollback"
    )

    plan = promo["rollback_plan"]
    stable_manifest = promo["stable_manifest"]
    assert stable_manifest["release_id"] == "stable-1"
    assert stable_manifest["schema_read"] == [1, 2]
    assert stable_manifest["bundle_sha256"] != promo["manifest"]["bundle_sha256"]
    assert plan["from"] == "candidate-1"
    assert plan["to"] == "stable-1"
    assert plan["restores_bundle"] == stable_manifest["bundle_sha256"]
    assert plan == plan_rollback(promo["manifest"], stable_manifest, (1, 2))
    narrow = dict(stable_manifest)
    narrow["schema_read"] = [1]
    with pytest.raises(ValueError, match="^rollback_incompatible:2$"):
        plan_rollback(promo["manifest"], narrow, (1, 2))
    assert promo["refusals"][2] == {
        "kind": "rollback",
        "error": "rollback_incompatible:2",
    }

    change_record = promo["evaluator_change"]
    assert change_record["reason"] == "recalibrated scorer"
    assert change_record["new_component_sha256"] == _NEW
    assert change_record["new_artifact_sha256"] == _NEW_ARTIFACT
    assert change_record["confirmation_valid"] == ["changed_evaluator"]
    assert change_record["requires_fresh_confirmation"] is True
    change = EvaluatorChange(
        version=change_record["version"],
        old_component_sha256=change_record["old_component_sha256"],
        old_artifact_sha256=change_record["old_artifact_sha256"],
        new_component_sha256=change_record["new_component_sha256"],
        new_artifact_sha256=change_record["new_artifact_sha256"],
        reason=change_record["reason"],
    )
    earlier = {
        "evaluator_component_sha256": _OLD,
        "evaluator_artifact_sha256": _OLD_ARTIFACT,
    }
    assert requires_fresh_confirmation(change, earlier) is True
    moved = dict(promo["manifest"])
    moved["evaluator"] = {
        "component_sha256": _NEW,
        "artifact_sha256": _NEW_ARTIFACT,
    }
    assert confirmation_valid(earlier, moved) == ["changed_evaluator"]

    mixed = dict(promo["manifest"])
    mixed["bundle_sha256"] = promo["mixed_manifest"]["bundle_sha256"]
    mixed["evaluator"] = promo["mixed_manifest"]["evaluator"]
    assert mixed["bundle_sha256"] != promo["manifest"]["bundle_sha256"]
    with pytest.raises(ValueError, match="^mixed_change$"):
        check_promotion(promo["manifest"], mixed)
    assert promo["refusals"][3] == {"kind": "mixed", "error": "mixed_change"}


def test_m3_acceptance_record() -> None:
    """The acceptance file names M3, the proving test, the status, and the s07 wait."""
    text = (_REPO / "docs/programme/M3-ACCEPTANCE.md").read_text(encoding="utf-8")
    assert "source delivered, not installed-qualified" in text
    assert "Waiting for Chris: OMP-317-s07 (child filing and blocking edges)" in text
    assert "python/omp-work/tests/test_research_m3_lab.py" in text
    assert "OMP-317" in text and "OMP-317-s06" in text
    assert "R13" in text and "R14" in text
    for phrase in (
        "LLM-driven exploration",
        "mechanism job queue",
        "microVM strategy isolation",
        "MCTS",
        "declared-interaction mixed releases",
        "/research commands",
    ):
        assert phrase in text
    assert "Do not claim an installed qualification." in text
