"""M4: cancellation fences research while engineering and durable evidence survive."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from native_jobs_support import native_jobs  # noqa: F401
from omp_work.jobs.store import NativeJobStore
from omp_work.research.continuous import (
    ContinuousRun,
    resume_continuous_program,
    run_continuous_program,
)
from omp_work.research.receipt import TrialReceipt
from omp_work.standing_policy import ActionRequest

pytest_plugins = ("test_workflow_service",)


def test_continuous_program_cancels_research_and_resumes_without_duplicate_jobs(tmp_path, native_jobs):
    """Service cancel, stale output, quota, and restart preserve authoritative state."""
    run = ContinuousRun(
        service=native_jobs.service,
        store=NativeJobStore(native_jobs.service.config),
        workspace_id=native_jobs.workspace_id,
        actor_id=native_jobs.actor_id,
        work_id=native_jobs.item["work_id"],
        campaign_id=native_jobs.campaign_id,
        trial_id=native_jobs.trial_id,
        revision_id=native_jobs.item["revision_id"],
        evaluator_component_sha256=native_jobs.components["evaluator"],
        worker_component_sha256=native_jobs.components["worker"],
        now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        repos=[],
    )
    directory = tmp_path / "continuous"
    result = run_continuous_program(directory, run)
    assert result["campaign_id"] == str(run.campaign_id)
    assert result["job_ids"] == ["research-cpu", "research-gpu"]
    assert result["service_state"] == "cancelled"
    assert result["late_settle_code"] == "job_fence_stale"
    assert result["gpu_status"] == "sealed"
    assert result["quota_code"] == "quota_exceeded"
    assert result["usage"] == {
        "compute_seconds": 1.0, "tokens": 10, "usd": "1", "quota_exceeded": False,
    }
    with run.store.transaction(run.workspace_id, run.actor_id) as cur:
        cur.execute(
            "SELECT job_id, status, worker_id FROM omp_jobs.jobs WHERE workspace_id = %s",
            (run.workspace_id,),
        )
        jobs = {row["job_id"]: row for row in cur.fetchall()}
        cur.execute(
            "SELECT input_tokens, price_usd FROM omp_jobs.usage_events WHERE usage_id = %s",
            (result["usage_id"],),
        )
        usage = cur.fetchone()
        cur.execute(
            "SELECT sum(price_usd) AS spent FROM omp_jobs.usage_events WHERE workspace_id = %s",
            (run.workspace_id,),
        )
        assert cur.fetchone()["spent"] == Decimal("11")
        cur.execute("SELECT count(*) AS n FROM omp_jobs.jobs WHERE workspace_id = %s", (run.workspace_id,))
        count_before = cur.fetchone()["n"]
    assert jobs["research-cpu"]["status"] == "cancelled"
    assert jobs["research-gpu"]["status"] == "sealed"
    assert jobs[result["engineering_job_id"]]["status"] == "admitted"
    assert jobs[result["engineering_job_id"]]["worker_id"] == "worker-cpu"
    assert usage["input_tokens"] == 10
    assert usage["price_usd"] == Decimal("1")

    snapshot = json.loads((directory / "program.json").read_text())
    assert snapshot["schema"] == "research-program.v1"
    resumed = resume_continuous_program(directory, run)
    with run.store.transaction(run.workspace_id, run.actor_id) as cur:
        cur.execute("SELECT count(*) AS n FROM omp_jobs.jobs WHERE workspace_id = %s", (run.workspace_id,))
        assert cur.fetchone()["n"] == count_before
    coordinator = resumed["coordinator"]
    assert coordinator.evidence["claims"][0]["status"] == "supported"
    assert coordinator.evidence["claims"][0]["passages"] == [{"source_id": "src-1", "text": "accuracy 0.91"}]
    assert coordinator.campaigns[str(run.campaign_id)].status == "cancelled"
    assert coordinator.campaigns[str(run.campaign_id)].job_ids == ["research-cpu"]
    assert coordinator.policies["policy-a"].money_limit_usd == Decimal("1")
    assert coordinator.policies["policy-a"].expires_at == run.now + timedelta(seconds=7200)
    with pytest.raises(ValueError, match="^duplicate_campaign$"):
        coordinator.admit_campaign(
            str(run.campaign_id), "machine_learning", now=run.now,
            action=ActionRequest(action_class="spend_beyond_threshold", amount_usd=Decimal("1")),
            gate=None,
        )
    facts = resumed["facts"]
    assert len(facts) == 1
    assert facts[0]["sequence"] == 1
    assert facts[0]["schema"] == "cpk5/v1"
    assert facts[0]["factType"] == "research.campaign"
    assert facts[0]["payload"]["campaignId"] == str(run.campaign_id)
    assert facts[0]["payload"]["revisionId"] == run.revision_id
    assert facts[0]["timestamp"] == "2026-10-02T00:00:00Z"
    assert result["fact_types"] == ["research.campaign"]
    receipt_path = directory / "receipts" / f"{run.trial_id}.json"
    receipt = TrialReceipt.model_validate_json(receipt_path.read_bytes())
    assert receipt.job_id == "research-gpu"
    assert receipt.campaign_id == str(run.campaign_id)
    assert receipt_path.read_bytes() == receipt.receipt_bytes()
    assert sorted(path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()) == [
        "facts.jsonl", "program.json", f"receipts/{run.trial_id}.json",
    ]
    assert result["intervention_limit"] == "1"
    assert result["other_policy_limit"] == "10"
    assert coordinator.policies["policy-b"].money_limit_usd == Decimal("10")
    assert result["queue_depth"] == coordinator.queue.depth() == 2
    assert result["b_refused"] == "unmet_dependencies"
    assert result["negative_acceptance"] == "negative_acceptance"
    assert result["canary_status"] == "drift_detected"
    assert result["comparison_status"] == "compared"
    assert result["fault_denominator"] == 2
    assert result["fault_kinds"] == ["crash"]


def test_acceptance_record_distinguishes_source_delivery_from_installed_qualification():
    """Acceptance record leaves deployment qualification and child filing explicit."""
    root = Path(__file__).resolve().parents[3]
    record = (root / "docs/programme/M4-ACCEPTANCE.md").read_text()
    assert "source delivered, not installed-qualified" in record
    assert "Waiting for Chris: OMP-318-s07" in record
