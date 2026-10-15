"""Adaptive campaign resume reuses persisted progress without repeating effects (OMP-605-s03-s04)."""

from __future__ import annotations

import os
from uuid import UUID

import pytest

from omp_work.jobs import research_campaign
from omp_work.jobs.campaign_decisions import decode, observations_digest
from omp_work.jobs.research_trial import trial_job_id
from omp_work.jobs.worker import JobWorker
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


def _definition(fix):
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    return _setup_tree_definition(fix, score_file, budget=3)


def _counters(fix, monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    seen: dict[str, list] = {"commands": [], "ticks": [], "dispatches": []}
    execute = fix.client.execute
    dispatch = research_campaign.dispatch_trial
    tick = JobWorker.tick

    def counted_execute(envelope):
        if envelope.command.type != "register_research_component":
            seen["commands"].append(envelope.command.type)
        return execute(envelope)

    def counted_dispatch(*args, **kwargs):
        seen["dispatches"].append(kwargs["trial_id"])
        return dispatch(*args, **kwargs)

    def counted_tick(self, job_id=None):
        seen["ticks"].append(job_id)
        return tick(self, job_id=job_id)

    monkeypatch.setattr(fix.client, "execute", counted_execute)
    monkeypatch.setattr(research_campaign, "dispatch_trial", counted_dispatch)
    monkeypatch.setattr(JobWorker, "tick", counted_tick)
    return seen


def _persisted(fix):
    view = fix.client.research(fix.key)
    (campaign_row,) = view.campaigns
    trials = sorted(
        (t for t in view.trials if t.campaign_id == campaign_row.campaign_id),
        key=lambda t: decode(t.reason).index,
    )
    return campaign_row, trials, view.observations


def _assert_complete(fix, result) -> None:
    campaign_row, trials, observations = _persisted(fix)
    assert campaign_row.state == "concluded"
    assert [t["step_id"] for t in result["trials"]] == _STEPS
    assert [t["trial_id"] for t in result["trials"]] == [str(t.trial_id) for t in trials]
    assert len(observations) == 3
    assert len({o.trial_id for o in observations}) == 3
    assert result["spent"] == 3.0
    pairs = []
    for trial, summary in zip(trials, result["trials"], strict=True):
        decision = decode(trial.reason)
        assert decision.observations_sha256 == observations_digest(pairs)
        pairs.append((trial.trial_id, summary["metrics"]["score"]))
    assert result["best"] == {"step_id": "a1", "metric": 9.0}


def test_crash_after_proposal_before_dispatch_reuses_pending_trial(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _definition(fix)
    real = research_campaign.dispatch_trial
    calls = []

    def crash_second(*args, **kwargs):
        calls.append(kwargs["trial_id"])
        if len(calls) == 2:
            raise Crash()
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", crash_second)
        with pytest.raises(Crash):
            _run(fix, definition)
    before_campaign, before_trials, _ = _persisted(fix)
    assert before_campaign.state == "running"
    assert len(before_trials) == 2
    assert _job(fix, before_trials[1].trial_id) is None

    seen = _counters(fix, monkeypatch)
    result = _run(fix, definition)

    assert result["campaign_id"] == str(before_campaign.campaign_id)
    _assert_complete(fix, result)
    _, trials, _ = _persisted(fix)
    assert [(t.trial_id, t.decision_id, t.reason, t.seed) for t in trials[:2]] == [
        (t.trial_id, t.decision_id, t.reason, t.seed) for t in before_trials
    ]
    assert seen["dispatches"] == [t.trial_id for t in trials[1:]]
    assert seen["ticks"] == [trial_job_id(t.trial_id) for t in trials[1:]]
    assert seen["commands"].count("propose_research_trial") == 1
    for forbidden in ("create_research_campaign", "admit_research_campaign"):
        assert forbidden not in seen["commands"]
    assert seen["commands"].count("set_research_campaign_state") == 1


def test_crash_after_settlement_before_next_pick_appends_next_index_once(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _definition(fix)
    real = research_campaign.next_step
    picks = []

    def crash_third(*args, **kwargs):
        picks.append(1)
        if len(picks) == 3:
            raise Crash()
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", crash_third)
        with pytest.raises(Crash):
            _run(fix, definition)
    before_campaign, before_trials, before_obs = _persisted(fix)
    assert len(before_trials) == 2 and len(before_obs) == 2

    seen = _counters(fix, monkeypatch)
    result = _run(fix, definition)

    assert result["campaign_id"] == str(before_campaign.campaign_id)
    _assert_complete(fix, result)
    _, trials, _ = _persisted(fix)
    assert [t.trial_id for t in trials[:2]] == [t.trial_id for t in before_trials]
    assert seen["commands"].count("propose_research_trial") == 1
    assert seen["dispatches"] == [trials[2].trial_id]
    assert seen["ticks"] == [trial_job_id(trials[2].trial_id)]


def test_restart_in_evaluating_only_concludes(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _definition(fix)
    execute = fix.client.execute

    def crash_on_conclude(envelope):
        if envelope.command.type == "conclude_research_campaign":
            raise Crash()
        return execute(envelope)

    with monkeypatch.context() as patch:
        patch.setattr(fix.client, "execute", crash_on_conclude)
        with pytest.raises(Crash):
            _run(fix, definition)
    before_campaign, before_trials, _ = _persisted(fix)
    assert before_campaign.state == "evaluating"

    seen = _counters(fix, monkeypatch)
    result = _run(fix, definition)

    assert result["campaign_id"] == str(before_campaign.campaign_id)
    assert result["outcome"] == "supported"
    _assert_complete(fix, result)
    assert seen["commands"] == ["conclude_research_campaign"]
    assert seen["dispatches"] == [] and seen["ticks"] == []


def test_changed_definition_and_ambiguous_matches_do_not_attach(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _definition(fix)
    real = research_campaign.next_step
    picks = []

    def crash_second(*args, **kwargs):
        picks.append(1)
        if len(picks) == 2:
            raise Crash()
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", crash_second)
        with pytest.raises(Crash):
            _run(fix, definition)
    old_id = fix.client.research(fix.key).campaigns[0].campaign_id

    changed = _setup_tree_definition(fix, fix.root / "score.txt", budget=2)
    result = _run(fix, changed)
    assert UUID(result["campaign_id"]) != old_id
    states = {c.campaign_id: c.state for c in fix.client.research(fix.key).campaigns}
    assert states[old_id] == "running"

    # Second identical running campaign makes the match ambiguous.
    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "_resumable_campaign", lambda *a: None)
        with pytest.raises(Crash):
            with monkeypatch.context() as inner:
                picks.clear()
                inner.setattr(research_campaign, "next_step", crash_second)
                _run(fix, definition)
    with pytest.raises(RuntimeError, match="ambiguous resume"):
        _run(fix, definition)
