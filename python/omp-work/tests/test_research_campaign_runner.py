"""Campaign runner settles targeted jobs and concludes from persisted evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from omp_work.jobs.campaign_definition import load_definition
from omp_work.jobs.research_campaign import run_research_campaign
from omp_work.jobs.research_trial import ResearchTrialHandler, trial_job_id
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.worker import Frozen, JobWorker
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.v1.client import WorkClient
from test_workflow_service import _create, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@pytest.fixture
def campaign(service, tmp_path: Path):
    apply_jobs_migrations(service.config)
    workspace_id, actor_id = uuid4(), uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "campaign runner")
    bearer = service.capabilities / f"campaign-runner-{workspace_id}.json"
    bearer.write_text(json.dumps({
        "token": f"campaign-runner-{workspace_id}", "actor_id": str(actor_id),
        "actor_kind": "automation", "workspaces": [str(workspace_id)],
        "scopes": ["work.read", "work.execute", "work.approve"],
    }))
    bearer.chmod(0o600)
    client = WorkClient(
        "http://testserver", workspace_id, bearer,
        transport=service.client._transport,
    )
    try:
        yield SimpleNamespace(
            client=client, store=NativeJobStore(service.config),
            key=item["key"], workspace_id=workspace_id, actor_id=actor_id,
            harness_dir=tmp_path / "cached", root=tmp_path,
        )
    finally:
        client.close()


def _definition(fix, scores: tuple[int, ...]):
    trials = []
    for index, score in enumerate(scores):
        path = fix.root / f"trial-{index}.sh"
        path.write_text(f'echo "METRIC score={score}"\n')
        trials.append({"harness": path.name})
    path = fix.root / "definition.json"
    path.write_text(json.dumps({
        "objective": "Every trial reaches score one", "domain": "machine_learning",
        "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
        "trials": trials,
    }))
    return load_definition(path)


def _run(fix, definition):
    return run_research_campaign(
        fix.client, fix.store, fix.key, definition,
        harness_dir=fix.harness_dir, actor_id=fix.actor_id,
    )


def _job(fix, trial_id):
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        return fix.store.job_view(cur, fix.workspace_id, trial_job_id(trial_id))


def test_same_item_supports_all_trials_refutes_mixed_and_leaves_older_job_backlog(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    supported_definition = _definition(fix, (1, 1))
    # Leave an older compatible research job queued before the real run.
    with monkeypatch.context() as patch:
        patch.setattr(JobWorker, "tick", lambda self, job_id=None: None)
        with pytest.raises(RuntimeError, match="did not execute target job"):
            _run(fix, supported_definition)
    before = fix.client.research(fix.key)
    older_trial = before.trials[0]
    assert _job(fix, older_trial.trial_id)["status"] == "backlog"

    # The loaded bytes remain authoritative after source harness edits.
    supported_definition.trials[0].harness.write_text('echo "METRIC score=0"\n')
    supported = _run(fix, supported_definition)
    refuted = _run(fix, _definition(fix, (1, 0)))
    assert supported["outcome"] == "supported"
    assert refuted["outcome"] == "refuted"
    assert "trial 2" in refuted["reason"]
    assert "score" in refuted["reason"]
    assert supported["campaign_id"] != refuted["campaign_id"]
    assert _job(fix, older_trial.trial_id)["status"] == "backlog"

    view = fix.client.research(fix.key)
    for result, scores in ((supported, (1.0, 1.0)), (refuted, (1.0, 0.0))):
        campaign_id = UUID(result["campaign_id"])
        persisted = next(c for c in view.campaigns if c.campaign_id == campaign_id)
        assert persisted.state == result["state"] == "concluded"
        assert persisted.outcome == result["outcome"]
        assert persisted.outcome_reason == result["reason"]
        persisted_trials = [t for t in view.trials if t.campaign_id == campaign_id]
        assert {str(t.trial_id) for t in persisted_trials} == {
            t["trial_id"] for t in result["trials"]
        }
        assert len({t.decision_id for t in persisted_trials}) == 2
        for trial, score in zip(result["trials"], scores, strict=True):
            assert trial["execution_status"] == "completed"
            assert trial["metrics"] == {"score": score}
            observation = next(o for o in view.observations if str(o.trial_id) == trial["trial_id"])
            assert observation.campaign_id == campaign_id
            assert observation.source_ref == f"job:{trial_job_id(trial['trial_id'])}"
            assert observation.payload["metrics"] == trial["metrics"]
            job = _job(fix, trial["trial_id"])
            assert job["status"] == "sealed"
            assert job["settlement"]["outcome"] == "succeeded"
            receipt = job["settlement"]["receipts"][0]
            assert receipt["evidence"]["content_digest"] == observation.payload_sha256
            assert receipt["evidence"]["metrics"] == trial["metrics"]


def test_unsettled_tick_with_observation_does_not_conclude(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ResearchTrialHandler.run

    def freeze_after_recording(self, worker, job):
        original(self, worker, job)
        raise Frozen()

    monkeypatch.setattr(ResearchTrialHandler, "run", freeze_after_recording)
    with pytest.raises(RuntimeError, match="unsettled"):
        _run(campaign, _definition(campaign, (1,)))
    view = campaign.client.research(campaign.key)
    assert len(view.observations) == 1
    assert view.campaigns[0].state == "running"
    assert view.campaigns[0].outcome is None
    job = _job(campaign, view.trials[0].trial_id)
    assert job["status"] == "admitted"
    assert job["worker_id"] is not None
    assert job["settlement"] is None


@pytest.mark.parametrize("missing_or_mismatched", ["missing", "campaign_id", "trial_id", "source_ref"])
def test_absent_or_mismatched_persisted_observation_prevents_conclusion(
    campaign, monkeypatch: pytest.MonkeyPatch, missing_or_mismatched: str,
) -> None:
    original = campaign.client.research

    def read(key):
        view = original(key)
        if missing_or_mismatched == "missing":
            return view.model_copy(update={"observations": ()})
        observation = view.observations[0]
        value = "job:wrong" if missing_or_mismatched == "source_ref" else uuid4()
        return view.model_copy(update={
            "observations": (observation.model_copy(update={missing_or_mismatched: value}),),
        })

    monkeypatch.setattr(campaign.client, "research", read)
    with pytest.raises(RuntimeError, match="matching persisted observation"):
        _run(campaign, _definition(campaign, (1,)))
    view = original(campaign.key)
    assert view.campaigns[0].state == "running"
    assert view.campaigns[0].outcome is None
    assert _job(campaign, view.trials[0].trial_id)["status"] == "sealed"


def test_failed_execution_refutes_even_when_metrics_pass(campaign) -> None:
    definition = _definition(campaign, (1,))
    definition.trials[0].harness.write_text('echo "METRIC score=1"\nexit 1\n')
    result = _run(campaign, load_definition(campaign.root / "definition.json"))
    assert result["outcome"] == "refuted"
    assert "crashed" in result["reason"]
    trial = result["trials"][0]
    assert trial["execution_status"] == "crashed"
    assert trial["metrics"] == {"score": 1.0}
    job = _job(campaign, trial["trial_id"])
    assert job["status"] == "failed"
    assert job["settlement"]["outcome"] == "failed"
    view = campaign.client.research(campaign.key)
    assert view.campaigns[0].outcome == "refuted"
    assert view.observations[0].source_ref == f"job:{trial_job_id(trial['trial_id'])}"
