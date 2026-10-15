"""Adaptive campaign resume reuses persisted progress without repeating effects (OMP-605-s03-s04 / OMP-605-s04)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_work.__main__ import main
from omp_work.jobs import research_campaign
from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_decisions import decode, observations_digest
from omp_work.jobs.campaign_definition import load_definition
from omp_work.jobs.research_campaign import run_research_campaign
from omp_work.jobs.research_trial import trial_job_id
from omp_work.jobs.worker import JobWorker
from omp_work.v1.client import _envelope
from omp_work.v1.models import (
    CancelResearchCampaignCommand,
    CancelResearchCampaignPayload,
    SetResearchCampaignStateCommand,
    SetResearchCampaignStatePayload,
)
from test_adaptive_campaign_runner import _setup_tree_definition
from test_research_campaign_cli import (
    _assert_unbuilt,
    _campaign_result,
    _layout,
    _spies,
)
from test_research_campaign_runner import _definition as _fixed_definition
from test_research_campaign_runner import _job, _run, campaign  # noqa: F401
from test_workflow_service import _create

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


def _make_definition_file(
    fix,
    *,
    harness_text_a1x: str | None = None,
    order: list[str] | None = None,
    parent_a1x: str = "a1",
    cost_a2: float = 1.0,
    metric: str = "score",
    width: int = 1,
    budget: int = 3,
):
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    harness_a = fix.root / "a.sh"
    harness_a.write_text('#!/bin/bash\necho "METRIC score=5"\n')
    harness_a1 = fix.root / "a1.sh"
    harness_a1.write_text(f'#!/bin/bash\nscore=$(cat "{score_file}")\necho "METRIC score=$score"\n')
    harness_a2 = fix.root / "a2.sh"
    harness_a2.write_text('#!/bin/bash\necho "METRIC score=3"\n')
    harness_a1x = fix.root / "a1x.sh"
    if harness_text_a1x is not None:
        harness_a1x.write_text(harness_text_a1x)
    else:
        harness_a1x.write_text('#!/bin/bash\necho "METRIC score=8"\n')

    all_trials = {
        "a": {"id": "a", "harness": harness_a.name},
        "a1": {"id": "a1", "parent": "a", "harness": harness_a1.name},
        "a2": {"id": "a2", "parent": "a", "harness": harness_a2.name, "cost": cost_a2},
        "a1x": {"id": "a1x", "parent": parent_a1x, "harness": harness_a1x.name},
    }
    if order is None:
        order = ["a", "a1", "a2", "a1x"]
    trial_list = [all_trials[k] for k in order]

    def_path = fix.root / f"definition-{uuid4().hex[:8]}.json"
    def_path.write_text(json.dumps({
        "objective": "Adaptive search tree evaluation",
        "domain": "machine_learning",
        "criteria": [
            {"metric": "score", "op": "gte", "threshold": 1},
            {"metric": "other_metric", "op": "gte", "threshold": 1},
        ],
        "search": {
            "policy": "greedy",
            "budget": budget,
            "width": width,
            "seed": 0,
            "metric": metric,
        },
        "trials": trial_list,
    }))
    return load_definition(def_path)


def test_interrupt_before_selection_index_2_and_resume_with_recomputed_own_digest(
    campaign, monkeypatch: pytest.MonkeyPatch, service,
) -> None:
    fix = campaign
    definition = _make_definition_file(fix)

    # 1. Uninterrupted reference run on another work item
    item_uninterrupted = _create(service, fix.workspace_id, "uninterrupted campaign item")
    uninterrupted_harness = fix.root / "harness_uninterrupted"
    uninterrupted_result = run_research_campaign(
        fix.client,
        fix.store,
        item_uninterrupted["key"],
        definition,
        harness_dir=uninterrupted_harness,
        actor_id=fix.actor_id,
    )
    assert uninterrupted_result["state"] == "concluded"
    assert len(uninterrupted_result["trials"]) == 3
    assert len(uninterrupted_result["decisions"]) == 3

    # 2. Interrupted run on fix.key: interrupt before selection index 2 (third next_step call)
    real_next_step = research_campaign.next_step
    picks: list[int] = []

    def interrupt_before_third_pick(*args, **kwargs):
        picks.append(1)
        if len(picks) == 3:
            raise Crash("interrupt before selection index 2")
        return real_next_step(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", interrupt_before_third_pick)
        with pytest.raises(Crash, match="interrupt before selection index 2"):
            _run(fix, definition)

    # Interrupted campaign has 2 settled trials (indices 0 and 1)
    before_campaign, before_trials, before_obs = _persisted(fix)
    assert before_campaign.state == "running"
    assert len(before_trials) == 2
    assert len(before_obs) == 2

    # Recompute digest strictly from interrupted campaign's OWN persisted metrics
    pairs: list[tuple[str, float | None]] = []
    for trial in before_trials:
        completion = read_trial_completion(
            fix.client, fix.store, fix.key, before_campaign.campaign_id, trial, fix.actor_id, "score"
        )
        pairs.append((str(trial.trial_id), completion.value))
    interrupted_own_digest = observations_digest(pairs)

    # 3. Resume the same campaign using resume_campaign_id
    resumed_result = run_research_campaign(
        fix.client,
        fix.store,
        fix.key,
        definition,
        harness_dir=fix.harness_dir,
        actor_id=fix.actor_id,
        resume_campaign_id=before_campaign.campaign_id,
    )

    # Assert sequence and policy match uninterrupted run
    assert resumed_result["campaign_id"] == str(before_campaign.campaign_id)
    assert resumed_result["state"] == "concluded"
    assert [t["step_id"] for t in resumed_result["trials"]] == [t["step_id"] for t in uninterrupted_result["trials"]]
    assert [d["policy"] for d in resumed_result["decisions"]] == [d["policy"] for d in uninterrupted_result["decisions"]]

    # Assert index 2 decision carries digest from interrupted campaign's own persisted metrics
    decisions = resumed_result["decisions"]
    assert len(decisions) == 3
    assert decisions[2]["index"] == 2
    assert decisions[2]["observations_count"] == 2
    assert decisions[2]["observations_sha256"] == interrupted_own_digest

    # Trials 0 and 1 match the interrupted campaign's trials
    assert [t["trial_id"] for t in resumed_result["trials"][:2]] == [str(t.trial_id) for t in before_trials]


def test_changed_definition_refuses_before_dispatch(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    base_def = _make_definition_file(fix)

    # Interrupt before index 2
    real_next_step = research_campaign.next_step
    picks: list[int] = []

    def interrupt_before_third_pick(*args, **kwargs):
        picks.append(1)
        if len(picks) == 3:
            raise Crash()
        return real_next_step(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", interrupt_before_third_pick)
        with pytest.raises(Crash):
            _run(fix, base_def)

    before_campaign, _, _ = _persisted(fix)
    cid = before_campaign.campaign_id

    dispatches: list[UUID] = []
    real_dispatch = research_campaign.dispatch_trial

    def counted_dispatch(*args, **kwargs):
        dispatches.append(kwargs["trial_id"])
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(research_campaign, "dispatch_trial", counted_dispatch)

    variants = [
        # Changed unopened harness
        _make_definition_file(fix, harness_text_a1x='#!/bin/bash\necho "METRIC score=99"\n'),
        # Changed order
        _make_definition_file(fix, order=["a", "a2", "a1", "a1x"]),
        # Changed parent
        _make_definition_file(fix, parent_a1x="a"),
        # Changed cost
        _make_definition_file(fix, cost_a2=2.0),
        # Changed search metric
        _make_definition_file(fix, metric="other_metric"),
        # Changed search width
        _make_definition_file(fix, width=2),
        # Changed search budget
        _make_definition_file(fix, budget=4),
    ]

    for changed_def in variants:
        dispatches.clear()
        with pytest.raises(RuntimeError, match="spec hash does not match"):
            run_research_campaign(
                fix.client,
                fix.store,
                fix.key,
                changed_def,
                harness_dir=fix.harness_dir,
                actor_id=fix.actor_id,
                resume_campaign_id=cid,
            )
        assert dispatches == []


def test_zero_decision_restart_and_drift_refusal(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _make_definition_file(fix)

    # Interrupt before very first pick (zero decisions)
    def interrupt_immediately(*args, **kwargs):
        raise Crash("interrupt before first pick")

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", interrupt_immediately)
        with pytest.raises(Crash, match="interrupt before first pick"):
            _run(fix, definition)

    before_campaign, before_trials, _ = _persisted(fix)
    assert len(before_trials) == 0
    assert before_campaign.state == "running"
    cid = before_campaign.campaign_id

    # Changed definition must refuse before dispatch even with zero decisions
    changed_def = _make_definition_file(fix, cost_a2=2.0)
    dispatches: list[UUID] = []
    real_dispatch = research_campaign.dispatch_trial

    def counted_dispatch(*args, **kwargs):
        dispatches.append(kwargs["trial_id"])
        return real_dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", counted_dispatch)
        with pytest.raises(RuntimeError, match="spec hash does not match"):
            run_research_campaign(
                fix.client,
                fix.store,
                fix.key,
                changed_def,
                harness_dir=fix.harness_dir,
                actor_id=fix.actor_id,
                resume_campaign_id=cid,
            )
        assert dispatches == []

    # Successful zero-decision restart with original definition
    seen = _counters(fix, monkeypatch)
    result = run_research_campaign(
        fix.client,
        fix.store,
        fix.key,
        definition,
        harness_dir=fix.harness_dir,
        actor_id=fix.actor_id,
        resume_campaign_id=cid,
    )
    assert result["campaign_id"] == str(cid)
    assert result["state"] == "concluded"
    assert len(result["trials"]) == 3
    # Skips create and admit
    for forbidden in ("create_research_campaign", "admit_research_campaign"):
        assert forbidden not in seen["commands"]


def test_nonrunning_and_wrong_item_campaign_refuses(
    campaign, service, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition = _definition(fix)

    # 1. Run a campaign to conclusion
    result = _run(fix, definition)
    concluded_id = UUID(result["campaign_id"])

    # Resuming concluded campaign refuses
    with pytest.raises(RuntimeError, match="expected running"):
        run_research_campaign(
            fix.client,
            fix.store,
            fix.key,
            definition,
            harness_dir=fix.harness_dir,
            actor_id=fix.actor_id,
            resume_campaign_id=concluded_id,
        )

    # 2. Cancelled campaign refuses
    other_item = _create(service, fix.workspace_id, "cancellable campaign")
    def_clean = _definition(fix)

    def crash_pick(*a, **kw):
        raise Crash()

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "next_step", crash_pick)
        with pytest.raises(Crash):
            run_research_campaign(
                fix.client,
                fix.store,
                other_item["key"],
                def_clean,
                harness_dir=fix.harness_dir / "clean",
                actor_id=fix.actor_id,
            )
    view = fix.client.research(other_item["key"])
    running_cid = view.campaigns[0].campaign_id

    # Transition to cancelled
    fix.client.execute(_envelope(fix.workspace_id, CancelResearchCampaignCommand(
        type="cancel_research_campaign",
        payload=CancelResearchCampaignPayload(
            campaign_id=running_cid,
            work_id=other_item["work_id"],
            reason="test cancel",
        ),
    )))

    with pytest.raises(RuntimeError, match="cancelled|expected running"):
        run_research_campaign(
            fix.client,
            fix.store,
            other_item["key"],
            def_clean,
            harness_dir=fix.harness_dir / "clean",
            actor_id=fix.actor_id,
            resume_campaign_id=running_cid,
        )

    # 3. Wrong item refuses
    with pytest.raises(RuntimeError, match="not on work item"):
        run_research_campaign(
            fix.client,
            fix.store,
            fix.key,
            definition,
            harness_dir=fix.harness_dir,
            actor_id=fix.actor_id,
            resume_campaign_id=running_cid,
        )

    # 4. Nonexistent campaign refuses
    with pytest.raises(RuntimeError, match="not on work item"):
        run_research_campaign(
            fix.client,
            fix.store,
            fix.key,
            definition,
            harness_dir=fix.harness_dir,
            actor_id=fix.actor_id,
            resume_campaign_id=uuid4(),
        )

    # 5. Non-explicit search definition refuses
    fixed_def = _fixed_definition(fix, (5,))
    with pytest.raises(RuntimeError, match="explicit search"):
        run_research_campaign(
            fix.client,
            fix.store,
            fix.key,
            fixed_def,
            harness_dir=fix.harness_dir,
            actor_id=fix.actor_id,
            resume_campaign_id=uuid4(),
        )


def test_cli_resume_passes_valid_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _, _, _ = _layout(tmp_path)
    resume_id = uuid4()
    seen: dict[str, object] = {}

    def spy_run(
        client, store, key, definition_obj, *, harness_dir: Path, actor_id: UUID, resume_campaign_id: UUID | None = None,
    ) -> dict[str, object]:
        seen["resume_campaign_id"] = resume_campaign_id
        return _campaign_result("supported")

    with _spies(monkeypatch):
        monkeypatch.setattr(research_campaign, "run_research_campaign", spy_run)
        code = main([
            "research",
            "run",
            "--key",
            "OMP-1",
            "--definition",
            str(definition),
            "--client-config",
            str(config),
            "--resume",
            str(resume_id),
        ])
    captured = capsys.readouterr()
    assert code == 0
    assert seen["resume_campaign_id"] == resume_id
    assert "concluded" in captured.out


def test_cli_resume_invalid_uuid_exits_2_before_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _, _, _ = _layout(tmp_path)
    with _spies(monkeypatch) as calls:
        code = main([
            "research",
            "run",
            "--key",
            "OMP-1",
            "--definition",
            str(definition),
            "--client-config",
            str(config),
            "--resume",
            "not-a-valid-uuid",
        ])
    captured = capsys.readouterr()
    assert code == 2
    assert "invalid resume UUID" in captured.err
    _assert_unbuilt(calls, captured.out)


def test_cli_no_resume_flag_preserves_old_call_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config, definition, _, _, _ = _layout(tmp_path)
    called = []

    def old_spy_run(
        client, store, key, definition_obj, *, harness_dir: Path, actor_id: UUID,
    ) -> dict[str, object]:
        called.append(True)
        return _campaign_result("supported")

    with _spies(monkeypatch):
        monkeypatch.setattr(research_campaign, "run_research_campaign", old_spy_run)
        code = main([
            "research",
            "run",
            "--key",
            "OMP-1",
            "--definition",
            str(definition),
            "--client-config",
            str(config),
        ])
    captured = capsys.readouterr()
    assert code == 0
    assert called == [True]
    assert "concluded" in captured.out
