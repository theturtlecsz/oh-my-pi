"""Receipt-backed mechanism study: populations, transfer, and L3 admission (OMP-606-s06)."""

from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.jobs.managed_trial import CAPABILITY
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.research.managed import verify_managed_trial
from omp_work.research.mechanism_recipe import THRESHOLD_GRID
from omp_work.research.mechanism_study import (
    BASELINE_FAMILY,
    CANDIDATE_FAMILY,
    GENERATORS,
    CampaignContext,
    MemberRun,
    assignments,
    execute,
    finish,
    freeze_study,
    gate_structural_admission,
    population_average,
    validate_study,
    run_controlled,
)
from omp_work.research import mechanism_report
from omp_work.research.mechanism_report import build_report
from omp_work.jobs.generated_campaign import GeneratedCampaign
from omp_work.research.recursive_transfer_freshness import PreparedTask
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory
from omp_work.research.mechanism_tasks import engineering_task, simulation_task
from omp_work.research.zlib_source_task import build_corpus
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from test_jobs_managed_trial import _project_in
from test_workflow_service import _create, _grant
from test_mission_links_store import _approve, _link, _seed_budget, _submit
from test_recursive_structure_606 import build_request

pytest_plugins = ("test_workflow_service",)
REPO = Path(__file__).resolve().parents[3]
native = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
NULL_CAPS = {
    "node_budget": 1,
    "selection_seconds": 30.0,
    "selection_timeout": 10.0,
    "selection_attempts": 4,
    "width": 1,
}
LOSS_CAPS = {**NULL_CAPS, "node_budget": 2}
_BROKEN = frozenset({
    "replay", "baseline_tamper", "judge_swap", "unequal_caps", "custody_mismatch",
    "duplicate_confirmation_content", "confirmation_mismatch", "protocol_mismatch",
    "missing_managed_trial", "metric_mismatch", "missing_dataset", "confirmation_reused",
})
_BARE_SOURCE = "def propose(steps, opened, *, seed=0, width=1):\n    return None, seed\n"


def _tiny_engineering(seed: int):
    source = f"mechanism-study-606-seed-{seed}\n".encode() * 32
    digest = hashlib.sha256(source).hexdigest()
    return engineering_task(seed, source, {"files": {f"seed-{seed}.bin": digest}})


def _ordered_source(order: tuple[str, ...]) -> str:
    listed = ", ".join(repr(step_id) for step_id in order)
    return (
        "def propose(steps, opened, *, seed=0, width=1):\n"
        f"    order = [{listed}]\n"
        "    for step_id in order:\n"
        "        if step_id in opened:\n"
        "            continue\n"
        "        parent = steps[step_id].get('parent_id')\n"
        "        if parent is not None and parent not in opened:\n"
        "            continue\n"
        "        return step_id, seed\n"
        "    return None, seed\n"
    )


def _steps(run) -> list[str]:
    return [row["step_id"] for row in run.result["decisions"]]


def _poison(run, metric: float):
    run.result["best"] = {"step_id": "forged", "metric": metric}
    run.result["steps"] = [{"id": "forged", "metric": metric}]
    run.result["candidate_score"] = metric
    run.result["summary"] = {"transfer": "win", "delta": metric}
    for trial in run.result.get("trials") or []:
        if isinstance(trial, dict):
            trial["metrics"] = {run.metric: metric, "score": metric, "ratio": metric}
    return run


def _receipt_mean(ctx, run) -> float:
    values = []
    for link in run.result["receipt_links"]:
        with ctx.store.transaction(ctx.workspace_id, ctx.actor_id) as cur:
            receipt, reasons = verify_managed_trial(
                cur, ctx.workspace_id, link["trial_id"], data_dir=ctx.data_dir,
            )
        assert reasons == [], reasons
        assert receipt is not None
        values.append(float(receipt.metrics[run.metric]))
    return sum(values) / len(values)


def _report(fix, runs, **kwargs):
    return build_report(
        runs,
        store=fix.ctx.store,
        workspace_id=fix.ctx.workspace_id,
        actor_id=fix.ctx.actor_id,
        work_id=fix.ctx.work_id,
        data_dir=fix.ctx.data_dir,
        **kwargs,
    )


