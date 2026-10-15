"""Generated campaigns select jailed strategies and execute real managed nodes."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from uuid import uuid4

import pytest

from omp_work.jobs.campaign_decisions import Decision, GeneratedDecision, decode, encode
from omp_work.jobs.generated_campaign import GeneratedCampaign
from omp_work.jobs.managed_trial import CAPABILITY, ManagedTrialHandler, ManagedTrialRefused
from omp_work.jobs.research_campaign import run_research_campaign
from omp_work.jobs.worker import JobWorker
from omp_work.research import mechanism, strategy_execution
from omp_work.research.custody import artifact_path
from omp_work.research.mechanism_tasks import simulation_task
from omp_work.research.receipt import managed_job_id
from omp_work.v1.canonical import sha256
from test_adaptive_campaign_limits import _issue_stop
from test_jobs_managed_trial import _project_in
from test_mission_links_store import _approve, _link, _seed_budget, _submit
from test_research_campaign_runner import campaign, _definition, _run  # noqa: F401

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


@pytest.fixture
def generated(service, campaign):
    item = campaign.client.work_item(campaign.key)
    _seed_budget(service, campaign.workspace_id, work_id=str(item.work_id),
                 revision_id=str(item.revision.revision_id), budget={
                     "usd": "6", "tokens": 600, "wall_clock_seconds": 120, "max_subagents": 2,
                 })
    project = _project_in(service, campaign.workspace_id)
    mission, _ = _submit(service, campaign.workspace_id, project,
                         kind="research.run", requested_capabilities=(CAPABILITY,))
    _approve(service, campaign.workspace_id, mission)
    status, body = _link(service, campaign.workspace_id, mission, str(item.work_id))
    assert status == 200, body
    return GeneratedCampaign(
        simulation_task(0, "development"), {"family": mechanism.ADAPTIVE_RESTART, "min_metric": 0},
        service.config.data_dir, node_budget=3,
    )


def run(fix, adapter, **kwargs):
    return run_research_campaign(
        fix.client, fix.store, fix.key, adapter.definition,
        harness_dir=fix.harness_dir, actor_id=fix.actor_id, generated=adapter, **kwargs,
    )


def test_real_nodes_replay_decisions_and_custody(campaign, generated):
    result = run(campaign, generated)
    assert result["outcome"] == "supported"
    assert [d["step_id"] for d in result["decisions"]] == ["a", "a1", "a1x"]
    assert result["spent"] == 3
    assert result["best"]["metric"] == max(t["metrics"]["score"] for t in result["trials"])
    view = campaign.client.research(campaign.key)
    persisted = view.campaigns[0]
    assert persisted.domain.value == generated.task.domain
    commitment_sha = persisted.spec.evaluation_protocol_id.removeprefix("generated:")
    commitment_bytes = artifact_path(generated.data_dir, campaign.workspace_id, commitment_sha).read_bytes()
    assert hashlib.sha256(commitment_bytes).hexdigest() == commitment_sha
    commitment = json.loads(commitment_bytes)
    assert commitment["source"] == hashlib.sha256(generated.source).hexdigest()
    assert commitment["recipe"] == sha256(generated.recipe)
    assert commitment["task"] == generated.task.manifest_sha256
    assert commitment["protocol"] == hashlib.sha256(generated.task.protocol_bytes).hexdigest()
    assert commitment["definition"]["budget"] == generated.node_budget
    assert commitment["selection_seconds"] == generated.selection_seconds
    trials = {str(t.trial_id): t for t in view.trials}
    links = result["receipt_links"]
    assert len(links) == 3
    assert len(result["node_attempts"]) == 3
    assert all(a["duration"] > 0 and a["status"] == "settled" for a in result["node_attempts"])
    for index, (summary, link) in enumerate(zip(result["trials"], links, strict=True)):
        trial = trials[summary["trial_id"]]
        decision = decode(trial.reason)
        assert isinstance(decision, GeneratedDecision)
        assert json.loads(encode(decision)) == result["decisions"][index]
        assert decision.observations_sha256 == sha256(links[:index])
        assert decision.source_sha256 == hashlib.sha256(generated.source).hexdigest()
        assert decision.recipe_sha256 == sha256(generated.recipe)
        assert decision.policy == "generated"
        attempt = result["selection_attempts"][index]
        assert attempt["failure"] is None
        assert attempt["duration"] > 0
        assert attempt["output_bytes"] > 0
        assert attempt["observations_sha256"] == decision.observations_sha256
        with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
            job = campaign.store.job_view(cur, campaign.workspace_id, managed_job_id(trial.trial_id))
        assert job["status"] == "sealed"
        evidence = job["settlement"]["receipts"][0]["evidence"]
        assert evidence["content_digest"] == link["receipt_sha256"]
        receipt = artifact_path(generated.data_dir, campaign.workspace_id, link["receipt_sha256"]).read_bytes()
        assert json.loads(receipt)["metrics"] == summary["metrics"]
    # Reproducible on the same frozen task, using fresh independent jobs/receipts.
    repeated = run(campaign, generated)
    assert [d["step_id"] for d in repeated["decisions"]] == [d["step_id"] for d in result["decisions"]]
    assert repeated["best"] == result["best"]
    assert {l["job_id"] for l in repeated["receipt_links"]}.isdisjoint(l["job_id"] for l in links)


def test_generated_families_select_distinct_paths(campaign, generated):
    adaptive = run(campaign, generated)
    root_first = replace(generated, recipe={"family": mechanism.ROOT_FIRST, "min_metric": 0}, source=None)
    roots = run(campaign, root_first)
    assert [d["step_id"] for d in roots["decisions"]] == ["a", "b", "c"]
    assert adaptive["decisions"][1]["step_id"] != roots["decisions"][1]["step_id"]
    assert adaptive["outcome"] == roots["outcome"] == "supported"
    assert generated.components.commitment != root_first.components.commitment


@pytest.mark.parametrize("limit", ["node_budget", "selection_seconds", "selection_attempts"])
def test_exhausted_caps_never_invoke_or_credit(campaign, generated, monkeypatch, limit):
    def forbidden(*args, **kwargs):
        pytest.fail("invoked despite exhausted cap")
    monkeypatch.setattr(strategy_execution, "execute_strategy", forbidden)
    monkeypatch.setattr(JobWorker, "tick", forbidden)
    result = run(campaign, replace(generated, **{limit: 0}))
    assert result["outcome"] == "refuted"
    assert result["best"] is None
    assert result["trials"] == result["selection_attempts"] == []


def test_parent_violation_records_failed_attempt_without_fallback(campaign, generated):
    source = b'def propose(steps, opened, *, seed=0, width=1):\n    return "a1x", seed\n'
    result = run(campaign, replace(generated, source=source))
    assert result["outcome"] == "refuted"
    assert result["trials"] == []
    assert result["best"] is None
    attempt, = result["selection_attempts"]
    assert attempt["failure"] == "invalid_selection"
    assert "parent_not_open" in attempt["reasons"]
    assert attempt["duration"] > 0
    with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
        cur.execute("SELECT artifact_sha256 FROM omp_research.artifacts WHERE workspace_id=%s AND manifest->>'source_ref'=%s",
                    (campaign.workspace_id, f"generated-selection:{result['campaign_id']}:0"))
        artifact = cur.fetchone()
    assert artifact is not None
    persisted = artifact_path(generated.data_dir, campaign.workspace_id, artifact["artifact_sha256"]).read_bytes()
    assert json.loads(persisted) == attempt


def test_reported_scores_cannot_override_receipt(campaign, generated, monkeypatch):
    original = ManagedTrialHandler.run
    def forged_metrics(self, worker, job):
        settlement = original(self, worker, job)
        settlement.receipts[0]["evidence"]["metrics"] = {"score": 999999}
        return settlement
    monkeypatch.setattr(ManagedTrialHandler, "run", forged_metrics)
    result = run(campaign, generated)
    assert result["outcome"] == "supported"
    assert 0 <= result["best"]["metric"] <= 1
    assert all(t["metrics"]["score"] <= 1 for t in result["trials"])


def test_tampered_observation_scores_refuse_conclusion(campaign, generated, monkeypatch):
    original = ManagedTrialHandler._record_observation
    def forged_observation(self, **kwargs):
        kwargs["payload"] = {**kwargs["payload"], "metrics": {"score": 999999}}
        kwargs["payload_sha256"] = sha256(kwargs["payload"])
        return original(self, **kwargs)
    monkeypatch.setattr(ManagedTrialHandler, "_record_observation", forged_observation)
    with pytest.raises(RuntimeError, match="contradictory_evidence"):
        run(campaign, generated)
    assert campaign.client.research(campaign.key).campaigns[0].outcome is None


def test_unsealed_receipt_never_concludes(campaign, generated, monkeypatch):
    original = JobWorker.tick
    def not_settled(self, job_id=None):
        original(self, job_id=job_id)
        with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
            cur.execute("UPDATE omp_jobs.jobs SET status='in_flight' WHERE workspace_id=%s AND job_id=%s",
                        (campaign.workspace_id, job_id))
        return job_id
    monkeypatch.setattr(JobWorker, "tick", not_settled)
    with pytest.raises(RuntimeError, match="missing_managed_receipt"):
        run(campaign, generated)
    assert campaign.client.research(campaign.key).campaigns[0].outcome is None


def test_conclusion_rereads_custody(campaign, generated, monkeypatch):
    original = campaign.client.research
    reads = 0
    def corrupt_before_conclusion(key):
        nonlocal reads
        view = original(key)
        if len(view.trials) == 1 and len(view.observations) == 1:
            reads += 1
            # Persisted trial and selection gate read first; conclusion reads third.
            if reads == 3:
                digest = view.observations[0].payload["receipt_sha256"]
                path = artifact_path(generated.data_dir, campaign.workspace_id, digest)
                path.chmod(0o600)
                path.write_bytes(b"tampered")
        return view
    monkeypatch.setattr(campaign.client, "research", corrupt_before_conclusion)
    with pytest.raises(RuntimeError, match="missing_managed_receipt"):
        run(campaign, replace(generated, node_budget=1))
    assert original(campaign.key).campaigns[0].outcome is None


@pytest.mark.parametrize("boundary", ["proposal", "selection", "evaluating"])
def test_live_cancellation_never_dispatches_or_concludes(campaign, generated, monkeypatch, boundary):
    original = campaign.client.execute
    stopped = False
    after = []
    def cancel_at_boundary(envelope):
        nonlocal stopped
        if stopped:
            after.append(envelope.command.type)
        response = original(envelope)
        command = envelope.command
        should_stop = (
            boundary == "proposal" and command.type == "propose_research_trial"
            or boundary == "selection" and command.type == "register_research_artifact"
            and command.payload.manifest.source_ref.startswith("generated-selection:")
            or boundary == "evaluating" and command.type == "set_research_campaign_state"
            and command.payload.target_state == "evaluating"
        )
        if should_stop:
            camp = campaign.client.research(campaign.key).campaigns[0]
            _issue_stop(campaign, camp, "cancelled")
            stopped = True
        return response
    monkeypatch.setattr(campaign.client, "execute", cancel_at_boundary)
    result = run(campaign, generated)
    assert stopped and not after
    assert result["state"] == "cancelled"
    assert result["outcome"] is None
    view = campaign.client.research(campaign.key)
    assert view.campaigns[0].outcome is None
    if boundary in ("selection", "proposal"):
        assert not view.observations
        assert result["spent"] == 0


def test_resume_refuses_before_registration(campaign, generated, monkeypatch):
    calls = []
    monkeypatch.setattr(campaign.client, "execute", lambda envelope: calls.append(envelope))
    with pytest.raises(RuntimeError, match="resume is unsupported"):
        run(campaign, generated, resume_campaign_id=uuid4())
    assert calls == []
    assert not campaign.harness_dir.exists()


def test_missing_mission_cannot_dispatch(campaign, service):
    adapter = GeneratedCampaign(simulation_task(0, "development"), {"min_metric": 0}, service.config.data_dir, node_budget=1)
    with pytest.raises(ManagedTrialRefused):
        run(campaign, adapter)
    view = campaign.client.research(campaign.key)
    assert view.campaigns[0].outcome is None
    assert not view.observations
    with campaign.store.transaction(campaign.workspace_id, campaign.actor_id) as cur:
        assert campaign.store.job_view(cur, campaign.workspace_id, managed_job_id(view.trials[0].trial_id)) is None


def test_v1_campaigns_remain_default(campaign):
    result = _run(campaign, _definition(campaign, (1,)))
    assert result["outcome"] == "supported"
    assert "selection_attempts" not in result
    trial = campaign.client.research(campaign.key).trials[0]
    assert isinstance(decode(trial.reason), Decision)
    assert decode(trial.reason).policy == "fixed"


def test_cumulative_selection_cap_stops_after_real_attempt(campaign, generated):
    result = run(campaign, replace(generated, selection_attempts=1))
    assert result["outcome"] == "refuted"
    assert len(result["trials"]) == len(result["selection_attempts"]) == 1
    assert result["spent"] == 1
    assert "selection_budget_exhausted" in result["reason"]


def test_generated_resume_without_adapter_also_refuses_before_mutation(campaign, generated, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(JobWorker, "tick", lambda self, job_id=None: None)
        with pytest.raises(RuntimeError, match="unsettled"):
            run(campaign, generated)
    campaign_id = campaign.client.research(campaign.key).campaigns[0].campaign_id
    calls = []
    monkeypatch.setattr(campaign.client, "execute", lambda envelope: calls.append(envelope))
    with pytest.raises(RuntimeError, match="resume is unsupported"):
        run(campaign, generated)
    with pytest.raises(RuntimeError, match="resume is unsupported"):
        run_research_campaign(
            campaign.client, campaign.store, campaign.key, generated.definition,
            harness_dir=campaign.harness_dir, actor_id=campaign.actor_id,
            resume_campaign_id=campaign_id,
        )
    assert calls == []


def test_cancel_during_native_execution_keeps_failed_attempt(campaign, generated, monkeypatch):
    original = ManagedTrialHandler.run
    def cancel_in_handler(self, worker, job):
        camp = campaign.client.research(campaign.key).campaigns[0]
        _issue_stop(campaign, camp, "cancelled")
        worker.cancel_requested.set()
        return original(self, worker, job)
    monkeypatch.setattr(ManagedTrialHandler, "run", cancel_in_handler)
    result = run(campaign, generated)
    assert result["state"] == "cancelled"
    assert result["outcome"] is None
    assert result["receipt_links"] == []
    assert len(result["node_attempts"]) == 1
    assert result["node_attempts"][0]["duration"] > 0
    assert result["trials"][0]["execution_status"] == "cancelled"
    assert campaign.client.research(campaign.key).campaigns[0].outcome is None


def test_measured_selection_overrun_blocks_dispatch(campaign, generated, monkeypatch):
    original = strategy_execution.execute_strategy
    def overrun(*args, **kwargs):
        result = original(*args, **kwargs)
        return replace(result, usage=replace(result.usage, duration=generated.selection_seconds + 1))
    monkeypatch.setattr(strategy_execution, "execute_strategy", overrun)
    with pytest.raises(RuntimeError, match="cumulative budget exceeded"):
        run(campaign, generated)
    view = campaign.client.research(campaign.key)
    assert not view.trials and not view.observations
    assert view.campaigns[0].outcome is None
