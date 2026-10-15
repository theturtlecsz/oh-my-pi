"""Search-budget and cancellation limits for an adaptive campaign (OMP-605-s03-s06).

Real runner, search, handler, and observation writes. Search budget is trial
cost. Native cpu reservation stays in dispatch and the worker.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_work.__main__ import main
from omp_work.jobs import research_campaign
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.campaign_decisions import decode
from omp_work.jobs.campaign_definition import load_definition, score_campaign, search_steps
from omp_work.jobs.research_trial import CAPABILITY, trial_job_id
from omp_work.jobs.store import JobError
from omp_work.jobs.worker import Frozen, JobWorker
from omp_work.v1.client import _envelope
from omp_work.v1.models import (
    CancelResearchCampaignCommand,
    CancelResearchCampaignPayload,
    ResearchBlockedDependency,
    SetResearchCampaignStateCommand,
    SetResearchCampaignStatePayload,
)
from test_research_campaign_runner import _job, _run, campaign  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_PASS = 'echo "METRIC score=1"\n'
_RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}


class _Crash(Exception):
    pass


def _definition(fix, specs: list[dict[str, object]], *, policy: str = "fixed", budget: float = 10):
    trials = []
    for spec in specs:
        name = f"{spec['id']}-{uuid4().hex[:8]}.sh"
        path = fix.root / name
        path.write_text(str(spec["script"]))
        trial: dict[str, object] = {"id": spec["id"], "harness": name}
        if "cost" in spec:
            trial["cost"] = spec["cost"]
        if spec.get("parent") is not None:
            trial["parent"] = spec["parent"]
        trials.append(trial)
    def_path = fix.root / f"definition-{uuid4().hex[:8]}.json"
    def_path.write_text(json.dumps({
        "objective": "Campaign limit",
        "domain": "machine_learning",
        "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
        "search": {
            "policy": policy,
            "budget": budget,
            "width": 1,
            "seed": 0,
            "metric": "score",
        },
        "trials": trials,
    }))
    return load_definition(def_path), def_path


def _with_budget(definition, budget: float):
    search = definition.search
    assert search is not None
    return replace(definition, search=replace(search, budget=budget))


def _persisted(fix, campaign_id: str | None = None):
    view = fix.client.research(fix.key)
    if campaign_id is None:
        camp = view.campaigns[-1]
    else:
        camp = next(c for c in view.campaigns if c.campaign_id == UUID(campaign_id))
    trials = sorted(
        (trial for trial in view.trials if trial.campaign_id == camp.campaign_id),
        key=lambda trial: decode(trial.reason).index,
    )
    return camp, trials, view


def _steps(result) -> list[str]:
    return [trial["step_id"] for trial in result["trials"]]


def _watch(fix, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    seen: dict[str, object] = {"commands": [], "dispatches": [], "started": False, "stopped": False}
    execute = fix.client.execute
    dispatch = research_campaign.dispatch_trial
    start = JobWorker.start

    def counted_execute(envelope):
        if seen["stopped"]:
            seen["commands"].append(envelope.command.type)
        return execute(envelope)

    def counted_dispatch(*args, **kwargs):
        if seen["stopped"]:
            seen["dispatches"].append(kwargs["trial_id"])
        return dispatch(*args, **kwargs)

    def counted_start(self):
        if seen["stopped"]:
            seen["started"] = True
            raise AssertionError("worker started after stop")
        return start(self)

    monkeypatch.setattr(fix.client, "execute", counted_execute)
    monkeypatch.setattr(research_campaign, "dispatch_trial", counted_dispatch)
    monkeypatch.setattr(JobWorker, "start", counted_start)
    return seen


def _issue_stop(fix, camp, state: str) -> None:
    policy = camp.policy_sha256
    assert policy is not None
    if state == "cancelled":
        command = CancelResearchCampaignCommand(
            type="cancel_research_campaign",
            payload=CancelResearchCampaignPayload(
                campaign_id=camp.campaign_id,
                work_id=camp.work_id,
                reason="stop between trials",
            ),
        )
    elif state == "paused":
        command = SetResearchCampaignStateCommand(
            type="set_research_campaign_state",
            payload=SetResearchCampaignStatePayload(
                campaign_id=camp.campaign_id,
                work_id=camp.work_id,
                expected_state="running",
                target_state="paused",
                policy_sha256=policy,
            ),
        )
    else:
        command = SetResearchCampaignStateCommand(
            type="set_research_campaign_state",
            payload=SetResearchCampaignStatePayload(
                campaign_id=camp.campaign_id,
                work_id=camp.work_id,
                expected_state="running",
                target_state="blocked",
                policy_sha256=policy,
                blocked_dependency=ResearchBlockedDependency(
                    kind="budget_scope",
                    ref="search",
                    reason="held",
                ),
            ),
        )
    fix.client.execute(_envelope(fix.workspace_id, command))


def _open_reservations(fix, job_id: str) -> list[dict[str, object]]:
    with fix.store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(
            """
            SELECT reservation_id, worker_id, resources
            FROM omp_jobs.reservations
            WHERE job_id=%s AND released_at IS NULL
            ORDER BY reservation_id
            """,
            (job_id,),
        )
        return list(cur.fetchall())


def test_search_budget_exact_boundary_runs_only_the_fitting_prefix(campaign) -> None:
    fix = campaign
    specs = [
        {"id": "a", "script": _PASS, "cost": 1},
        {"id": "b", "script": _PASS, "cost": 1},
        {"id": "c", "script": _PASS, "cost": 1},
    ]
    exact, _ = _definition(fix, specs[:2], budget=2)
    filled = _run(fix, exact)
    assert _steps(filled) == ["a", "b"]
    assert filled["spent"] == 2.0
    assert filled["state"] == "concluded"
    assert (filled["outcome"], filled["reason"]) == score_campaign([
        ("supported", "passed 1 criterion"),
        ("supported", "passed 1 criterion"),
    ])

    boundary, _ = _definition(fix, specs, budget=2)
    stopped = _run(fix, boundary)
    assert _steps(stopped) == ["a", "b"]
    assert stopped["spent"] == 2.0
    assert stopped["state"] == "concluded"
    camp, trials, _ = _persisted(fix, stopped["campaign_id"])
    assert camp.state == "concluded"
    assert camp.outcome == "supported"
    assert [decode(trial.reason).step_id for trial in trials] == ["a", "b"]


def test_fractional_search_budget_uses_candidate_cost_fsum(campaign) -> None:
    fix = campaign
    costs = [0.1, 0.2, 0.1]
    specs = [
        {"id": "a", "script": _PASS, "cost": costs[0]},
        {"id": "b", "script": _PASS, "cost": costs[1]},
        {"id": "c", "script": _PASS, "cost": costs[2]},
    ]
    total = math.fsum(costs)
    full, _ = _definition(fix, specs, budget=1)
    full = _with_budget(full, total)
    ran = _run(fix, full)
    assert _steps(ran) == ["a", "b", "c"]
    assert ran["spent"] == total

    short, _ = _definition(fix, specs, budget=1)
    short = _with_budget(short, math.nextafter(total, 0.0))
    prefix = _run(fix, short)
    assert _steps(prefix) == ["a", "b"]
    assert prefix["spent"] == math.fsum(costs[:2])
    assert prefix["state"] == "concluded"
    _, trials, _ = _persisted(fix, prefix["campaign_id"])
    assert [decode(trial.reason).step_id for trial in trials] == ["a", "b"]


def test_too_expensive_first_pick_does_not_fall_back(campaign) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "dear", "script": _PASS, "cost": 5},
        {"id": "cheap", "script": _PASS, "cost": 1},
    ], budget=4)
    result = _run(fix, definition)
    assert result["trials"] == []
    assert result["best"] is None
    assert result["spent"] == 0.0
    assert result["state"] == "concluded"
    assert (result["outcome"], result["reason"]) == score_campaign([])
    camp, trials, _ = _persisted(fix, result["campaign_id"])
    assert camp.state == "concluded"
    assert camp.outcome == "supported"
    assert trials == []


def test_too_expensive_next_pick_does_not_fall_back(campaign) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "a", "script": _PASS, "cost": 3},
        {"id": "b", "script": _PASS, "cost": 3},
        {"id": "c", "script": _PASS, "cost": 1},
    ], budget=5)
    result = _run(fix, definition)
    assert _steps(result) == ["a"]
    assert result["spent"] == 3.0
    assert result["state"] == "concluded"
    assert result["outcome"] == "supported"
    assert "passed 1 trial" in result["reason"]
    _, trials, _ = _persisted(fix, result["campaign_id"])
    assert [decode(trial.reason).step_id for trial in trials] == ["a"]


def test_no_legal_pick_stops_through_score_campaign(campaign) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "a", "script": 'echo "METRIC score=5"\nexit 1\n', "cost": 1},
        {"id": "a1", "script": _PASS, "parent": "a", "cost": 1},
    ], policy="greedy", budget=10)
    result = _run(fix, definition)
    assert _steps(result) == ["a"]
    assert result["spent"] == 1.0
    assert result["spent"] < 10
    assert result["best"] is None
    assert result["state"] == "concluded"
    assert result["outcome"] == "refuted"
    camp, trials, _ = _persisted(fix, result["campaign_id"])
    assert camp.state == "concluded"
    assert camp.outcome == "refuted"
    assert [decode(trial.reason).step_id for trial in trials] == ["a"]


def test_best_excludes_failed_and_missing_metrics(campaign) -> None:
    fix = campaign
    mixed, _ = _definition(fix, [
        {"id": "crash", "script": 'echo "METRIC score=100"\nexit 1\n'},
        {"id": "blank", "script": 'echo "no metric"\n'},
        {"id": "ok", "script": 'echo "METRIC score=2"\n'},
    ], budget=10)
    result = _run(fix, mixed)
    assert _steps(result) == ["crash", "blank", "ok"]
    assert result["trials"][0]["metrics"] == {"score": 100.0}
    assert result["trials"][0]["execution_status"] == "crashed"
    assert result["trials"][1]["execution_status"] == "completed"
    assert result["trials"][1]["metrics"] == {}
    assert result["best"] == {"step_id": "ok", "metric": 2.0}
    assert result["outcome"] == "refuted"

    missing, _ = _definition(fix, [{"id": "blank", "script": 'echo "no metric"\n'}])
    missing_result = _run(fix, missing)
    assert missing_result["best"] is None
    assert missing_result["state"] == "concluded"
    assert missing_result["outcome"] == "refuted"

    failed, _ = _definition(fix, [
        {"id": "crash", "script": 'echo "METRIC score=100"\nexit 1\n'},
    ])
    failed_result = _run(fix, failed)
    assert failed_result["trials"][0]["metrics"] == {"score": 100.0}
    assert failed_result["best"] is None
    assert failed_result["outcome"] == "refuted"


def test_native_reservation_refusal_is_not_a_score(campaign, monkeypatch: pytest.MonkeyPatch) -> None:
    """Capacity already holds cpu=1, the same gate as native job admission.

    The trial still fits the search budget. The worker's reservation refusal
    is surfaced and the campaign is not scored.
    """
    fix = campaign
    definition, _ = _definition(fix, [{"id": "a", "script": _PASS, "cost": 1}], budget=10)
    held: list[str] = []
    real_start = JobWorker.start

    def start_and_hold(self):
        result = real_start(self)
        work_id = fix.client.work_item(fix.key).work_id
        hold_id = f"hold-{uuid4()}"
        enqueued = enqueue_job(
            fix.store,
            operation_id=str(uuid4()),
            workspace_id=fix.workspace_id,
            actor_id=fix.actor_id,
            job_id=hold_id,
            work_id=work_id,
            kind="compute",
            required_capabilities=[CAPABILITY],
            resources=_RESOURCES,
            lease_seconds=60,
        )
        claimed = claim_job(
            fix.store,
            operation_id=str(uuid4()),
            workspace_id=fix.workspace_id,
            actor_id=fix.actor_id,
            worker_id=self.worker_id,
        )
        assert enqueued["status"] == "applied"
        assert claimed["job"]["job_id"] == hold_id
        held.append(hold_id)
        return result

    monkeypatch.setattr(JobWorker, "start", start_and_hold)
    with pytest.raises(RuntimeError, match="did not execute target job") as caught:
        _run(fix, definition)
    assert "supported" not in str(caught.value)
    assert "refuted" not in str(caught.value)

    camp, trials, _ = _persisted(fix)
    assert camp.state == "running"
    assert camp.outcome is None
    assert len(trials) == 1
    trial_job = _job(fix, trials[0].trial_id)
    assert trial_job["status"] == "backlog"
    assert _open_reservations(fix, trial_job_id(trials[0].trial_id)) == []
    assert len(_open_reservations(fix, held[0])) == 1


def test_pending_recovery_counts_persisted_proposal_cost_once(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "a", "script": _PASS, "cost": 1},
        {"id": "b", "script": _PASS, "cost": 1},
        {"id": "c", "script": _PASS, "cost": 1},
    ], budget=2)
    real = research_campaign.dispatch_trial
    calls: list[UUID] = []

    def crash_second(*args, **kwargs):
        calls.append(kwargs["trial_id"])
        if len(calls) == 2:
            raise _Crash()
        return real(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", crash_second)
        with pytest.raises(_Crash):
            _run(fix, definition)
    before, before_trials, _ = _persisted(fix)
    assert before.state == "running"
    assert [decode(trial.reason).step_id for trial in before_trials] == ["a", "b"]
    assert _job(fix, before_trials[1].trial_id) is None

    seen: dict[str, list] = {"commands": [], "dispatches": []}
    execute = fix.client.execute

    def counted_execute(envelope):
        if envelope.command.type != "register_research_component":
            seen["commands"].append(envelope.command.type)
        return execute(envelope)

    def counted_dispatch(*args, **kwargs):
        seen["dispatches"].append(kwargs["trial_id"])
        return real(*args, **kwargs)

    monkeypatch.setattr(fix.client, "execute", counted_execute)
    monkeypatch.setattr(research_campaign, "dispatch_trial", counted_dispatch)
    result = _run(fix, definition)
    assert result["campaign_id"] == str(before.campaign_id)
    assert _steps(result) == ["a", "b"]
    assert result["spent"] == 2.0
    assert result["state"] == "concluded"
    camp, trials, _ = _persisted(fix, result["campaign_id"])
    assert [trial.trial_id for trial in trials] == [trial.trial_id for trial in before_trials]
    assert [decode(trial.reason).step_id for trial in trials] == ["a", "b"]
    assert seen["dispatches"] == [before_trials[1].trial_id]
    assert seen["commands"].count("propose_research_trial") == 0


@pytest.mark.parametrize("state", ["cancelled", "paused", "blocked"])
def test_stop_between_trials_proposes_and_dispatches_nothing_further(
    campaign, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "a", "script": _PASS, "cost": 1},
        {"id": "b", "script": _PASS, "cost": 1},
    ], budget=10)
    seen = _watch(fix, monkeypatch)
    real = research_campaign.next_step
    calls = {"n": 0}

    def stop_on_second(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            camp = next(
                row for row in fix.client.research(fix.key).campaigns if row.state == "running"
            )
            _issue_stop(fix, camp, state)
            seen["stopped"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(research_campaign, "next_step", stop_on_second)
    if state == "cancelled":
        result = _run(fix, definition)
        assert result["state"] == "cancelled"
        assert result["outcome"] is None
        assert result["cancel_reason"] == "stop between trials"
        assert _steps(result) == ["a"]
    else:
        with pytest.raises(RuntimeError, match=f"is {state}"):
            _run(fix, definition)

    camp, trials, view = _persisted(fix)
    assert camp.state == state
    assert camp.outcome is None
    assert [decode(trial.reason).step_id for trial in trials] == ["a"]
    assert len(view.observations) == 1
    assert _job(fix, trials[0].trial_id)["status"] == "sealed"
    assert seen["commands"] == []
    assert seen["dispatches"] == []
    assert seen["started"] is False


def test_cancel_after_resume_lookup_does_not_dispatch_pending(
    campaign, monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix = campaign
    definition, _ = _definition(fix, [
        {"id": "a", "script": _PASS, "cost": 1},
        {"id": "b", "script": _PASS, "cost": 1},
    ], budget=10)
    real_dispatch = research_campaign.dispatch_trial
    calls: list[UUID] = []

    def crash_second(*args, **kwargs):
        calls.append(kwargs["trial_id"])
        if len(calls) == 2:
            raise _Crash()
        return real_dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", crash_second)
        with pytest.raises(_Crash):
            _run(fix, definition)
    before, before_trials, _ = _persisted(fix)
    assert before.state == "running"
    assert len(before_trials) == 2
    assert _job(fix, before_trials[1].trial_id) is None

    seen = _watch(fix, monkeypatch)
    real_lookup = research_campaign._resumable_campaign

    def lookup_then_cancel(client, key, item, setup):
        found = real_lookup(client, key, item, setup)
        assert found is not None
        assert found.state == "running"
        _issue_stop(fix, found, "cancelled")
        seen["stopped"] = True
        return found

    monkeypatch.setattr(research_campaign, "_resumable_campaign", lookup_then_cancel)
    result = _run(fix, definition)
    assert result["state"] == "cancelled"
    assert result["outcome"] is None
    assert result["cancel_reason"] == "stop between trials"
    assert _steps(result) == ["a", "b"]
    assert [d.index for d in result["decisions"]] == [0, 1]

    camp, trials, _ = _persisted(fix)
    assert camp.campaign_id == before.campaign_id
    assert camp.state == "cancelled"
    assert camp.outcome is None
    assert [trial.trial_id for trial in trials] == [trial.trial_id for trial in before_trials]
    assert _job(fix, trials[1].trial_id) is None
    assert seen["commands"] == []
    assert seen["dispatches"] == []
    assert seen["started"] is False


def test_cli_research_run_third_pick_follows_real_a1_observation(
    campaign, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    fix = campaign
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    definition, def_path = _definition(fix, [
        {"id": "a", "script": 'echo "METRIC score=5"\n', "cost": 1},
        {
            "id": "a1",
            "script": f'score=$(cat "{score_file}")\necho "METRIC score=$score"\n',
            "parent": "a",
            "cost": 1,
        },
        {"id": "a2", "script": 'echo "METRIC score=3"\n', "parent": "a", "cost": 1},
        {"id": "a1x", "script": 'echo "METRIC score=8"\n', "parent": "a1", "cost": 1},
    ], policy="greedy", budget=3)
    fresh = search_steps(definition)
    assert [fresh[step_id]["metric"] for step_id in ("a", "a1", "a2", "a1x")] == [None, None, None, None]

    monkeypatch.setattr(fix.client, "close", lambda: None)
    monkeypatch.setattr("omp_work.__main__._research_client", lambda _path: (fix.client, fix.actor_id))
    monkeypatch.setattr("omp_work.jobs.store.NativeJobStore", lambda _ops: fix.store)

    def invoke() -> dict[str, object]:
        code = main([
            "research", "run",
            "--key", fix.key,
            "--definition", str(def_path),
            "--harness-dir", str(fix.harness_dir),
            "--client-config", str(fix.root / "client.json"),
        ])
        captured = capsys.readouterr()
        assert code == 0, captured.err
        return json.loads(captured.out)

    high = invoke()
    assert [trial["step_id"] for trial in high["trials"]] == ["a", "a1", "a1x"]
    assert high["trials"][1]["metrics"] == {"score": 9.0}
    assert high["decisions"][2]["step_id"] == "a1x"
    assert high["decisions"][2]["policy"] == "greedy"
    assert high["decisions"][2]["observations_count"] == 2

    score_file.write_text("1\n")
    again = search_steps(load_definition(def_path))
    assert [again[step_id]["metric"] for step_id in ("a", "a1", "a2", "a1x")] == [None, None, None, None]
    low = invoke()
    assert [trial["step_id"] for trial in low["trials"]] == ["a", "a1", "a2"]
    assert low["trials"][1]["metrics"] == {"score": 1.0}
    assert low["decisions"][2]["step_id"] == "a2"
    assert low["decisions"][2]["policy"] == "greedy"
    assert high["campaign_id"] != low["campaign_id"]


def test_nonuniform_costs_never_propose_or_dispatch_over_reservation(campaign, monkeypatch):
    definition, _ = _definition(campaign, [
        {"id": str(i), "script": _PASS, "cost": cost}
        for i, cost in enumerate([1, 2, 2, 1])
    ], budget=3)
    dispatch = research_campaign.dispatch_trial
    dispatched = []

    def checked_dispatch(*args, **kwargs):
        _, trials, _ = _persisted(campaign)
        costs = {trial.id: trial.cost for trial in definition.trials}
        assert math.fsum(costs[decode(t.reason).step_id] for t in trials) <= 3
        dispatched.append(kwargs["trial_id"])
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(research_campaign, "dispatch_trial", checked_dispatch)
    result = _run(campaign, definition)
    _, trials, _ = _persisted(campaign)
    assert _steps(result) == ["0", "1"]
    assert result["spent"] == 3
    assert dispatched == [t.trial_id for t in trials]


def test_cancel_after_first_settlement_stops_before_next_selection(campaign, monkeypatch):
    definition, _ = _definition(campaign, [
        {"id": str(i), "script": _PASS} for i in range(2)
    ])
    tick = JobWorker.tick
    picks = []
    select = research_campaign.next_step

    def counted_selection(*args, **kwargs):
        picks.append(1)
        return select(*args, **kwargs)

    def settle_then_cancel(self, job_id=None):
        result = tick(self, job_id=job_id)
        camp, _, _ = _persisted(campaign)
        _issue_stop(campaign, camp, "cancelled")
        return result

    monkeypatch.setattr(JobWorker, "tick", settle_then_cancel)
    monkeypatch.setattr(research_campaign, "next_step", counted_selection)
    result = _run(campaign, definition)
    camp, trials, _ = _persisted(campaign)
    assert result["state"] == camp.state == "cancelled"
    assert result["outcome"] is camp.outcome is None
    assert result["cancel_reason"] == "stop between trials"
    assert _steps(result) == ["0"]
    assert result["trials"][0]["execution_status"] == "completed"
    assert len(trials) == len(picks) == 1


def test_cancel_after_initial_running_read_keeps_third_undispatched_decision(campaign, monkeypatch):
    definition, _ = _definition(campaign, [
        {"id": str(i), "script": _PASS} for i in range(4)
    ])
    dispatch = research_campaign.dispatch_trial
    count = 0

    def crash_third(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            raise _Crash()
        return dispatch(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", crash_third)
        with pytest.raises(_Crash):
            _run(campaign, definition)
    camp, before, _ = _persisted(campaign)
    assert [decode(t.reason).index for t in before] == [0, 1, 2]
    assert _job(campaign, before[2].trial_id) is None
    state_read = research_campaign._campaign_state
    reads = 0

    def cancel_after_read(*args):
        nonlocal reads
        state = state_read(*args)
        reads += 1
        if reads == 1:
            assert state == "running"
            _issue_stop(campaign, camp, "cancelled")
        return state

    def forbidden(*args, **kwargs):
        pytest.fail("execution or selection after cancellation")

    monkeypatch.setattr(research_campaign, "_campaign_state", cancel_after_read)
    monkeypatch.setattr(research_campaign, "dispatch_trial", forbidden)
    monkeypatch.setattr(research_campaign, "next_step", forbidden)
    monkeypatch.setattr(JobWorker, "tick", forbidden)
    monkeypatch.setattr(JobWorker, "start", forbidden)
    result = _run(campaign, definition)
    assert result["state"] == "cancelled"
    assert result["outcome"] is None
    assert result["cancel_reason"] == "stop between trials"
    assert _steps(result) == ["0", "1", "2"]
    assert [d.index for d in result["decisions"]] == [0, 1, 2]
    assert result["trials"][2]["execution_status"] == "pending"
    assert [t["trial_id"] for t in result["trials"]] == [str(t.trial_id) for t in before]
    assert _job(campaign, before[2].trial_id) is None
    assert _persisted(campaign)[0].outcome is None
    with pytest.raises(RuntimeError, match="cancelled, expected running"):
        research_campaign.run_research_campaign(
            campaign.client, campaign.store, campaign.key, definition,
            harness_dir=campaign.harness_dir, actor_id=campaign.actor_id,
            resume_campaign_id=camp.campaign_id,
        )


def test_cancel_during_pending_lease_wait_stops_before_another_tick(campaign, monkeypatch):
    definition, _ = _definition(campaign, [
        {"id": str(i), "script": _PASS} for i in range(2)
    ])

    def frozen_tick(self, job_id=None):
        raise Frozen()

    # Claim an actual pending job, then leave its lease active as a crashed worker would.
    dispatch = research_campaign.dispatch_trial

    def dispatch_and_claim(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        camp, _, _ = _persisted(campaign)
        claim_job(
            campaign.store, operation_id=str(uuid4()), workspace_id=campaign.workspace_id,
            actor_id=campaign.actor_id, worker_id=f"research-campaign:{camp.campaign_id}",
        )
        return result

    with monkeypatch.context() as patch:
        patch.setattr(research_campaign, "dispatch_trial", dispatch_and_claim)
        patch.setattr(JobWorker, "tick", frozen_tick)
        with pytest.raises(Frozen):
            _run(campaign, definition)
    camp, before, _ = _persisted(campaign)
    assert _job(campaign, before[0].trial_id)["worker_id"] is not None
    tick = JobWorker.tick
    ticks = []

    def counted_tick(self, job_id=None):
        ticks.append(job_id)
        return tick(self, job_id=job_id)

    def cancel_during_wait(seconds):
        _issue_stop(campaign, camp, "cancelled")

    monkeypatch.setattr(JobWorker, "tick", counted_tick)
    monkeypatch.setattr(research_campaign.time, "sleep", cancel_during_wait)
    result = _run(campaign, definition)
    assert ticks == [trial_job_id(before[0].trial_id)]
    assert result["state"] == "cancelled"
    assert result["outcome"] is None
    assert result["cancel_reason"] == "stop between trials"
    assert _steps(result) == ["0"]
    assert [d.index for d in result["decisions"]] == [0]
    assert _persisted(campaign)[0].outcome is None
    assert len(_persisted(campaign)[1]) == 1


def test_dispatch_item_budget_refusal_propagates_without_later_proposal(campaign, monkeypatch):
    definition, _ = _definition(campaign, [
        {"id": str(i), "script": _PASS} for i in range(3)
    ])
    refusal = JobError("budget_exceeded")

    def refuse(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(research_campaign, "dispatch_trial", refuse)
    with pytest.raises(JobError) as caught:
        _run(campaign, definition)
    assert caught.value is refusal
    camp, trials, view = _persisted(campaign)
    assert camp.state == "running"
    assert camp.outcome is None
    assert [decode(t.reason).index for t in trials] == [0]
    assert view.observations == ()
    assert _job(campaign, trials[0].trial_id) is None


@pytest.mark.parametrize("boundary", ["proposal", "evaluating"])
def test_cancel_after_persisted_transition_prevents_dispatch_or_conclusion(campaign, monkeypatch, boundary):
    definition, _ = _definition(campaign, [{"id": "a", "script": _PASS}])
    execute = campaign.client.execute
    commands = []
    stopped = False

    def cancel_at_boundary(envelope):
        nonlocal stopped
        command = envelope.command
        if stopped:
            commands.append(command.type)
        result = execute(envelope)
        if (
            boundary == "proposal" and command.type == "propose_research_trial"
            or boundary == "evaluating" and command.type == "set_research_campaign_state"
            and command.payload.target_state == "evaluating"
        ):
            camp, _, _ = _persisted(campaign)
            _issue_stop(campaign, camp, "cancelled")
            stopped = True
        return result

    monkeypatch.setattr(campaign.client, "execute", cancel_at_boundary)
    result = _run(campaign, definition)
    camp, trials, _ = _persisted(campaign)
    assert stopped
    assert commands == []
    assert result["state"] == camp.state == "cancelled"
    assert result["outcome"] is camp.outcome is None
    assert result["cancel_reason"] == "stop between trials"
    assert _steps(result) == ["a"]
    assert [d.index for d in result["decisions"]] == [0]
    if boundary == "proposal":
        assert _job(campaign, trials[0].trial_id) is None
        assert result["trials"][0]["execution_status"] == "pending"
    else:
        assert _job(campaign, trials[0].trial_id)["status"] == "sealed"
        assert result["trials"][0]["execution_status"] == "completed"
