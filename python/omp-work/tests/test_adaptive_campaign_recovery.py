"""Recorded adaptive decisions resume without repeating trial side effects."""

from __future__ import annotations

from dataclasses import replace
import os

import pytest

from omp_work.jobs import research_campaign
from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_decisions import decode, encode, observations_digest
from omp_work.jobs.research_trial import ResearchTrialHandler
from omp_work.jobs.worker import Frozen, JobWorker
from test_adaptive_campaign_runner import _setup_tree_definition
from test_research_campaign_runner import _job, campaign  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


class Crash(Exception):
    pass


def _definition(fix):
    score = fix.root / "score.txt"
    score.write_text("9\n")
    definition = _setup_tree_definition(fix, score, budget=4)
    # Extend the selected branch so recovery must make another adaptive pick.
    return replace(definition, trials=tuple(
        replace(t, parent="a1") if t.id == "a2" else t
        for t in definition.trials
    ))


def _run(fix, definition, **kwargs):
    return research_campaign.run_research_campaign(
        fix.client, fix.store, fix.key, definition,
        harness_dir=fix.harness_dir, actor_id=fix.actor_id, **kwargs,
    )


def _trials(fix, campaign_id):
    return sorted(
        (t for t in fix.client.research(fix.key).trials if t.campaign_id == campaign_id),
        key=lambda t: decode(t.reason).index,
    )


@pytest.mark.parametrize("interruption", ["proposal", "dispatch", "observation", "settlement"])
def test_resume_reuses_recorded_decision_and_runs_each_trial_once(
    campaign, monkeypatch, interruption,
):
    fix = campaign
    definition = _definition(fix)
    uninterrupted = _run(fix, definition)
    runs = []
    execute_harness = ResearchTrialHandler._execute_harness
    dispatch = research_campaign.dispatch_trial
    run = ResearchTrialHandler.run
    tick = JobWorker.tick
    dispatched = []

    def counted_harness(self, harness, timeout):
        runs.append(harness)
        return execute_harness(self, harness, timeout)

    def interrupt_dispatch(*args, **kwargs):
        dispatched.append(kwargs["trial_id"])
        if len(dispatched) == 3 and interruption == "proposal":
            raise Crash()
        result = dispatch(*args, **kwargs)
        if len(dispatched) == 3 and interruption == "dispatch":
            raise Crash()
        return result

    def freeze_after_observation(self, worker, job):
        result = run(self, worker, job)
        if self.run_count == 3 and interruption == "observation":
            raise Frozen()
        return result

    def interrupt_settlement(self, job_id=None):
        result = tick(self, job_id=job_id)
        if len(runs) == 3 and interruption == "settlement":
            raise Crash()
        return result

    monkeypatch.setattr(ResearchTrialHandler, "_execute_harness", counted_harness)
    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", interrupt_dispatch)
        patch.setattr(ResearchTrialHandler, "run", freeze_after_observation)
        patch.setattr(JobWorker, "tick", interrupt_settlement)
        error = RuntimeError if interruption == "observation" else Crash
        with pytest.raises(error):
            _run(fix, definition, lease_seconds=1)

    view = fix.client.research(fix.key)
    active = next(c for c in view.campaigns if c.state == "running")
    before = _trials(fix, active.campaign_id)
    assert len(before) == 3
    pending = before[2]
    original_job = _job(fix, pending.trial_id)
    observations = {o.observation_id: o for o in view.observations}
    if interruption == "proposal":
        assert original_job is None
    else:
        assert original_job["lease_seconds"] == 1
        if interruption != "settlement":
            assert original_job["settlement"] is None
    if interruption == "observation":
        assert any(o.trial_id == pending.trial_id for o in observations.values())

    picks = []
    next_step = research_campaign.next_step
    resumed_dispatches = []

    def select(policy, tree, opened, **kwargs):
        # Index 2 must execute its persisted pick before search resumes.
        assert len(opened) >= 3
        picks.append(len(opened))
        return next_step(policy, tree, opened, **kwargs)

    def track_dispatch(*args, **kwargs):
        resumed_dispatches.append(kwargs["trial_id"])
        return dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", select)
        patch.setattr(research_campaign, "dispatch_trial", track_dispatch)
        # Resume's default lease must not replace the existing one-second lease.
        result = _run(fix, definition)

    after = _trials(fix, active.campaign_id)
    assert after[:3] == before
    assert len(after) == len({t.trial_id for t in after}) == 4
    assert len({t.decision_id for t in after}) == 4
    assert len(runs) == 4
    assert picks[0] == 3
    expected_dispatches = [t.trial_id for t in after[3:]]
    if interruption == "proposal":
        expected_dispatches.insert(0, pending.trial_id)
    assert resumed_dispatches == expected_dispatches
    if original_job is not None:
        settled = _job(fix, pending.trial_id)
        assert settled["resources"] == original_job["resources"]
        assert settled["lease_seconds"] == original_job["lease_seconds"]
        assert settled["settlement"]["outcome"] == "succeeded"
    final_view = fix.client.research(fix.key)
    assert all(o in final_view.observations for o in observations.values())
    campaign_observations = [o for o in final_view.observations if o.campaign_id == active.campaign_id]
    assert len(campaign_observations) == len({o.trial_id for o in campaign_observations}) == 4
    assert [t["step_id"] for t in result["trials"]] == [
        t["step_id"] for t in uninterrupted["trials"]
    ]
    assert result["spent"] == uninterrupted["spent"] == 4.0
    assert result["best"] == uninterrupted["best"]
    prefix = []
    for trial in after:
        decision = decode(trial.reason)
        assert decision.observations_sha256 == observations_digest(prefix)
        completion = read_trial_completion(
            fix.client, fix.store, fix.key, active.campaign_id, trial, fix.actor_id, "score",
        )
        prefix.append((trial.trial_id, completion.value))