def _measured(report):
    return {(row["role"], row["threshold"], row["task_id"]): row for row in report.measurements}


def _bare(role: str, **overrides):
    caps = {
        "node_budget": 1.0, "selection_seconds": 30.0, "selection_timeout": 10.0,
        "selection_attempts": 4, "width": 1,
    }
    fields = dict(
        role=role,
        family=BASELINE_FAMILY if role == "baseline" else CANDIDATE_FAMILY,
        threshold=None,
        recipe_sha256="0" * 64,
        source_sha256=hashlib.sha256(_BARE_SOURCE.encode()).hexdigest(),
        spec_sha256="",
        source=_BARE_SOURCE,
        task_id="heldout-empty",
        content_sha256="ab" * 32,
        manifest_sha256="cd" * 32,
        domain="engineering",
        cohort="heldout",
        phase="confirmation",
        seed=0,
        protocol_sha256="12" * 32,
        evaluator_sha256="34" * 32,
        metric="ratio",
        direction="higher",
        caps=caps,
        dataset_id="56" * 32,
        result={
            "best": {"metric": 999},
            "steps": [{"metric": 999}],
            "trials": [{"metrics": {"ratio": 999}}],
            "receipt_links": [],
            "decisions": [],
            "selection_attempts": [],
            "node_attempts": [],
            "spent": 0,
            "candidate_score": 999,
            "summary": {"transfer": "win", "delta": 999},
        },
        failure=None,
    )
    fields.update(overrides)
    return MemberRun(**fields)


@pytest.fixture(scope="module")
def mechanism_runs(service, tmp_path_factory):
    apply_jobs_migrations(service.config)
    workspace_id, actor_id = uuid4(), uuid4()
    _grant(service, workspace_id)
    created = _create(service, workspace_id, "mechanism study 606")
    bearer = service.capabilities / f"mechanism-study-{workspace_id}.json"
    bearer.write_text(json.dumps({
        "token": f"mechanism-study-{workspace_id}", "actor_id": str(actor_id),
        "actor_kind": "automation", "workspaces": [str(workspace_id)],
        "scopes": ["work.read", "work.execute", "work.approve"],
    }))
    bearer.chmod(0o600)
    client = WorkClient(
        "http://testserver", workspace_id, bearer, transport=service.client._transport,
    )
    try:
        item = client.work_item(created["key"])
        _seed_budget(
            service, workspace_id, work_id=str(item.work_id),
            revision_id=str(item.revision.revision_id),
            budget={"usd": "500", "tokens": 50000, "wall_clock_seconds": 50000, "max_subagents": 100},
        )
        project = _project_in(service, workspace_id)
        mission, _mission_body = _submit(
            service, workspace_id, project, kind="research.run",
            requested_capabilities=(CAPABILITY,),
            budget_policy={
                "usd": "1000", "tokens": 100000, "wall_clock_seconds": 100000, "max_subagents": 200,
            },
        )
        _approve(service, workspace_id, mission)
        status, body = _link(service, workspace_id, mission, str(item.work_id))
        assert status == 200, body
        ctx = CampaignContext(
            client=client, store=NativeJobStore(service.config), key=created["key"],
            workspace_id=workspace_id, actor_id=actor_id, work_id=item.work_id,
            harness_dir=tmp_path_factory.mktemp("mechanism-606") / "cached",
            data_dir=service.config.data_dir,
        )
        study = freeze_study(tasks=tuple(_tiny_engineering(seed) for seed in range(5)), caps=NULL_CAPS)
        digest = study.digest
        population = execute(study, ctx)
        assert study.digest == digest
        assert len(population) == 30
        for run in population:
            assert run.failure is None, (run.family, run.threshold, run.seed, run.failure, run.result.get("reason"))
            assert run.result.get("outcome") == "supported", run.result.get("reason")
            assert _steps(run) == ["a"]
            assert len(run.result["receipt_links"]) == 1
        corpus, hashes, tree = build_corpus(REPO, 0)
        engineering = engineering_task(0, corpus, {"files": hashes, "tree_sha256": tree})
        simulation = simulation_task(0, "development")
        recipe = {"min_metric": 0}
        baseline_source = _ordered_source(("a", "b"))
        candidate_source = _ordered_source(("a", "a2"))
        contrast = []
        for task in (simulation, engineering):
            contrast.append(run_controlled(
                task, role="baseline", family=BASELINE_FAMILY, source=baseline_source,
                recipe=recipe, caps=LOSS_CAPS, ctx=ctx,
            ))
            contrast.append(run_controlled(
                task, role="candidate", family=CANDIDATE_FAMILY, source=candidate_source,
                recipe=recipe, caps=LOSS_CAPS, ctx=ctx,
            ))
        for run, expected in zip(contrast, (["a", "b"], ["a", "a2"], ["a", "b"], ["a", "a2"]), strict=True):
            assert run.failure is None, (run.domain, run.role, run.failure, run.result.get("reason"))
            assert _steps(run) == expected, (run.domain, run.role, _steps(run))
        yield SimpleNamespace(ctx=ctx, study=study, population=population, contrast=tuple(contrast), digest=digest)
    finally:
        client.close()


