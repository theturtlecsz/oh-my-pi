"""Read-only recovery replays an adaptive campaign's trusted prefix (OMP-605).

The snapshots are the same shapes the runner persists: ``ResearchTrial`` rows
carrying an encoded decision, ``ResearchObservation`` rows, and native job
settlements. ``recover_progress`` reads them through the shared
``read_trial_completion`` reader, so a metric can only come from a verified
evaluator receipt.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from omp_work.jobs.campaign_decisions import (
    Decision,
    decision_id,
    encode,
    format_rationale,
    observations_digest,
)
from omp_work.jobs.campaign_definition import (
    CampaignDefinition,
    Criterion,
    PreparedTrial,
    SearchConfig,
)
from omp_work.jobs.campaign_recovery import (
    CampaignRecoveryError,
    recover_progress,
)
from omp_work.v1.api_models import ResearchView
from omp_work.v1.models import (
    ResearchCampaign,
    ResearchCampaignSpec,
    ResearchCompatibilityManifest,
    ResearchObservation,
    ResearchTrial,
)

METRIC = "score"
SEED = 7
_NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _sha(tag: str) -> str:
    """A 64-hex digest tagged by ``tag``; distinct tags give distinct digests."""
    return (tag.encode().hex() * 64)[:64]


class _Store:
    """A NativeJobStore stand-in whose ``job_view`` serves one snapshot."""

    def __init__(self, jobs: dict[str, dict[str, object]]) -> None:
        self.jobs = jobs
        self.reads: list[str] = []

    @contextmanager
    def transaction(self, workspace_id: UUID, actor_id: UUID):
        yield None

    def job_view(self, cur, workspace_id: UUID, job_id: str):
        self.reads.append(job_id)
        return self.jobs.get(job_id)


class _Client:
    def __init__(self, view: ResearchView) -> None:
        self.view = view

    def research(self, key: str) -> ResearchView:
        return self.view


def _spec() -> ResearchCampaignSpec:
    return ResearchCampaignSpec(
        objective="recover an adaptive campaign",
        evaluation_protocol_id="research-run",
        evaluation_protocol_sha256=_sha("e"),
        resource_policy_ref="res-policy-standard",
        candidate_mapping_policy="strict_hash_match",
    )


def _definition(digests: dict[str, str]) -> CampaignDefinition:
    trials = tuple(
        PreparedTrial(
            harness=Path("/dev/null"),
            harness_bytes=b"",
            candidate_digest=digest,
            id=step_id,
            parent=parent,
            cost=cost,
        )
        for step_id, parent, cost, digest in (
            ("a", None, 5, digests["a"]),
            ("b", "a", 2, digests["b"]),
            ("c", "a", 3, digests["c"]),
        )
    )
    return CampaignDefinition(
        objective="recover an adaptive campaign",
        domain="machine_learning",
        timeout_seconds=60,
        criteria=(Criterion(metric=METRIC, op="gte", threshold=1),),
        trials=trials,
        search=SearchConfig(
            policy="greedy", budget=30, width=1, seed=SEED, metric=METRIC
        ),
    )


def _decision(index: int, step_id: str, parent: str | None, pairs) -> Decision:
    return Decision(
        index=index,
        policy="greedy",
        step_id=step_id,
        observations_count=index,
        observations_sha256=observations_digest(pairs),
        rationale=format_rationale(step_id, parent=parent),
    )


def _observation(
    campaign_id: UUID, trial_id: UUID, status: str, payload_sha256: str
) -> ResearchObservation:
    return ResearchObservation(
        observation_id=uuid5(NAMESPACE_URL, f"obs:{trial_id}"),
        workspace_id=uuid4(),
        campaign_id=campaign_id,
        trial_id=trial_id,
        issuer_kind="legacy_autoresearch",
        source_ref=f"job:trial:{trial_id}",
        execution_status=status,
        payload={},
        payload_sha256=payload_sha256,
        observed_at=_NOW,
        recorded_at=_NOW,
    )


def _trial(
    campaign_id: UUID,
    work_id: UUID,
    index: int,
    step_id: str,
    parent: str | None,
    decision: Decision,
    candidate_digest: str,
    *,
    evaluator: str,
    environment: str,
    decision_uuid: UUID | None = None,
    seed: int | None = SEED,
    spec_sha256: str,
    manifest_sha256: str,
    policy_sha256: str,
    reason: str | None = None,
) -> ResearchTrial:
    return ResearchTrial(
        trial_id=uuid4(),
        workspace_id=uuid4(),
        campaign_id=campaign_id,
        work_id=work_id,
        decision_id=decision_uuid or decision_id(campaign_id, index),
        candidate_digest=candidate_digest,
        experiment_spec_sha256=spec_sha256,
        evaluator_sha256=evaluator,
        environment_sha256=environment,
        input_manifest_sha256=manifest_sha256,
        seed=seed,
        policy_sha256=policy_sha256,
        state="proposed",
        proposed_at=_NOW,
        reason=encode(decision) if reason is None else reason,
    )


def _settlement(receipt: dict[str, object]) -> dict[str, object]:
    return {"outcome": "succeeded", "receipts": [receipt]}


def _receipt(
    evaluator: str, content_digest: str, metrics: dict[str, object]
) -> dict[str, object]:
    return {
        "role": "evaluator",
        "issuer_component_sha256": evaluator,
        "evidence": {
            "id": "ev",
            "kind": "receipt",
            "content_digest": content_digest,
            "trust": "verified",
            "metrics": dict(metrics),
        },
    }


def _job(status: str, receipt: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "status": status,
        "settlement": _settlement(receipt) if receipt is not None else None,
    }


def _snapshot(
    *,
    prefix: int = 2,
    metrics=(5.0, 2.0),
    crashed: frozenset[int] = frozenset(),
    pending_status="admitted",
    pending_obs=False,
):
    """Build the canonical three-step snapshot with ``prefix`` settled trials.

    Ledger order is deliberately shuffled (index 1, 0, 2) so recovery must sort
    by the decoded decision index. An index in ``crashed`` settles with a
    non-completed execution whose receipt still carries the metric, so its
    trusted pair is null.
    """
    campaign_id, work_id = uuid4(), uuid4()
    spec_sha, policy_sha, manifest_sha = _sha("s"), _sha("p"), _sha("m")
    digests = {"a": _sha("da"), "b": _sha("db"), "c": _sha("dc")}
    evaluator, environment = _sha("v"), _sha("n")
    definition = _definition(digests)
    manifest = ResearchCompatibilityManifest(
        contract_version="research-compatibility.v1",
        evaluators=(evaluator,),
        environments=(environment,),
    )
    campaign = ResearchCampaign(
        campaign_id=campaign_id,
        workspace_id=uuid4(),
        work_id=work_id,
        revision_id=uuid4(),
        domain="machine_learning",
        spec=_spec(),
        spec_sha256=spec_sha,
        policy_sha256=policy_sha,
        state="running",
        created_at=_NOW,
        admitted_at=_NOW,
        compatibility=manifest,
        compatibility_sha256=manifest_sha,
    )

    steps = (("a", None, "a"), ("b", "a", "b"), ("c", "a", "c"))
    built: dict[
        int, tuple[ResearchTrial, ResearchObservation | None, dict[str, object]]
    ] = {}
    pairs: list[tuple[str, float | None]] = []
    for index, (step_id, parent, key) in enumerate(steps):
        decision = _decision(index, step_id, parent, list(pairs))
        trial = _trial(
            campaign_id,
            work_id,
            index,
            step_id,
            parent,
            decision,
            digests[key],
            evaluator=evaluator,
            environment=environment,
            spec_sha256=spec_sha,
            manifest_sha256=manifest_sha,
            policy_sha256=policy_sha,
        )
        if index < prefix:
            is_crashed = index in crashed
            value = None if is_crashed else metrics[index]
            payload_sha = _sha(f"o{index}")
            observation = _observation(
                campaign_id,
                trial.trial_id,
                "crashed" if is_crashed else "completed",
                payload_sha,
            )
            job = _job(
                "failed" if is_crashed else "sealed",
                _receipt(evaluator, payload_sha, {METRIC: metrics[index]}),
            )
            pairs.append((str(trial.trial_id), value))
        elif index == len(steps) - 1 and not pending_obs:
            observation = None
            job = _job(pending_status)
        else:
            observation = _observation(
                campaign_id, trial.trial_id, "completed", _sha("pend")
            )
            job = _job("admitted")
        built[index] = (trial, observation, job)

    jobs = {f"trial:{trial.trial_id}": job for trial, _, job in built.values()}
    # Ledger order (1, 0, 2) is not decision order.
    order = [1, 0, 2] if prefix < 3 else [0, 1, 2]
    trials = [built[index][0] for index in order]
    observations = tuple(
        observation
        for index in order
        for observation in (built[index][1],)
        if observation is not None
    )
    view = ResearchView(
        work_id=work_id,
        campaigns=(campaign,),
        trials=tuple(trials),
        observations=observations,
    )
    return SimpleNamespace(
        campaign=campaign,
        definition=definition,
        client=_Client(view),
        store=_Store(jobs),
        built=built,
        digests=digests,
        evaluator=evaluator,
        environment=environment,
        manifest_sha=manifest_sha,
    )


def _recover(snap):
    return recover_progress(
        snap.client,
        snap.store,
        "key",
        snap.campaign,
        snap.definition,
        uuid4(),
    )


def test_replay_reproduces_ordered_trusted_prefix_and_spent_including_pending() -> None:
    snap = _snapshot()
    progress = _recover(snap)

    assert progress.opened == ("a", "b", "c")
    # Every persisted proposal is charged once, including the pending last one.
    assert progress.spent == 10.0
    assert [decision.index for decision in progress.decisions] == [0, 1, 2]
    assert [decision.step_id for decision in progress.decisions] == ["a", "b", "c"]
    # Trusted pairs cover only the settled prefix; the third trial's decision
    # digest was computed from the two settled receipts (null-free).
    assert progress.pairs == (
        (str(snap.built[0][0].trial_id), 5.0),
        (str(snap.built[1][0].trial_id), 2.0),
    )
    assert [result.index for result in progress.results] == [0, 1]
    assert progress.verdicts == (("supported", "passed 1 criterion"),) * 2
    assert progress.pending is not None
    assert progress.pending.index == 2
    assert progress.pending.step_id == "c"
    assert progress.tree["a"]["metric"] == 5.0
    assert progress.tree["c"]["metric"] is None


def test_pending_proposal_is_excluded_from_results_pairs_and_verdicts() -> None:
    snap = _snapshot(prefix=2, pending_obs=True)
    progress = _recover(snap)

    assert progress.pending is not None
    assert progress.pending.trial_id == snap.built[2][0].trial_id
    assert len(progress.results) == 2
    assert len(progress.verdicts) == 2
    assert all(pair[0] != str(snap.built[2][0].trial_id) for pair in progress.pairs)
    # A pending observation alone is not a completion: no metric was charged.
    assert progress.tree["c"]["metric"] is None


def test_failed_settled_metric_is_null_in_pairs_and_carries_infra_error() -> None:
    snap = _snapshot(crashed=frozenset({1}))
    progress = _recover(snap)

    trial = snap.built[1][0]
    # A crashed execution refutes even when the receipt carries a passing metric,
    # so the trusted pair is null and the tree node carries the infra error.
    assert progress.pairs[1] == (str(trial.trial_id), None)
    result = progress.results[1]
    assert result.value is None
    assert result.execution_status == "crashed"
    assert "crashed" in result.infra_error
    assert result.metrics == {METRIC: 2.0}
    assert result.verdict[0] == "refuted"
    assert progress.tree["b"]["metric"] is None
    assert (
        progress.tree["b"]["infra_error"] == "execution status crashed is not completed"
    )


def test_shared_candidate_digest_under_distinct_step_ids_is_accepted() -> None:
    snap = _snapshot()
    # Point step "c" at the same digest as "b": a digest does not identify a step.
    snap.definition = _definition({**snap.digests, "c": snap.digests["b"]})
    trial = snap.built[2][0]
    snap.client.view = snap.client.view.model_copy(
        update={
            "trials": tuple(
                item.model_copy(update={"candidate_digest": snap.digests["b"]})
                if item.trial_id == trial.trial_id
                else item
                for item in snap.client.view.trials
            ),
        }
    )
    progress = _recover(snap)
    assert progress.opened == ("a", "b", "c")
    assert (
        progress.tree["b"]["candidate_digest"] == progress.tree["c"]["candidate_digest"]
    )


def test_step_is_never_identified_from_candidate_digest_alone() -> None:
    snap = _snapshot()
    trial = snap.built[2][0]
    # A candidate digest that matches step "a" is not enough to name step "c".
    snap.client.view = snap.client.view.model_copy(
        update={
            "trials": tuple(
                item.model_copy(update={"candidate_digest": snap.digests["a"]})
                if item.trial_id == trial.trial_id
                else item
                for item in snap.client.view.trials
            ),
        }
    )
    with pytest.raises(
        CampaignRecoveryError, match="candidate digest does not match step"
    ):
        _recover(snap)


def _replace_trial(snap, index: int, **updates) -> None:
    trial = snap.built[index][0]
    snap.client.view = snap.client.view.model_copy(
        update={
            "trials": tuple(
                item.model_copy(update=updates)
                if item.trial_id == trial.trial_id
                else item
                for item in snap.client.view.trials
            ),
        }
    )


def test_inconsistent_history_before_proposal_is_rejected() -> None:
    cases = {
        "reason": {"reason": encode(_decision(1, "c", "a", []))},
        "seed": {"seed": SEED + 1},
        "spec": {"experiment_spec_sha256": _sha("z")},
        "manifest": {"input_manifest_sha256": _sha("z")},
        "policy": {"policy_sha256": _sha("z")},
        "evaluator": {"evaluator_sha256": _sha("z")},
        "environment": {"environment_sha256": _sha("z")},
        "decision_id": {"decision_id": uuid4()},
    }
    for update in cases.values():
        snap = _snapshot()
        _replace_trial(snap, 1, **update)
        with pytest.raises(CampaignRecoveryError):
            _recover(snap)


def test_digest_that_does_not_match_trusted_pairs_is_rejected() -> None:
    snap = _snapshot()
    broken = _decision(1, "b", "a", [(str(snap.built[0][0].trial_id), 99.0)])
    _replace_trial(snap, 1, reason=encode(broken))
    with pytest.raises(CampaignRecoveryError, match="digest does not match"):
        _recover(snap)


def test_gap_repeat_and_unsorted_indices_are_rejected() -> None:
    snap = _snapshot()
    _replace_trial(snap, 1, reason=encode(_decision(2, "b", "a", [])))
    with pytest.raises(CampaignRecoveryError, match="contiguous and unique"):
        _recover(snap)

    snap = _snapshot()
    repeated = _decision(0, "b", "a", [])
    _replace_trial(snap, 1, reason=encode(repeated))
    with pytest.raises(CampaignRecoveryError, match="contiguous and unique"):
        _recover(snap)


def test_repeated_step_and_unknown_step_are_rejected() -> None:
    snap = _snapshot()
    # "a" was opened at index 0; a second decision may not open it again.
    repeated = _decision(1, "a", None, [(str(snap.built[0][0].trial_id), 5.0)])
    _replace_trial(snap, 1, reason=encode(repeated))
    with pytest.raises(CampaignRecoveryError, match="repeats already-opened step"):
        _recover(snap)

    snap = _snapshot()
    unknown = _decision(1, "zz", None, [(str(snap.built[0][0].trial_id), 5.0)])
    _replace_trial(snap, 1, reason=encode(unknown))
    with pytest.raises(CampaignRecoveryError, match="names unknown step"):
        _recover(snap)


def test_legacy_and_malformed_adaptive_records_are_rejected() -> None:
    snap = _snapshot()
    _replace_trial(snap, 1, reason="legacy exploratory trial")
    with pytest.raises(CampaignRecoveryError, match="no adaptive decision record"):
        _recover(snap)

    snap = _snapshot()
    _replace_trial(snap, 1, reason='{"v": 2, "index": 1}')
    with pytest.raises(CampaignRecoveryError, match="malformed adaptive record"):
        _recover(snap)


def test_unsettled_trial_before_the_last_proposal_is_rejected() -> None:
    snap = _snapshot(prefix=1)
    with pytest.raises(
        CampaignRecoveryError, match="unsettled before the last proposal"
    ):
        _recover(snap)


@pytest.mark.parametrize("corruption", ["unverified", "unbound", "wrong_issuer"])
def test_invalid_receipt_is_refused_through_the_shared_reader(corruption: str) -> None:
    snap = _snapshot()
    trial = snap.built[0][0]
    receipt = _receipt(snap.evaluator, _sha("o0"), {METRIC: 5.0})
    if corruption == "unverified":
        receipt["evidence"]["trust"] = "unverified"  # type: ignore[index]
    elif corruption == "unbound":
        receipt["evidence"]["content_digest"] = _sha("z")  # type: ignore[index]
    else:
        receipt["issuer_component_sha256"] = _sha("z")
    snap.store.jobs[f"trial:{trial.trial_id}"] = _job("sealed", receipt)
    with pytest.raises(RuntimeError):
        _recover(snap)


def test_unsorted_ledger_recovers_from_later_recorded_first() -> None:
    # The snapshot stores index 1 first; recovery must emit the sorted prefix.
    snap = _snapshot()
    assert snap.client.view.trials[0].trial_id != snap.built[0][0].trial_id
    progress = _recover(snap)
    assert [result.trial_id for result in progress.results] == [
        snap.built[0][0].trial_id,
        snap.built[1][0].trial_id,
    ]