@pytest.mark.parametrize("corruption", ["malformed", "digest"])
def test_corrupt_pending_decision_refuses_before_dispatch(campaign, monkeypatch, corruption):
    fix = campaign
    definition = _definition(fix)
    dispatch = research_campaign.dispatch_trial
    calls = []

    def interrupt(*args, **kwargs):
        calls.append(kwargs["trial_id"])
        if len(calls) == 3:
            raise Crash()
        return dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", interrupt)
        with pytest.raises(Crash):
            _run(fix, definition)
    view = fix.client.research(fix.key)
    active = view.campaigns[0]
    pending = _trials(fix, active.campaign_id)[2]
    reason = '{"v":1}' if corruption == "malformed" else encode(
        replace(decode(pending.reason), observations_sha256="0" * 64)
    )
    corrupt_view = view.model_copy(update={
        "trials": tuple(t.model_copy(update={"reason": reason}) if t.trial_id == pending.trial_id else t
                        for t in view.trials),
    })

    def forbidden(*args, **kwargs):
        pytest.fail("corrupt record reached execution or selection")

    with monkeypatch.context() as patch:
        patch.setattr(fix.client, "research", lambda key: corrupt_view)
        patch.setattr(research_campaign, "dispatch_trial", forbidden)
        patch.setattr(research_campaign, "next_step", forbidden)
        patch.setattr(JobWorker, "tick", forbidden)
        with pytest.raises(RuntimeError, match="malformed|digest does not match"):
            _run(fix, definition)
    assert fix.client.research(fix.key) == view
    assert _job(fix, pending.trial_id) is None


def test_active_long_lease_stays_pending_without_duplicate_effects(campaign, monkeypatch):
    fix = campaign
    definition = _definition(fix)
    run = ResearchTrialHandler.run

    def freeze(self, worker, job):
        run(self, worker, job)
        raise Frozen()

    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "run", freeze)
        with pytest.raises(RuntimeError, match="unsettled"):
            _run(fix, definition)
    before = fix.client.research(fix.key)
    assert len(before.trials) == len(before.observations) == 1
    pending = before.trials[0]

    def forbidden(*args, **kwargs):
        pytest.fail("active lease repeated trial effects or selection")

    with monkeypatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "_execute_harness", forbidden)
        patch.setattr(research_campaign, "dispatch_trial", forbidden)
        patch.setattr(research_campaign, "next_step", forbidden)
        with pytest.raises(RuntimeError, match="unsettled"):
            _run(fix, definition, lease_seconds=1)
    assert fix.client.research(fix.key) == before
    job = _job(fix, pending.trial_id)
    assert job["settlement"] is None
    assert job["lease_seconds"] == 60
