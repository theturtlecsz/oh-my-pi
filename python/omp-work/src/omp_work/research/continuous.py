"""M4 source proof composing the program, native jobs, facts, and accounting APIs.

The service is the acceptance harness from ``test_workflow_service``; this
procedure exercises its real command endpoint, not an installed controller.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from omp_work.jobs import JobError, enqueue_job
from omp_work.jobs.store import NativeJobStore
from omp_work.program_facts import append_program_fact, read_program_facts
from omp_work.research.accounting import (
    ResourceAccounting,
    ResourceLimits,
    audit_fault_tolerance,
    evaluate_comparison,
    inject_fault,
    intervene_policy,
    protected_confirmation,
    register_comparison,
    run_canary,
)
from omp_work.research.capacity import ResearchCapacity
from omp_work.research.program import (
    CampaignDependency,
    ProgramCoordinator,
    QuestionItem,
    QuestionQueue,
    load_snapshot,
    refresh_evidence,
    renew_standing_policy,
)
from omp_work.research.receipt import EvaluationProtocol, MetricSpec, TrialReceipt
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy, validate_policy
from test_workflow_service import _command


@dataclass(frozen=True)
class ContinuousRun:
    service: object
    store: NativeJobStore
    workspace_id: UUID
    actor_id: UUID
    work_id: str
    campaign_id: UUID
    trial_id: UUID
    revision_id: str
    evaluator_component_sha256: str
    worker_component_sha256: str
    now: datetime
    repos: list[RepositoryRecord]


def run_continuous_program(directory: str | Path, run: ContinuousRun) -> dict:
    """Execute one continuous-program acceptance run using existing authorities."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    policies = {
        policy_id: StandingPolicy(
            policy_id=policy_id,
            action_class="spend_beyond_threshold",
            money_limit_usd=Decimal("10"),
            expires_at=run.now + timedelta(seconds=3600),
            decision_id="decision-1",
        )
        for policy_id in ("policy-a", "policy-b")
    }
    for policy in policies.values():
        validate_policy(policy, run.repos)
    queue = QuestionQueue()
    for number in (1, 2):
        queue.enqueue(QuestionItem(
            question_id=f"q{number}", question="holdout", domain="machine_learning",
            priority=number, scope="holdout", created_at=run.now.isoformat(),
        ))
    campaign_id = str(run.campaign_id)
    coordinator = ProgramCoordinator(
        "continuous-program", policies, "policy-a", queue,
        dependencies=(CampaignDependency("campaign-b", (campaign_id,)),),
        repos=run.repos,
    )
    capacity = ResearchCapacity(run.store, run.workspace_id, run.actor_id, engineering_headroom=1)
    for worker_id, capability in (("worker-cpu", "compute.cpu"), ("worker-gpu", "compute.gpu")):
        capacity.register(str(uuid4()), worker_id, run.worker_component_sha256, [capability], 2)
    accounting = ResourceAccounting(
        ResourceLimits(3600, 100, Decimal("5")), store=run.store,
        workspace_id=run.workspace_id, actor_id=run.actor_id,
    )
    action = ActionRequest(action_class="spend_beyond_threshold", amount_usd=Decimal("1"))
    coordinator.admit_campaign(campaign_id, "machine_learning", run.now, action, gate=accounting)
    # Names are bound before each try. The handler assigns the refusal, and the
    # else clause raises, but pylint does not treat that raise as definite.
    b_refused = None
    try:
        coordinator.admit_campaign("campaign-b", "machine_learning", run.now, action, gate=accounting)
    except ValueError as error:
        b_refused = str(error)
    else:
        raise AssertionError("dependent campaign admitted before predecessor completed")

    cpu = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
    gpu = {"cpu": 0, "memory_mib": 0, "gpu": 1, "model_calls": 0}
    for job_id, capability, resources in (
        ("research-cpu", "compute.cpu", cpu), ("research-gpu", "compute.gpu", gpu),
    ):
        capacity.enqueue(
            str(uuid4()), job_id, UUID(run.work_id), [capability], resources, 60,
            trial_id=run.trial_id,
        )
    coordinator.attach_job(campaign_id, "research-cpu")
    enqueue_job(
        run.store, operation_id=str(uuid4()), workspace_id=run.workspace_id,
        actor_id=run.actor_id, job_id="engineering-1", work_id=UUID(run.work_id),
        kind="compute", required_capabilities=["compute.cpu"], resources=cpu,
        lease_seconds=60, parent_job_id=None, trial_id=None,
    )
    claims = {}
    for worker_id in ("worker-gpu", "worker-cpu", "worker-cpu"):
        job = capacity.claim(str(uuid4()), worker_id)["job"]
        claims[job["job_id"]] = job
    usage_event = accounting.record(
        operation_id=str(uuid4()), work_id=run.work_id, job_id="research-cpu",
        request_id="usage-1", wall_clock_seconds=1, tokens=10, usd=Decimal("1"),
    )
    usage = accounting.total_usage()
    coordinator.set_evidence(refresh_evidence(
        {"claims": [{"claim_id": "c1", "status": "unresolved", "source_ids": [], "passages": []}]},
        [{"source_id": "src-1", "text": "accuracy 0.91", "supports": ["c1"], "contradicts": []}],
    ))
    coordinator.save_snapshot(directory)
    append_program_fact(directory / "facts.jsonl", {
        "schema": "cpk5/v1", "sourceIdentity": "research-program",
        "sourceVersion": "research-program.v1", "sequence": 1,
        "factType": "research.campaign", "timestamp": run.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload": {
            "campaignId": campaign_id, "status": "admitted", "objective": "holdout",
            "domain": "machine_learning", "revisionId": run.revision_id,
            "activeTrials": 1, "completedTrials": 0,
        },
    })
    sound = TrialReceipt(
        trial_id=str(run.trial_id), campaign_id=campaign_id, job_id="research-gpu",
        candidate_digest="a" * 64, evaluator_component_sha256="b" * 64,
        evaluator_artifact_sha256="c" * 64, protocol_sha256="d" * 64,
        negative_control="rejected", verdict="qualified", metrics={"score": 1.0},
    )
    receipts = directory / "receipts"
    receipts.mkdir(exist_ok=True)
    (receipts / f"{run.trial_id}.json").write_bytes(sound.receipt_bytes())
    gpu_claim = claims["research-gpu"]
    settled = capacity.settle(
        str(uuid4()), "research-gpu", gpu_claim["worker_id"], gpu_claim["fence"],
        "succeeded", sound, run.evaluator_component_sha256,
    )
    status, body = _command(run.service, run.workspace_id, {
        "type": "cancel_research_campaign",
        "payload": {"campaign_id": campaign_id, "work_id": run.work_id, "reason": "stop"},
    })
    if status != 200:
        raise AssertionError(body)
    for job_id in coordinator.cancel_campaign(campaign_id, "stop"):
        capacity.cancel(str(uuid4()), job_id, "stop")
    cpu_claim = claims["research-cpu"]
    late_settle_code = None
    try:
        capacity.settle(
            str(uuid4()), "research-cpu", cpu_claim["worker_id"], cpu_claim["fence"],
            "succeeded", sound.model_copy(update={"job_id": "research-cpu"}),
            run.evaluator_component_sha256,
        )
    except JobError as error:
        late_settle_code = error.code
    else:
        raise AssertionError("cancelled lease accepted late settlement")
    accounting.record(
        operation_id=str(uuid4()), work_id=run.work_id, job_id="research-cpu",
        request_id="usage-2", wall_clock_seconds=0, tokens=0, usd=Decimal("10"),
    )
    quota_code = None
    try:
        coordinator.admit_campaign("campaign-c", "machine_learning", run.now, action, gate=accounting)
    except ValueError as error:
        quota_code = str(error)
    else:
        raise AssertionError("campaign admitted beyond quota")

    old = policies["policy-a"]
    renewed = renew_standing_policy(
        old, replace(old, expires_at=run.now + timedelta(seconds=7200)), run.repos, run.now,
        ChangeAuthority(requested_by_kind="client", decision_id=old.decision_id, answered_by_kind="owner"),
    )
    coordinator.replace_policy("policy-a", renewed)
    narrowed = intervene_policy(
        renewed, replace(renewed, money_limit_usd=Decimal("1")),
        ChangeAuthority(requested_by_kind="client"),
    )
    coordinator.replace_policy("policy-a", narrowed)
    protocol = EvaluationProtocol(
        metrics=(MetricSpec(name="score", direction="higher"),), primary="score",
        candidate_command=("python", "candidate.py"), evaluator_command=("python", "evaluator.py"),
        timeout_seconds=60, confirmation_sha256="ab" * 32,
    )
    negative = protected_confirmation(sound.model_copy(update={
        "confirmation_sha256": protocol.confirmation_sha256, "negative_control": "accepted",
        "protocol_sha256": hashlib.sha256(protocol.protocol_bytes()).hexdigest(),
    }), protocol)
    canary = run_canary(1.0, 1.2, 0.05, "a" * 64, "a" * 64)
    prereg = register_comparison(
        "cmp-1", protocol_sha256="d" * 64, primary="score", direction="higher", independent_n=2,
    )
    comparison = evaluate_comparison(
        prereg, sound, sound.model_copy(update={"metrics": {"score": 1.1}}), cpu, cpu,
    )
    faults = audit_fault_tolerance([inject_fault("crash"), {"ok": True}], claimed_speedup=10)
    coordinator.save_snapshot(directory)
    return {
        "campaign_id": campaign_id, "job_ids": ["research-cpu", "research-gpu"],
        "engineering_job_id": "engineering-1", "usage": usage, "usage_id": usage_event["usage_id"],
        "fact_types": [fact["factType"] for fact in read_program_facts(directory / "facts.jsonl")],
        "quota_code": quota_code, "late_settle_code": late_settle_code,
        "service_state": body["result"]["campaign"]["state"],
        "intervention_limit": str(narrowed.money_limit_usd),
        "other_policy_limit": str(coordinator.policies["policy-b"].money_limit_usd),
        "queue_depth": queue.depth(), "b_refused": b_refused,
        "negative_acceptance": negative["status"], "canary_status": canary["status"],
        "comparison_status": comparison["status"], "gpu_status": settled["job"]["status"],
        "fault_denominator": faults["denominator"], "fault_kinds": faults["failure_kinds"],
    }


def resume_continuous_program(directory: str | Path, run: ContinuousRun) -> dict:
    """Restore saved authority and evidence without creating jobs."""
    coordinator = load_snapshot(directory)
    coordinator.repos = run.repos
    return {"coordinator": coordinator, "facts": read_program_facts(Path(directory) / "facts.jsonl")}
