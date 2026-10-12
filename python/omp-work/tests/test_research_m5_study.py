"""M5 fixture source proof only; native custody does not imply real adoption."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import timedelta
from uuid import uuid4

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.jobs.usage import record_usage
from omp_work.research.recursive_lab import load_packet, run_study
from omp_work.v1.canonical import canonical_json
from recursive_transfer_execution_support import build_execution, seal_step
from recursive_transfer_support import register_task_dataset
from test_research_recursive_method import TRACES

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
                                reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
pytest_plugins = ("test_workflow_service",)


def study_input(native_jobs, tmp_path, *, score=0.8, failed=False, drift=False):
    execution = build_execution(native_jobs)
    cohorts = []
    for phase, domain in (("development", "machine_learning"),
                          ("confirmation", "machine_learning"), ("confirmation", "engineering")):
        execution.arms.clear()
        # Distinct custody payloads, not renamed copies of one holdout.
        execution.steps["a"]["domain_fixture"] = domain
        execution.steps["a"]["phase_fixture"] = phase
        if domain == "engineering":
            execution.steps["b"]["children"] = ["b1"]
            execution.steps["b1"] = {"parent_id": "b", "children": [], "metric": 0.5, "cost": 1}
        for step in execution.steps:
            seal_step(execution, step, baseline_score=0.6 if step == "a" else 0.7,
                      candidate_score=0.6 if step == "a" else score,
                      candidate_source=b"# changed downstream source\n" if failed and step == "a1" else None)
        cohort = execution.cohort().model_copy(update={"domain": domain, "phase": phase})
        cohorts.append(cohort.model_dump(mode="json"))
    study = execution.candidate.study
    generated = execution.candidate.result
    pin = study.upstream_assets[0]
    start, end = study.window_start, study.window_end
    controls = {}
    arms = {}
    for name in ("baseline", "candidate"):
        context = getattr(execution, f"{name}_context")
        frozen = getattr(execution, name)
        controls[name] = {
            "grant": context.grant.model_dump(mode="json"),
            "state": context.state.model_dump(mode="json"), "policy": asdict(context.policy),
            "authority": asdict(context.authority), "action": asdict(context.action),
            "repositories": [asdict(r) for r in context.repositories.values()],
            "operation_id": str(uuid4()), "request_id": str(uuid4()),
        }
        arms[name] = {"study": frozen.study.model_dump(mode="json"), "development_traces": {"dev": TRACES}}
    release = {
        "release_id": generated.artifact_sha256, "bundle_sha256": generated.artifact_sha256,
        "method_sha256": study.method_sha256, "evaluator_component_sha256": study.evaluator_component_sha256,
        "evaluator_artifact_sha256": study.evaluator_artifact_sha256, "schema_read": [1],
    }
    stable = {**release, "release_id": "named-stable-method", "bundle_sha256": execution.baseline.result.artifact_sha256,
              "method_sha256": execution.baseline.result.source_sha256}
    raw = {
        "program_id": "M5-source-proof", "run_id": str(uuid4()), **arms,
        "controller": {"config": asdict(native_jobs.service.config),
                       "workspace_id": native_jobs.workspace_id, "actor_id": native_jobs.actor_id,
                       "work_id": native_jobs.item["work_id"], **controls,
                       "execution_operation_id": str(uuid4()), "execution_request_id": str(uuid4())},
        "cohorts": cohorts, "quality_bound": {"minimum_delta": 0.01, "minimum_independent_tasks": 2},
        "ownership_costs": {category: {"usd": None, "compute_seconds": None, "tokens": None, "owner_minutes": None}
                            for category in ("research", "evaluation", "maintenance", "interventions")},
        "release": release,
        "maintenance_policy": {
            "frozen_at": start - timedelta(seconds=1), "window_start": start, "window_end": end,
            "authorization_decision_id": execution.candidate_context.grant.decision_id,
            "drift_tolerance": 0.01 if drift else 1, "max_regression": 0.1,
            "cost_bounds": {"usd": "0", "compute_seconds": "0", "tokens": 0, "owner_minutes": "0"},
            "reference_primary": 0.8, "direction": "higher", "tested_artifact_sha256": generated.artifact_sha256,
            "rollback_target": stable, "persisted_schema_versions": [1],
            "base_commit": "6" * 40, "candidate_commit": "7" * 40,
            "consumed_paths": [{"identity": pin.identity, "asset_id": pin.identity,
                                "path": "research/generator.py", "content_sha256": pin.content_sha256}],
        },
        "upstream_check": {"baseCommit": "6" * 40, "candidateCommit": "7" * 40,
                           "files": [{"assetId": pin.identity, "path": "research/generator.py", "status": "unchanged",
                                      "fromSha256": pin.content_sha256, "toSha256": pin.content_sha256}],
                           "newFiles": [], "overlayConflicts": []},
    }
    input_path = tmp_path / f"input-{uuid4()}.json"
    input_path.write_text(canonical_json(raw))
    return input_path, raw, execution


def test_two_domains_execute_changed_recipe_with_native_evidence(native_jobs, tmp_path):
    path, _, execution = study_input(native_jobs, tmp_path)
    output = tmp_path / "packet"
    result = run_study(path, output, execution.candidate.result.source_sha256)
    packet = load_packet(output, result["packet_sha256"])
    evidence = packet["decision"]
    assert result["used_method_sha256"] == execution.candidate.result.source_sha256
    assert evidence["evidence_class"] == "fixture_source_proof_only"
    assert not result["eligible"]  # Unmeasured ownership/compute cannot claim adoption.
    assert {row["domain"] for row in evidence["transfer"]} == {"machine_learning", "engineering"}
    for row in evidence["transfer"]:
        assert row["selections"] == {"baseline": "b", "candidate": "a1"}, row["failures"]
        assert row["delta"] == pytest.approx(0.1)
        assert row["denominator"] == (8 if row["domain"] == "engineering" else 6)
        assert row["candidate"]["selected_bound_step"]["evidence"]["receipt"]["verdict"] == "qualified"
        assert row["candidate"]["state"]["usage"]
    assert evidence["comparison"]["transfer_domains"] == ["engineering"]
    assert evidence["costs"]["series"][0]["cumulative"]["complete"] is False
    assert evidence["maintenance"]["reasons"] == ["unknown_benefit"]
    assert evidence["methods"]["baseline"]["source_sha256"] != evidence["methods"]["candidate"]["source_sha256"]
    assert not evidence["installed_qualified"]
    # One generation plus three proposals per arm: no task shares a usage ID.
    proposal_requests = {row["request_id"] for row in evidence["native_usage"]
                         if json.loads((output / "sources" / "input.json").read_text())["controller"]["execution_request_id"] in row["request_id"]}
    assert len(proposal_requests) == 6
    assert len(evidence["methods"]["candidate"]["state"]["usage"]) == 5


def test_failed_downstream_attempt_retains_denominator_and_both_methods(native_jobs, tmp_path):
    path, _, _ = study_input(native_jobs, tmp_path, failed=True)
    result = run_study(path, tmp_path / "packet")
    evidence = load_packet(tmp_path / "packet", result["packet_sha256"])["decision"]
    assert not result["eligible"]
    for row in evidence["transfer"]:
        assert row["denominator"] == (8 if row["domain"] == "engineering" else 6)
        assert row["failures"] and row["delta"] is None
    assert set(evidence["methods"]) == {"baseline", "candidate"}
    assert evidence["history"]["failures"]


def test_transfer_regression_proposes_named_retirement_preserving_evidence(native_jobs, tmp_path):
    path, _, execution = study_input(native_jobs, tmp_path, score=0.2)
    result = run_study(path, tmp_path / "packet", execution.baseline.result.source_sha256)
    evidence = load_packet(tmp_path / "packet", result["packet_sha256"])["decision"]
    assert not result["eligible"] and evidence["chosen_arm"] == "baseline"
    assert evidence["maintenance"]["proposal"] == "retire"
    assert evidence["maintenance"]["rollback"]["to"] == "named-stable-method"
    assert evidence["methods"]["candidate"]["source_sha256"] == execution.candidate.result.source_sha256
    assert all(row["delta"] < 0 for row in evidence["transfer"])


def test_drift_without_new_authorization_emits_pending_question(native_jobs, tmp_path):
    path, _, _ = study_input(native_jobs, tmp_path, score=0.9, drift=True)
    result = run_study(path, tmp_path / "packet")
    packet = load_packet(tmp_path / "packet", result["packet_sha256"])
    assert packet["decision"]["maintenance"]["proposal"] == "reevaluate"
    assert packet["questions"][0]["question_id"] == packet["decision"]["maintenance"]["question"]["question_id"]
    assert "reevaluation" not in result


@pytest.mark.parametrize("target", ("dossier.json", "report.md", "sources/candidate.py", "program.json"))
def test_tampered_packet_rejected(native_jobs, tmp_path, target):
    path, _, _ = study_input(native_jobs, tmp_path)
    output = tmp_path / "packet"
    result = run_study(path, output)
    (output / target).write_text("tampered")
    with pytest.raises((ValueError, json.JSONDecodeError)):
        load_packet(output, result["packet_sha256"])


def test_drift_reevaluation_requires_separate_decision_window(native_jobs, tmp_path):
    path, raw, _ = study_input(native_jobs, tmp_path, score=0.9, drift=True)
    newer_path, _, _ = study_input(native_jobs, tmp_path)
    raw["reevaluation_input_path"] = str(newer_path)
    path.write_text(canonical_json(raw))
    result = run_study(path, tmp_path / "packet")
    assert result["reevaluation_pending"] == "separate_reevaluation_authorization_required"
    assert load_packet(tmp_path / "packet", result["packet_sha256"])["questions"]
    assert not (tmp_path / "packet" / "reevaluation").exists()


def test_separately_authorized_drift_runs_same_pipeline_and_retains_prior_packet(native_jobs, tmp_path):
    path, raw, _ = study_input(native_jobs, tmp_path, score=0.9, drift=True)
    newer_path, newer, _ = study_input(native_jobs, tmp_path, score=0.8)
    # Fixture owner authorization only: not a real decision or adoption receipt.
    for arm in ("baseline", "candidate"):
        control = newer["controller"][arm]
        control["grant"]["decision_id"] = "separate-fixture-window"
        control["state"]["grant"]["decision_id"] = "separate-fixture-window"
        control["policy"]["decision_id"] = "separate-fixture-window"
        control["authority"]["decision_id"] = "separate-fixture-window"
    newer["maintenance_policy"]["authorization_decision_id"] = "separate-fixture-window"
    # Fresh content, not another task ID attached to previously consumed bytes.
    for cohort in newer["cohorts"]:
        domain = cohort["domain"]
        payload = {"steps": {
            "a": {"parent_id": None, "children": ["a1"], "cost": 2, "metric": 0.6},
            "a1": {"parent_id": "a", "children": [], "cost": 2, "metric": 0.8},
            "b": {"parent_id": None, "children": [], "cost": 2, "metric": 0.7, "fixture": domain, "phase": cohort["phase"]},
        }, "opened": ["a"]}
        if domain == "engineering":
            payload["steps"]["b"]["children"] = ["b1"]
            payload["steps"]["b1"] = {"parent_id": "b", "children": [], "metric": 0.5, "cost": 2}
        cohort["dataset_id"] = register_task_dataset(native_jobs, uuid4().hex, payload)
    newer_path.write_text(canonical_json(newer))
    raw["reevaluation_input_path"] = str(newer_path)
    path.write_text(canonical_json(raw))
    output = tmp_path / "packet"
    result = run_study(path, output)
    packet = load_packet(output, result["packet_sha256"])
    child = load_packet(output / "reevaluation", result["reevaluation"]["packet_sha256"])
    assert packet["decision"]["maintenance"]["proposal"] == "reevaluate"
    assert child["decision"]["maintenance"]["proposal"] == "retain"
    assert len(child["decision"]["history"]["successes"]) > len(packet["decision"]["history"]["successes"])
    assert all(row["selections"]["candidate"] == "a1" for row in child["decision"]["transfer"])
    assert not result["reevaluation"]["eligible"]


def test_unknown_native_tokens_remain_unknown_in_cost_projection(native_jobs, tmp_path):
    path, _, execution = study_input(native_jobs, tmp_path)
    record_usage(
        execution.native.store, operation_id=str(uuid4()), workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id, work_id=execution.native.work_id,
        job_id=execution.arms["a1"][1].job_id, request_id=str(uuid4()), role="worker",
        model="fixture-unmeasured", measurement="unknown", input_tokens=None,
        output_tokens=None, cache_tokens=None, price_usd=None,
    )
    result = run_study(path, tmp_path / "packet")
    evidence = load_packet(tmp_path / "packet", result["packet_sha256"])["decision"]
    unknown = [row for row in evidence["native_usage"] if row["measurement"] == "unknown"]
    assert unknown and unknown[0]["input_tokens"] is None
    assert evidence["costs"]["series"][0]["cumulative"]["axes"]["tokens"]["candidate_operating"] is None
    assert not result["eligible"]