def test_population_average_is_not_the_best_member():
    assert population_average((0.9, 0.0, -0.3)) == pytest.approx(0.2)
    assert population_average((0.9, 0.0, -0.3)) != 0.9
    with pytest.raises(ValueError, match="population_incomplete"):
        population_average((0.9, 0.0))


def test_freeze_preregisters_generators_caps_and_cohorts():
    study = freeze_study(REPO)
    again = freeze_study(REPO)
    assert study.digest == again.digest
    assert study.default_activated is False
    assert [policy.family for policy in study.policies] == [family for family in GENERATORS for _ in THRESHOLD_GRID]
    assert [policy.threshold for policy in study.policies] == [threshold for _ in GENERATORS for threshold in THRESHOLD_GRID]
    families = {policy.family: policy.source for policy in study.policies if policy.threshold == 0.25}
    assert families[BASELINE_FAMILY] != families[CANDIDATE_FAMILY]
    assert len({policy.implementation_sha256 for policy in study.policies}) == 2
    cohorts = {task.cohort for task in study.tasks}
    assert cohorts == {"development", "confirmation", "heldout"}
    assert len(study.tasks) == 10
    assert all(task.real_evidence for task in study.tasks)
    for task in study.tasks:
        if task.domain == "engineering":
            assert task.content_sha256 not in study.spec_json
    assert len(assignments(study)) == 60


def test_missing_receipts_stay_inconclusive_and_keep_the_failed_attempt():
    failed = replace(_bare("baseline"), failure="selection_budget_exhausted")
    report = build_report(
        (failed, _bare("candidate")),
        store=None, workspace_id=uuid4(), actor_id=uuid4(), work_id=uuid4(), data_dir=REPO,
        summary={"transfer": "win", "delta": 999, "outcome": "win"},
    )
    assert report.outcome == "inconclusive"
    assert report.ok is True
    assert report.transfer["inferred"] is False
    assert report.default_activated is False
    assert report.programme_qualification is False
    assert all(row["value"] is None for row in report.measurements)
    assert "missing_receipt" in report.failures
    assert any(row["failure"] == "selection_budget_exhausted" for row in report.costs)
    assert any(row["failure"] == "selection_budget_exhausted" for row in report.l1["selections"])
    assert report.l3["admitted"] is False
    assert report.l3["reason"] == "not_attempted"


@native
def test_contradictory_tree_and_runner_scores_cannot_reverse_receipts(mechanism_runs):
    poisoned = tuple(
        _poison(deepcopy(run), 999 if run.role == "candidate" else 0)
        for run in mechanism_runs.population
    )
    report = _report(
        mechanism_runs, poisoned, study=mechanism_runs.study,
        summary={"transfer": "win", "delta": 999, "outcome": "win"},
    )
    assert report.dossier_reasons == []
    assert _BROKEN.isdisjoint(report.failures), report.failures
    assert report.outcome == "null result"
    assert report.transfer["inferred"] is False
    assert report.transfer["deltas"] == [0, 0, 0, 0, 0]
    assert all(pair["difference"] == 0 for pair in report.pairs)
    assert report.l2["best_member"] is False
    assert report.l2["population_average"] is True
    measured = _measured(report)
    for run in poisoned:
        measurement = measured[(run.role, run.threshold, run.task_id)]
        assert run.result["best"]["metric"] in {0, 999}
        assert measurement["value"] == pytest.approx(_receipt_mean(mechanism_runs.ctx, run))
        assert measurement["value"] != 999


