"""Adaptive research campaign runner tests (OMP-605-s03-s02)."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_work.jobs.campaign_decisions import decode, format_rationale, observations_digest
from omp_work.jobs.campaign_definition import load_definition
from omp_work.jobs.research_trial import ResearchTrialHandler, trial_job_id
from omp_work.v1.canonical import sha256
from omp_work.v1.client import _envelope
from omp_work.v1.models import (
    RecordResearchObservationCommand,
    RecordResearchObservationPayload,
)
from test_research_campaign_runner import _job, _run, campaign  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _setup_tree_definition(fix, score_file: Path, *, budget: int = 3):
    harness_a = fix.root / "a.sh"
    harness_a.write_text('#!/bin/bash\necho "METRIC score=5"\n')

    harness_a1 = fix.root / "a1.sh"
    harness_a1.write_text(
        f'#!/bin/bash\nscore=$(cat "{score_file}")\necho "METRIC score=$score"\n'
    )

    harness_a2 = fix.root / "a2.sh"
    harness_a2.write_text('#!/bin/bash\necho "METRIC score=3"\n')

    harness_a1x = fix.root / "a1x.sh"
    harness_a1x.write_text('#!/bin/bash\necho "METRIC score=8"\n')

    def_path = fix.root / f"definition-{uuid4().hex[:8]}.json"
    def_path.write_text(json.dumps({
        "objective": "Adaptive search tree evaluation",
        "domain": "machine_learning",
        "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
        "search": {
            "policy": "greedy",
            "budget": budget,
            "width": 1,
            "seed": 0,
            "metric": "score",
        },
        "trials": [
            {"id": "a", "harness": harness_a.name},
            {"id": "a1", "parent": "a", "harness": harness_a1.name},
            {"id": "a2", "parent": "a", "harness": harness_a2.name},
            {"id": "a1x", "parent": "a1", "harness": harness_a1x.name},
        ],
    }))
    return load_definition(def_path)


def test_score_file_9_chooses_a1x_and_score_file_1_chooses_a2(campaign) -> None:
    fix = campaign
    score_file = fix.root / "score.txt"

    # Case 1: score_file = 9 -> a1 gets 9 -> greedy chooses a1x over a2
    score_file.write_text("9\n")
    definition_9 = _setup_tree_definition(fix, score_file, budget=3)
    result_9 = _run(fix, definition_9)

    assert result_9["state"] == "concluded"
    assert result_9["outcome"] == "supported"
    assert result_9["spent"] == 3.0
    assert len(result_9["trials"]) == 3
    assert [t["step_id"] for t in result_9["trials"]] == ["a", "a1", "a1x"]
    assert result_9["trials"][0]["metrics"] == {"score": 5.0}
    assert result_9["trials"][1]["metrics"] == {"score": 9.0}
    assert result_9["trials"][2]["metrics"] == {"score": 8.0}
    assert result_9["best"] == {"step_id": "a1", "metric": 9.0}

    # Verify decisions and that selection index=2 reads two receipts
    decisions_9 = result_9["decisions"]
    assert len(decisions_9) == 3

    # Decision 0 (root a)
    d0 = decisions_9[0]
    assert d0.v == 1
    assert d0.index == 0
    assert d0.policy == "greedy"
    assert d0.step_id == "a"
    assert d0.observations_count == 0
    assert d0.observations_sha256 == observations_digest([])
    assert d0.rationale == "node=a;parent=none;metric=none"

    # Decision 1 (a1)
    trial_a_id = UUID(result_9["trials"][0]["trial_id"])
    d1 = decisions_9[1]
    assert d1.v == 1
    assert d1.index == 1
    assert d1.policy == "greedy"
    assert d1.step_id == "a1"
    assert d1.observations_count == 1
    assert d1.observations_sha256 == observations_digest([(trial_a_id, 5.0)])
    assert d1.rationale == "node=a1;parent=a;metric=5.0"

    # Decision 2 (a1x - chosen because a1 metric 9 > a metric 5)
    trial_a1_id = UUID(result_9["trials"][1]["trial_id"])
    d2 = decisions_9[2]
    assert d2.v == 1
    assert d2.index == 2
    assert d2.policy == "greedy"
    assert d2.step_id == "a1x"
    assert d2.observations_count == 2
    expected_digest_2 = observations_digest([(trial_a_id, 5.0), (trial_a1_id, 9.0)])
    assert d2.observations_sha256 == expected_digest_2
    assert d2.rationale == "node=a1x;parent=a1;metric=9.0"

    # Verify persisted proposal reason matches decoded decision and exposes all codec fields
    view_9 = fix.client.research(fix.key)
    persisted_trials_9 = [t for t in view_9.trials if t.campaign_id == UUID(result_9["campaign_id"])]
    assert len(persisted_trials_9) == 3
    for p_trial, dec in zip(persisted_trials_9, decisions_9, strict=True):
        decoded = decode(p_trial.reason)
        assert decoded == dec
        raw = json.loads(p_trial.reason)
        assert set(raw.keys()) == {
            "v", "index", "policy", "step_id",
            "observations_count", "observations_sha256", "rationale",
        }

    # Case 2: score_file = 1 -> a1 gets 1 -> greedy chooses a2 over a1x
    score_file.write_text("1\n")
    definition_1 = _setup_tree_definition(fix, score_file, budget=3)
    result_1 = _run(fix, definition_1)

    assert result_1["state"] == "concluded"
    assert result_1["outcome"] == "supported"
    assert result_1["spent"] == 3.0
    assert len(result_1["trials"]) == 3
    assert [t["step_id"] for t in result_1["trials"]] == ["a", "a1", "a2"]
    assert result_1["trials"][0]["metrics"] == {"score": 5.0}
    assert result_1["trials"][1]["metrics"] == {"score": 1.0}
    assert result_1["trials"][2]["metrics"] == {"score": 3.0}
    assert result_1["best"] == {"step_id": "a", "metric": 5.0}

    d2_case1 = result_1["decisions"][2]
    assert d2_case1.v == 1
    assert d2_case1.index == 2
    assert d2_case1.policy == "greedy"
    assert d2_case1.step_id == "a2"
    assert d2_case1.observations_count == 2
    t_a_1 = UUID(result_1["trials"][0]["trial_id"])
    t_a1_1 = UUID(result_1["trials"][1]["trial_id"])
    assert d2_case1.observations_sha256 == observations_digest([(t_a_1, 5.0), (t_a1_1, 1.0)])
    assert d2_case1.rationale == "node=a2;parent=a;metric=5.0"


def test_candidate_authored_observation_ignored_by_selection_and_digest(campaign) -> None:
    fix = campaign
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    definition = _setup_tree_definition(fix, score_file, budget=2)

    # After trial a finishes execution, write a candidate_authored observation on the same source_ref with score=100
    original_run = ResearchTrialHandler.run

    def run_and_inject_candidate_authored(self, worker, job):
        settlement = original_run(self, worker, job)
        if self.run_count == 1:
            job_id = str(job["job_id"])
            trial_id = UUID(str(job["trial_id"]))
            fix.client.execute(_envelope(
                fix.workspace_id,
                RecordResearchObservationCommand(
                    type="record_research_observation",
                    payload=RecordResearchObservationPayload(
                        observation_id=uuid4(),
                        campaign_id=UUID(str(self.last_observation["campaign_id"])),
                        trial_id=trial_id,
                        issuer_kind="candidate_authored",
                        source_ref=f"job:{job_id}",
                        execution_status="completed",
                        payload={"metrics": {"score": 100.0}},
                        payload_sha256=sha256({"metrics": {"score": 100.0}}),
                        observed_at=datetime.now(UTC),
                    ),
                ),
            ))
        return settlement

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ResearchTrialHandler, "run", run_and_inject_candidate_authored)
        result = _run(fix, definition)

    # Both observations exist in the item observations
    view = fix.client.research(fix.key)
    obs_for_trial_a = [
        o for o in view.observations if str(o.trial_id) == result["trials"][0]["trial_id"]
    ]
    assert len(obs_for_trial_a) == 2
    assert {o.issuer_kind for o in obs_for_trial_a} == {"legacy_autoresearch", "candidate_authored"}

    # Next pick is still a1, not influenced by candidate_authored score 100
    assert result["trials"][0]["step_id"] == "a"
    assert result["trials"][0]["metrics"] == {"score": 5.0}
    assert result["trials"][1]["step_id"] == "a1"

    # Digest remains receipt-only (using score 5.0, not 100.0)
    t_a = UUID(result["trials"][0]["trial_id"])
    assert result["decisions"][1].observations_sha256 == observations_digest([(t_a, 5.0)])


@pytest.mark.parametrize("corrupt_field", ["issuer", "trust", "digest"])
def test_corrupt_receipt_after_settlement_prevents_next_proposal(
    campaign, corrupt_field: str, monkeypatch,
) -> None:
    fix = campaign
    score_file = fix.root / "score.txt"
    score_file.write_text("9\n")
    definition = _setup_tree_definition(fix, score_file, budget=3)

    original_job_view = fix.store.job_view

    def job_view_corrupt(cur, workspace_id, job_id):
        job = original_job_view(cur, workspace_id, job_id)
        if job and job.get("settlement") and job["settlement"].get("receipts"):
            receipt = job["settlement"]["receipts"][0]
            if corrupt_field == "issuer":
                receipt["issuer_component_sha256"] = "0" * 64
            elif corrupt_field == "trust":
                receipt["evidence"]["trust"] = "untrusted"
            elif corrupt_field == "digest":
                receipt["evidence"]["content_digest"] = "0" * 64
        return job

    monkeypatch.setattr(fix.store, "job_view", job_view_corrupt)
    with pytest.raises(RuntimeError, match="evaluator receipt is not bound to its observation"):
        _run(fix, definition)

    # Exactly 1 trial was proposed; corruption prevented the next proposal
    view = fix.client.research(fix.key)
    assert len(view.trials) == 1
    assert view.campaigns[0].state == "running"
    assert view.campaigns[0].outcome is None
