"""Trial completion is read only from custody-checked evaluator receipts."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from omp_work.jobs.campaign_completion import read_trial_completion
from omp_work.jobs.campaign_definition import load_definition
from test_research_campaign_runner import _definition, _run, campaign  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _read(fix, metric="score"):
    view = fix.client.research(fix.key)
    trial = view.trials[0]
    return read_trial_completion(
        fix.client, fix.store, fix.key, trial.campaign_id, trial, fix.actor_id, metric,
    )


def _unverified(receipt):
    receipt["evidence"]["trust"] = "unverified"


def _unbound(receipt):
    receipt["evidence"]["content_digest"] = "0" * 64


def _wrong_issuer(receipt):
    receipt["issuer_component_sha256"] = "1" * 64


@pytest.mark.parametrize("change", [_wrong_issuer, _unverified, _unbound])
def test_unbound_or_untrusted_receipt_is_refused(campaign, monkeypatch, change) -> None:
    # The store seals only registered, verified receipts, so a real sealed
    # settlement is altered at the read seam to prove the reader re-checks it.
    _run(campaign, _definition(campaign, (1,)))
    assert _read(campaign).value == 1.0
    original = campaign.store.job_view

    def job_view(cur, workspace_id, job_id):
        job = original(cur, workspace_id, job_id)
        change(job["settlement"]["receipts"][0])
        return job

    monkeypatch.setattr(campaign.store, "job_view", job_view)
    with pytest.raises(RuntimeError, match="not bound to its observation"):
        _read(campaign)


def _missing_receipts(settlement):
    settlement["receipts"] = []


def _duplicate_receipts(settlement):
    settlement["receipts"] = settlement["receipts"] * 2


@pytest.mark.parametrize("change", [_missing_receipts, _duplicate_receipts])
def test_missing_and_duplicate_receipts_are_refused(campaign, monkeypatch, change) -> None:
    _run(campaign, _definition(campaign, (1,)))
    assert _read(campaign).value == 1.0
    original = campaign.store.job_view

    def job_view(cur, workspace_id, job_id):
        job = original(cur, workspace_id, job_id)
        change(job["settlement"])
        return job

    monkeypatch.setattr(campaign.store, "job_view", job_view)
    with pytest.raises(RuntimeError, match="no unique evaluator receipt"):
        _read(campaign)


@pytest.mark.parametrize("mode", ["absent", "candidate_authored"])
def test_observation_must_be_the_trusted_issuers(campaign, monkeypatch, mode) -> None:
    _run(campaign, _definition(campaign, (1,)))
    assert _read(campaign).value == 1.0
    original = campaign.client.research

    def read(key):
        view = original(key)
        if mode == "absent":
            return view.model_copy(update={"observations": ()})
        return view.model_copy(update={"observations": tuple(
            o.model_copy(update={"issuer_kind": "candidate_authored"})
            for o in view.observations
        )})

    monkeypatch.setattr(campaign.client, "research", read)
    with pytest.raises(RuntimeError, match="matching persisted observation"):
        _read(campaign)


def test_candidate_authored_same_source_ref_ignored_when_trusted_issuer_present(
    campaign, monkeypatch,
) -> None:
    _run(campaign, _definition(campaign, (1,)))
    original = campaign.client.research

    def read(key):
        view = original(key)
        candidate_obs = view.observations[0].model_copy(
            update={"issuer_kind": "candidate_authored"},
        )
        return view.model_copy(
            update={"observations": (view.observations[0], candidate_obs)},
        )

    monkeypatch.setattr(campaign.client, "research", read)
    assert _read(campaign).value == 1.0


def test_metrics_come_from_receipt_not_observation_payload(campaign, monkeypatch) -> None:
    _run(campaign, _definition(campaign, (1,)))
    original = campaign.client.research

    def read(key):
        view = original(key)
        return view.model_copy(update={"observations": tuple(
            o.model_copy(update={"payload": {"metrics": {"score": 99.0}}})
            for o in view.observations
        )})

    monkeypatch.setattr(campaign.client, "research", read)
    completion = _read(campaign)
    assert completion.metrics == {"score": 1.0}
    assert completion.value == 1.0


def test_failed_execution_has_no_selection_value_despite_passing_metrics(campaign) -> None:
    definition = _definition(campaign, (1,))
    definition.trials[0].harness.write_text('echo "METRIC score=1"\nexit 1\n')
    _run(campaign, load_definition(campaign.root / "definition.json"))
    completion = _read(campaign)
    assert completion.execution_status == "crashed"
    assert completion.metrics == {"score": 1.0}
    assert completion.value is None
    assert "crashed" in completion.infra_error


@pytest.mark.parametrize(
    ("metric_name", "metric_value"),
    [
        ("missing", None),
        ("bool_metric", True),
        ("nan_metric", float("nan")),
        ("inf_metric", float("inf")),
    ],
)
def test_invalid_or_missing_selection_metric_has_no_value(
    campaign, monkeypatch, metric_name, metric_value,
) -> None:
    _run(campaign, _definition(campaign, (1,)))
    if metric_value is not None:
        original = campaign.store.job_view

        def job_view(cur, workspace_id, job_id):
            job = original(cur, workspace_id, job_id)
            job["settlement"]["receipts"][0]["evidence"]["metrics"][metric_name] = metric_value
            return job

        monkeypatch.setattr(campaign.store, "job_view", job_view)

    completion = _read(campaign, metric=metric_name)
    assert completion.execution_status == "completed"
    assert completion.value is None
    assert metric_name in completion.infra_error


def test_wrong_campaign_id_is_refused(campaign) -> None:
    _run(campaign, _definition(campaign, (1,)))
    view = campaign.client.research(campaign.key)
    trial = view.trials[0]
    with pytest.raises(RuntimeError, match="does not belong to campaign"):
        read_trial_completion(
            campaign.client, campaign.store, campaign.key,
            uuid4(), trial, campaign.actor_id, "score",
        )


def test_unsettled_job_is_refused(campaign, monkeypatch) -> None:
    _run(campaign, _definition(campaign, (1,)))
    original = campaign.store.job_view

    def job_view(cur, workspace_id, job_id):
        job = original(cur, workspace_id, job_id)
        job["status"] = "admitted"
        job["settlement"] = None
        return job

    monkeypatch.setattr(campaign.store, "job_view", job_view)
    with pytest.raises(RuntimeError, match="is unsettled"):
        _read(campaign)