@native
def test_source_win_plus_held_out_loss_stays_loss(mechanism_runs):
    poisoned = []
    for run in mechanism_runs.contrast:
        metric = 0 if run.domain == "simulation" and run.role == "candidate" else 999
        if run.domain == "engineering" and run.role == "baseline":
            metric = 0
        if run.domain == "simulation" and run.role == "baseline":
            metric = 999
        poisoned.append(_poison(deepcopy(run), metric))
    report = _report(
        mechanism_runs, tuple(poisoned),
        summary={"transfer": "win", "source": "win", "delta": 999},
    )
    assert report.dossier_reasons == []
    assert _BROKEN.isdisjoint(report.failures), report.failures
    assert report.source["outcome"] == "win"
    assert report.source["deltas"][0] > 0
    assert report.source["inferred"] is False
    assert report.transfer["outcome"] == "loss"
    assert report.transfer["deltas"][0] < 0
    assert report.transfer["inferred"] is False
    assert report.transfer["stats"] is None
    assert report.outcome == "loss"
    assert report.outcome != report.source["outcome"]
    assert report.programme_qualification is False
    engineering = [group for group in report.groups if group["domain"] == "engineering"]
    assert engineering
    assert any(delta < 0 for group in engineering for delta in group["deltas"])
    assert all(group["keep_both"] for group in engineering)
    assert {tuple(row["steps"]) for row in report.l1["selections"] if row["role"] == "baseline"} == {("a", "b")}
    assert {tuple(row["steps"]) for row in report.l1["selections"] if row["role"] == "candidate"} == {("a", "a2")}
    assert report.owner_options == ("revise", "defer", "retire")
    measured = _measured(report)
    for run in poisoned:
        measurement = measured[(run.role, run.threshold, run.task_id)]
        assert measurement["value"] == pytest.approx(_receipt_mean(mechanism_runs.ctx, run))
        assert measurement["value"] not in {0, 999}


@native
def test_replay_fails(mechanism_runs):
    first = _report(mechanism_runs, mechanism_runs.population, study=mechanism_runs.study)
    assert first.outcome == "null result"
    second = _report(
        mechanism_runs, mechanism_runs.population, study=mechanism_runs.study,
        history=ConsumptionHistory.from_snapshot(json.loads(json.dumps(first.history.snapshot()))),
    )
    assert second.outcome == "failed"
    assert second.ok is False
    assert "replay" in second.failures
    assert second.transfer["inferred"] is False


@native
def test_baseline_tamper_fails(mechanism_runs):
    runs = tuple(deepcopy(run) for run in mechanism_runs.population)
    digest = runs[0].result["decisions"][0]["source_sha256"]
    runs[0].result["decisions"][0]["source_sha256"] = ("e" if digest[0] != "e" else "f") + digest[1:]
    report = _report(mechanism_runs, runs, study=mechanism_runs.study)
    assert report.outcome == "failed"
    assert "baseline_tamper" in report.failures
    assert report.transfer["inferred"] is False


@native
def test_judge_swap_fails(mechanism_runs):
    runs = tuple(deepcopy(run) for run in mechanism_runs.population)
    swapped = replace(runs[0], protocol_sha256="ab" * 32)
    report = _report(mechanism_runs, (swapped, *runs[1:]), study=mechanism_runs.study)
    assert report.outcome == "failed"
    assert "judge_swap" in report.failures
    assert report.transfer["inferred"] is False
    assert len(report.l2["slices"]) == 5
    assert {claim["protocol_sha256"] for claim in report.l2["slices"]} == {
        run.protocol_sha256 for run in mechanism_runs.population
    }


