"""Adaptive campaign worker recovery (OMP-605-s03-s05)."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg.types.json import Jsonb

from omp_work.jobs import research_campaign
from omp_work.jobs.campaign_decisions import decode
from omp_work.jobs.research_trial import ResearchTrialHandler, trial_job_id
from omp_work.jobs.worker import Frozen, JobWorker
from test_adaptive_campaign_runner import _setup_tree_definition
from test_research_campaign_runner import _job, _run, campaign  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_STEPS = ["a", "a1", "a1x"]


class Crash(Exception):
    pass


def _definition(fix, *, budget: int = 3):
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    return _setup_tree_definition(fix, score_file, budget=budget)


def _persisted(fix):
    view = fix.client.research(fix.key)
    (campaign_row,) = view.campaigns
    trials = sorted(
        (t for t in view.trials if t.campaign_id == campaign_row.campaign_id),
        key=lambda t: decode(t.reason).index,
    )
    return campaign_row, trials, view.observations


def _job_by_id(fix, job_id: str) -> dict[str, Any] | None:
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        return fix.store.job_view(cur, fix.workspace_id, job_id)


def test_crash_after_dispatch_before_tick_runs_once(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner crashes after dispatch_trial before JobWorker.tick; rerun ticks without second dispatch."""
    fix = campaign
    definition = _definition(fix)
    real_dispatch = research_campaign.dispatch_trial
    dispatches: list[UUID] = []
    harness_runs: list[str] = []

    def dispatch_and_crash_second(*args, **kwargs):
        trial_id = kwargs["trial_id"]
        result = real_dispatch(*args, **kwargs)
        dispatches.append(trial_id)
        if len(dispatches) == 2:
            raise Crash("crash after dispatch before tick")
        return result

    real_execute = ResearchTrialHandler._execute_harness

    def counted_execute(self, harness, timeout):
        harness_runs.append(harness.decode("utf-8", "replace"))
        return real_execute(self, harness, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", dispatch_and_crash_second)
        patch.setattr(ResearchTrialHandler, "_execute_harness", counted_execute)
        with pytest.raises(Crash, match="crash after dispatch before tick"):
            _run(fix, definition)

    before_campaign, before_trials, before_obs = _persisted(fix)
    assert before_campaign.state == "running"
    assert len(before_trials) == 2
    assert len(before_obs) == 1
    assert len(harness_runs) == 1

    pending_trial = before_trials[1]
    pending_job = _job(fix, pending_trial.trial_id)
    assert pending_job is not None
    assert pending_job["status"] == "backlog"

    # Track operations on rerun
    rerun_dispatches: list[UUID] = []

    def tracked_dispatch(*args, **kwargs):
        rerun_dispatches.append(kwargs["trial_id"])
        return real_dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", tracked_dispatch)
        patch.setattr(ResearchTrialHandler, "_execute_harness", counted_execute)
        result = _run(fix, definition)

    assert result["state"] == "concluded"
    assert result["outcome"] == "supported"
    assert [t["step_id"] for t in result["trials"]] == _STEPS
    assert result["spent"] == 3.0
    assert result["best"] == {"step_id": "a1", "metric": 9.0}

    # Dispatch restart ran trial 1 harness once; total runs across both executions is 3 (one per step)
    assert len(harness_runs) == 3
    # Trial 1 was already dispatched in backlog; rerun only dispatched trial 2
    _, after_trials, after_obs = _persisted(fix)
    assert rerun_dispatches == [after_trials[2].trial_id]
    assert [t.trial_id for t in after_trials[:2]] == [t.trial_id for t in before_trials]
    assert len(after_obs) == 3


def test_frozen_after_observation_before_settlement_recovers_without_second_harness_run(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Frozen after writing observation leaves active lease; refuses until expired, then seals without rerun."""
    fix = campaign
    definition = _definition(fix)
    harness_runs: list[str] = []

    real_record = ResearchTrialHandler._record_observation

    def record_and_freeze_second(self, **kwargs):
        real_record(self, **kwargs)
        if self.run_count == 2:
            raise Frozen()

    real_execute = ResearchTrialHandler._execute_harness

    def counted_execute(self, harness, timeout):
        harness_runs.append(harness.decode("utf-8", "replace"))
        return real_execute(self, harness, timeout)

    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "_record_observation", record_and_freeze_second)
        patch.setattr(ResearchTrialHandler, "_execute_harness", counted_execute)
        with pytest.raises(RuntimeError, match="unsettled"):
            _run(fix, definition)

    before_campaign, before_trials, before_obs = _persisted(fix)
    assert before_campaign.state == "running"
    assert len(before_trials) == 2
    assert len(before_obs) == 2
    assert len(harness_runs) == 2

    pending_trial = before_trials[1]
    pending_job_id = trial_job_id(pending_trial.trial_id)
    pending_job = _job(fix, pending_trial.trial_id)
    assert pending_job is not None
    assert pending_job["status"] == "admitted"
    assert pending_job["settlement"] is None

    orig_obs = next(o for o in before_obs if o.trial_id == pending_trial.trial_id)
    orig_obs_id = orig_obs.observation_id
    orig_payload_sha256 = orig_obs.payload_sha256
    orig_reason = pending_trial.reason

    # Enqueue an unrelated older queued job in backlog with capability research.trial
    unrelated_job_id = f"trial:unrelated-{uuid4().hex[:12]}"
    work_id = fix.client.work_item(fix.key).work_id
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(
            """
            INSERT INTO omp_jobs.jobs(
                job_id, status, source, workspace_id, work_id, kind,
                required_capabilities, resources, lease_seconds, fence, attempt
            ) VALUES (
                %s, 'backlog', 'native', %s, %s, 'compute',
                '["research.trial"]'::jsonb, '{"cpu": 1}'::jsonb, 60, 0, 0
            )
            """,
            (unrelated_job_id, fix.workspace_id, work_id),
        )

    # Active lease refusal: unexpired unsettled job refuses progress
    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "_execute_harness", counted_execute)
        with pytest.raises(RuntimeError, match="research worker did not execute target job"):
            _run(fix, definition)

    # Refusal did not advance campaign or touch unrelated job
    view_refused = fix.client.research(fix.key)
    assert len(view_refused.trials) == 2
    assert len(view_refused.observations) == 2
    assert len(harness_runs) == 2
    assert _job_by_id(fix, unrelated_job_id)["status"] == "backlog"

    # Expire ONLY the fixture job's lease via store transaction
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s",
            (pending_job_id,),
        )

    # Rerun now recovers: observes persisted observation and seals without repeating harness
    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "_execute_harness", counted_execute)
        result = _run(fix, definition)

    assert result["state"] == "concluded"
    assert result["outcome"] == "supported"
    assert [t["step_id"] for t in result["trials"]] == _STEPS
    assert result["spent"] == 3.0
    assert result["best"] == {"step_id": "a1", "metric": 9.0}

    # Harness for trial 1 was NOT run a second time (only trial 2 ran on rerun)
    assert len(harness_runs) == 3

    # Assert original observation id, settlement digest, proposal reason
    after_campaign, after_trials, after_obs = _persisted(fix)
    recovered_obs = next(o for o in after_obs if o.trial_id == pending_trial.trial_id)
    assert recovered_obs.observation_id == orig_obs_id

    sealed_job = _job(fix, pending_trial.trial_id)
    assert sealed_job["status"] == "sealed"
    receipt = sealed_job["settlement"]["receipts"][0]
    assert receipt["evidence"]["content_digest"] == orig_payload_sha256
    assert receipt["evidence"]["trust"] == "verified"

    recovered_trial = next(t for t in after_trials if t.trial_id == pending_trial.trial_id)
    assert recovered_trial.reason == orig_reason

    # The unrelated older queued job was never touched and remains in backlog
    assert _job_by_id(fix, unrelated_job_id)["status"] == "backlog"


@pytest.mark.parametrize("corrupt_field", ["issuer", "trust", "content_digest"])
def test_corrupt_recovered_receipt_prevents_next_proposal(
    campaign, corrupt_field: str,
) -> None:
    """Corrupt recovered evaluator receipt prevents next proposal; pending observation cannot supply score."""
    fix = campaign
    definition = _definition(fix, budget=3)

    # Run trial 0 and crash before trial 1 dispatch
    real_dispatch = research_campaign.dispatch_trial
    calls: list[UUID] = []

    def crash_before_second_dispatch(*args, **kwargs):
        calls.append(kwargs["trial_id"])
        if len(calls) == 2:
            raise Crash("stop after trial 0 settlement")
        return real_dispatch(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", crash_before_second_dispatch)
        with pytest.raises(Crash, match="stop after trial 0 settlement"):
            _run(fix, definition)

    before_campaign, before_trials, before_obs = _persisted(fix)
    assert len(before_trials) == 2
    assert len(before_obs) == 1
    trial_0_id = before_trials[0].trial_id
    job_0_id = trial_job_id(trial_0_id)

    # Corrupt settled trial 0's receipt in PostgreSQL store
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute("SELECT settlement FROM omp_jobs.jobs WHERE job_id = %s", (job_0_id,))
        settlement = cur.fetchone()["settlement"]
        receipt = settlement["receipts"][0]
        if corrupt_field == "issuer":
            receipt["issuer_component_sha256"] = "0" * 64
        elif corrupt_field == "trust":
            receipt["evidence"]["trust"] = "unverified"
        elif corrupt_field == "content_digest":
            receipt["evidence"]["content_digest"] = "0" * 64
        cur.execute(
            "UPDATE omp_jobs.jobs SET settlement = %s WHERE job_id = %s",
            (Jsonb(settlement), job_0_id),
        )

    # Resuming must refuse progress because receipt is corrupt
    with pytest.raises(RuntimeError, match="evaluator receipt is not bound to its observation"):
        _run(fix, definition)

    # Exactly 2 trials exist (trial 0 settled, trial 1 pending proposal before dispatch); no third trial proposed
    view = fix.client.research(fix.key)
    assert len(view.trials) == 2
    assert view.campaigns[0].state == "running"
    assert view.campaigns[0].outcome is None


def test_pending_observation_with_corrupt_settlement_cannot_supply_score(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a pending trial's settlement has corrupt receipt evidence, it cannot supply score and prevents next proposal."""
    fix = campaign
    definition = _definition(fix, budget=3)

    real_record = ResearchTrialHandler._record_observation

    def record_and_freeze_second(self, **kwargs):
        real_record(self, **kwargs)
        if self.run_count == 2:
            raise Frozen()

    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "_record_observation", record_and_freeze_second)
        with pytest.raises(RuntimeError, match="unsettled"):
            _run(fix, definition)

    before_campaign, before_trials, before_obs = _persisted(fix)
    assert len(before_trials) == 2
    assert len(before_obs) == 2

    pending_trial = before_trials[1]
    pending_job_id = trial_job_id(pending_trial.trial_id)

    # Expire pending job's lease
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE job_id = %s",
            (pending_job_id,),
        )

    # Tamper with observe output to return an untrusted receipt
    real_observe = ResearchTrialHandler.observe

    def observe_corrupt(self, worker, job):
        settlement = real_observe(self, worker, job)
        if settlement and settlement.receipts:
            settlement.receipts[0]["evidence"]["trust"] = "untrusted"
        return settlement

    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "observe", observe_corrupt)
        with pytest.raises(RuntimeError, match="evaluator receipt is not bound to its observation"):
            _run(fix, definition)

    # Campaign did not propose trial 2 (a1x); score from pending observation payload was not used
    view = fix.client.research(fix.key)
    assert len(view.trials) == 2
    assert view.campaigns[0].state == "running"
    assert view.campaigns[0].outcome is None