@native
def test_unequal_caps_fail(mechanism_runs):
    runs = tuple(deepcopy(run) for run in mechanism_runs.population)
    widened = replace(runs[3], caps={**runs[3].caps, "node_budget": 9.0})
    report = _report(mechanism_runs, runs[:3] + (widened,) + runs[4:], study=mechanism_runs.study)
    assert report.outcome == "failed"
    assert "unequal_caps" in report.failures
    assert report.transfer["inferred"] is False


@native
def test_incomplete_pairs_prevent_inference(mechanism_runs):
    kept = mechanism_runs.population[:5] + mechanism_runs.population[6:]
    report = _report(mechanism_runs, kept, study=mechanism_runs.study)
    assert report.outcome == "incomplete comparison"
    assert report.transfer["inferred"] is False
    assert report.transfer["stats"] is None
    assert None in report.transfer["deltas"]
    assert report.programme_qualification is False
    assert report.l2["best_member"] is False


@native
def test_null_study_completes_without_structural_admission(mechanism_runs, native_jobs):
    _execution, state, proposal, ctx, request = build_request(native_jobs, score=0.4)
    studies = state.studies
    history = state.proposal_history

    def gate(proof, *, bound=state):
        return gate_structural_admission(
            bound, proposal, proof, grant=ctx.grant, policy=ctx.policy, authority=ctx.authority,
            action=ctx.action, repositories=ctx.repositories, now=ctx.now(), study=mechanism_runs.study,
        )

    for proof in (True, {"status": "compared", "deltas": [999], "keep_both": False}):
        refused = gate(proof)
        assert refused.admitted is False
        assert refused.reason == "confirmation_required"
        assert refused.default_activated is False
        assert refused.state.studies == studies
        assert refused.state.proposal_history == history
    engineering = replace(request, cohorts=tuple(
        cohort.model_copy(update={"domain": "engineering"}) for cohort in request.cohorts
    ))
    held_out = gate(engineering)
    assert held_out.admitted is False
    assert held_out.reason == "engineering_cannot_admit"
    assert held_out.state.studies == studies
    assert held_out.default_activated is False

    admission = gate(request)
    assert admission.admitted is False
    assert admission.reason == "confirmation_failed"
    assert admission.structural is True
    assert admission.default_activated is False
    assert admission.state.studies == studies
    assert admission.state.proposal_history == history
    assert mechanism_runs.study.digest == mechanism_runs.digest
    execution = finish(mechanism_runs.study, mechanism_runs.population, admission)
    assert execution.completed is True
    assert execution.default_activated is False
    report = build_report(
        execution,
        store=mechanism_runs.ctx.store,
        workspace_id=mechanism_runs.ctx.workspace_id,
        actor_id=mechanism_runs.ctx.actor_id,
        work_id=mechanism_runs.ctx.work_id,
        data_dir=mechanism_runs.ctx.data_dir,
        summary={"transfer": "win", "delta": 999},
    )
    assert report.completed is True
    assert report.outcome == "null result"
    assert report.ok is True
    assert report.default_activated is False
    assert report.programme_qualification is False
    assert report.goal_retained is True
    assert report.dossier_reasons == []
    assert _BROKEN.isdisjoint(report.failures), report.failures
    assert report.transfer["deltas"] == [0, 0, 0, 0, 0]
    assert report.transfer["inferred"] is False
    assert report.transfer["stats"] is None
    assert len(report.l2["slices"]) == 5
    assert len({claim["protocol_sha256"] for claim in report.l2["slices"]}) == 5
    assert report.source["outcome"] == "inconclusive"
    assert report.l3["admitted"] is False
    assert report.l3["reason"] == "confirmation_failed"
    assert report.l3["structural"] is True
    assert report.l3["production_rule_change"] == f"{BASELINE_FAMILY} -> {CANDIDATE_FAMILY}"
    assert report.l3["cohort_proof"]["admitted"] is False
    assert report.l2["population_average"] is True
    assert report.l2["best_member"] is False
    assert {tuple(row["steps"]) for row in report.l1["selections"]} == {("a",)}
    assert report.owner_options == ("revise", "defer", "retire")
    assert report.next_trial["seeds"] == 5
    assert report.next_trial["node_reservations"] == 30
    assert "programme qualification" in report.next_trial["note"]
    assert {row["role"] for row in report.costs} == {"baseline", "candidate"}
    assert any(delta == 0 for group in report.groups for delta in group["deltas"])
    assert "engineering paired difference 0" in " ".join(
        # Zeros stay in the verified dossier negatives via the transfer deltas.
        [f"engineering paired difference {delta}" for delta in report.transfer["deltas"]]
    )


def test_nested_preregistration_edits_cannot_change_execution_caps():
    study = freeze_study(tasks=(_tiny_engineering(0),))
    study.caps["node_budget"] = 123
    study.cohort_sha256["heldout"] = "0" * 64
    assert study.caps["node_budget"] == 5
    validate_study(study)
    with pytest.raises(ValueError, match="study_changed"):
        validate_study(replace(study, caps_json=json.dumps({**study.caps, "node_budget": 123})))
    with pytest.raises(ValueError, match="study_changed"):
        build_report(runs=[], study=replace(study, digest="0" * 64), store=None,
                     workspace_id=uuid4(), actor_id=uuid4(), work_id=uuid4(), data_dir=REPO)


@native
def test_registered_width_reaches_jailed_policy(mechanism_runs, tmp_path):
    task = _tiny_engineering(7)
    source = (
        "def propose(steps, opened, *, seed=0, width=1):\n"
        "    return ('b' if width == 3 else 'a'), seed\n"
    )
    run = run_controlled(
        task, role="candidate", family=CANDIDATE_FAMILY, source=source,
        recipe={"min_metric": 0.5}, caps={**NULL_CAPS, "width": 3}, ctx=mechanism_runs.ctx,
    )
    assert run.failure is None
    assert _steps(run) == ["b"]
    with pytest.raises(ValueError, match="width"):
        GeneratedCampaign(task, {"min_metric": 0.5}, tmp_path, width=0)


@native
def test_omitted_frozen_task_remains_in_denominator(mechanism_runs):
    study = freeze_study(tasks=(*mechanism_runs.study.tasks, _tiny_engineering(5)), caps=NULL_CAPS)
    report = _report(mechanism_runs, mechanism_runs.population, study=study)
    assert report.transfer["inferred"] is False
    assert report.transfer["stats"] is None
    assert report.outcome == "incomplete comparison"
    assert len(report.pairs) == 18
    missing = [pair for pair in report.pairs if pair["seed"] == 5]
    assert len(missing) == 3
    assert all(pair["difference"] is None and "missing_member" in pair["failures"] for pair in missing)
    assert len(report.transfer["deltas"]) == 6
    assert report.transfer["deltas"].count(None) == 1


@native
@pytest.mark.parametrize("reason", ["missing_managed_trial", "confirmation_mismatch", "metric_mismatch"])
def test_refused_cohorts_invalidate_all_downstream_metrics(mechanism_runs, monkeypatch, reason):
    def refuse(cohorts, native, studies):
        return tuple(PreparedTask(False, (reason,), cohort.dataset_id, None, None, ()) for cohort in cohorts)
    monkeypatch.setattr(mechanism_report, "prepare_cohorts", refuse)
    report = _report(mechanism_runs, mechanism_runs.population, study=mechanism_runs.study)
    assert reason in report.failures
    assert report.transfer["inferred"] is False
    assert all(delta is None for delta in report.transfer["deltas"])
    assert all(pair["difference"] is None for pair in report.pairs)
    assert all(row["value"] is None for row in report.measurements)
    assert all(not group["deltas"] for group in report.groups)
    assert all(reason in group["failures"] for group in report.groups)


def test_five_complete_matched_pairs_support_only_local_inference():
    claim = mechanism_report._slice_claim([
        {"seed": seed, "difference": delta} for seed, delta in enumerate((1, 2, 3, 4, 5))
    ])
    assert claim["inferred"] is True
    assert claim["stats"]["mean"] == 3
    assert claim["stats"]["confidence_interval_95"] == pytest.approx([1.03675684, 4.96324316])
    assert claim["programme_qualification"] is False
    missing = mechanism_report._slice_claim([*claim["seeds"], {"seed": 5, "difference": None}])
    assert missing["stats"] is None
    assert missing["inferred"] is False
    assert missing["deltas"] == [1, 2, 3, 4, 5, None]
