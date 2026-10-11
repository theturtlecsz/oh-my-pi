"""Native execution fixtures with independently sealed recursive transfer arms."""

from __future__ import annotations

import hashlib
import io
import tarfile
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.store import register_worker
from omp_work.research.recursive import RecursionState
from omp_work.research.recursive_method import MethodContext, Observation, run_method
from omp_work.research.recursive_transfer_evidence import ConsumptionHistory, NativeEvidence
from omp_work.research.recursive_transfer_freshness import CohortTask, StepArms
from omp_work.research.recursive_transfer_methods import FrozenMethod
from omp_work.research.receipt import (
    MANAGED_CAPABILITY, EvaluationProtocol, MetricSpec, ReceiptExpectation, TrialReceipt, managed_job_id,
)
from omp_work.v1.canonical import canonical_json, sha256
from recursive_transfer_support import SealedArm, register_artifact_bytes, register_task_dataset
from test_research_contract import _admit_payload, _manifest, _register_component, _sample_spec, _trial_payload
from test_research_managed_gate import (
    _default_protocol, _evaluator_descriptor, _policy_descriptor, _receipt_bytes, _settle_managed, _store,
)
from test_research_recursive_method import inputs, tree
from test_workflow_service import _command


@dataclass
class TransferExecution:
    native_jobs: SimpleNamespace
    native: NativeEvidence
    baseline: FrozenMethod
    candidate: FrozenMethod
    baseline_context: MethodContext
    candidate_context: MethodContext
    protocol: EvaluationProtocol
    campaign_id: UUID
    policy_sha256: str
    worker_component: str
    steps: dict[str, dict[str, object]]
    opened: set[str]
    arms: dict[str, tuple[SealedArm, SealedArm]]

    def cohort(self) -> CohortTask:
        """Register a fresh holdout containing the execution tree and its arms."""
        dataset = register_task_dataset(self.native_jobs, uuid4().hex, {
            "steps": self.steps, "opened": sorted(self.opened),
        })
        return CohortTask(
            phase="confirmation", domain="machine_learning", metric=self.protocol.primary,
            direction=self.protocol.metrics[0].direction,
            protocol_sha256=self.baseline.study.protocol_sha256,
            seed=0, width=1, budget=2, dataset_id=dataset,
            steps=tuple(StepArms(step, pair[0].trial_id, pair[1].trial_id) for step, pair in self.arms.items()),
        )

    def observations(self, arm: Literal["baseline", "candidate"]) -> dict[str, Observation]:
        """Use custody-bound receipts as controller observations, including opened steps."""
        index = 0 if arm == "baseline" else 1
        return {
            step: Observation(pair[index].receipt_bytes, ReceiptExpectation.model_validate(
                pair[index].receipt.model_dump(include=set(ReceiptExpectation.model_fields))
            ))
            for step, pair in self.arms.items()
        }


def build_execution(native_jobs: SimpleNamespace, *, direction: Literal["higher", "lower"] = "higher") -> TransferExecution:
    """Generate both frozen methods with live native controller authority and accounting."""
    protocol = _default_protocol().model_copy(update={
        "metrics": (MetricSpec(name="score", direction=direction),),
    })
    protocol_sha = register_artifact_bytes(native_jobs, protocol.protocol_bytes(), "protocol.json")
    evaluator, evaluator_sha = _evaluator_descriptor()
    policy, policy_sha = _policy_descriptor(sha256({"policy": uuid4().hex}))
    for descriptor, digest in ((evaluator, evaluator_sha), (policy, policy_sha)):
        status, body = _command(native_jobs.service, native_jobs.workspace_id, {
            "type": "register_research_component",
            "payload": {"component_sha256": digest, "descriptor": descriptor},
        })
        assert status == 200, body
    worker = _register_component(native_jobs.service, native_jobs.workspace_id, "worker",
                                 name=f"transfer-worker-{uuid4()}", capabilities=(MANAGED_CAPABILITY,))
    manifest, manifest_sha = _manifest(
        workers=(worker,), evaluators=(evaluator_sha,), audits=(native_jobs.components["audit"],),
        releases=(native_jobs.components["release"],), environments=(native_jobs.components["environment"],),
    )
    campaign_id = uuid4()
    spec, _ = _sample_spec()
    spec["evaluation_protocol_sha256"] = protocol_sha
    spec_sha = sha256(spec)
    for command in (
        {"type": "create_research_campaign", "payload": {
            "campaign_id": str(campaign_id), "work_id": native_jobs.item["work_id"],
            "revision_id": native_jobs.item["revision_id"], "domain": "machine_learning",
            "spec": spec, "spec_sha256": spec_sha,
        }},
        {"type": "admit_research_campaign", "payload": _admit_payload(
            campaign_id, native_jobs.item["work_id"], native_jobs.item["revision_id"],
            spec_sha, policy_sha, manifest, manifest_sha,
        )},
    ):
        status, body = _command(native_jobs.service, native_jobs.workspace_id, command)
        assert status == 200, body

    now = datetime.now(timezone.utc)
    start, end = now - timedelta(minutes=1), now + timedelta(hours=1)
    methods: list[FrozenMethod] = []
    contexts: list[MethodContext] = []
    for name, threshold in (("baseline", 0.8), ("candidate", 0.5)):
        study, recipe, method_spec, dev, context, _ = inputs(threshold)
        grant = context.grant.model_copy(update={"window_start": start, "window_end": end})
        study = study.model_copy(update={
            "study_id": name, "window_start": start, "window_end": end,
            "evaluator_component_sha256": evaluator_sha,
            "evaluator_artifact_sha256": evaluator["artifact_sha256"], "protocol_sha256": protocol_sha,
        })
        context = replace(context, state=RecursionState(grant=grant), grant=grant,
                          policy=replace(context.policy, expires_at=end), store=_store(native_jobs),
                          workspace_id=native_jobs.workspace_id, actor_id=native_jobs.actor_id,
                          work_id=native_jobs.item["work_id"], operation_id=str(uuid4()), request_id=str(uuid4()),
                          now=lambda: datetime.now(timezone.utc))
        result = run_method(study, recipe, method_spec, dev, context=context)
        methods.append(FrozenMethod(result, study))
        # Generation already committed its operation; proposals need a new one.
        contexts.append(replace(context, state=result.state, operation_id=str(uuid4())))
    ctx = contexts[1]
    native = NativeEvidence(
        ctx.store, ctx.workspace_id, ctx.actor_id, ctx.work_id, native_jobs.service.config.data_dir,
        grant=ctx.grant, policy=ctx.policy, authority=ctx.authority, action=ctx.action,
        repositories=ctx.repositories, operation_id=ctx.operation_id, request_id=ctx.request_id,
        history=ConsumptionHistory(),
    )
    return TransferExecution(native_jobs, native, *methods, *contexts, protocol,
                             campaign_id, policy_sha, worker, tree(), {"a"}, {})


def seal_step(
    execution: TransferExecution, step_id: str, *, baseline_score: float = 0.6,
    candidate_score: float = 0.7, baseline_resources: dict[str, int] | None = None,
    candidate_resources: dict[str, int] | None = None,
    baseline_outcome: Literal["succeeded", "failed"] = "succeeded",
    candidate_outcome: Literal["succeeded", "failed"] = "succeeded",
    baseline_source: bytes | None = None, candidate_source: bytes | None = None,
) -> tuple[SealedArm, SealedArm]:
    """Seal two real trials for an opened or selectable step under the shared work ID."""
    pair = tuple(_seal_arm(execution, frozen, score, resources, outcome, source) for frozen, score, resources, outcome, source in (
        (execution.baseline, baseline_score, baseline_resources, baseline_outcome, baseline_source),
        (execution.candidate, candidate_score, candidate_resources, candidate_outcome, candidate_source),
    ))
    baseline, candidate = pair
    execution.arms[step_id] = (baseline, candidate)
    return baseline, candidate


def _seal_arm(
    execution: TransferExecution, frozen: FrozenMethod, score: float,
    resources: dict[str, int] | None, outcome: Literal["succeeded", "failed"],
    source_override: bytes | None,
) -> SealedArm:
    fix = execution.native_jobs
    trial_id = uuid4()
    source = frozen.result.source_bytes if source_override is None else source_override
    assert source is not None
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("research/strategies/recursive.py")
        member.size = len(source)
        archive.addfile(member, io.BytesIO(source))
    candidate_digest = register_artifact_bytes(fix, buffer.getvalue(), "candidate.tar")
    status, body = _command(fix.service, fix.workspace_id, {
        "type": "propose_research_trial", "payload": _trial_payload(
            execution.campaign_id, fix.item["work_id"], execution.policy_sha256,
            frozen.study.evaluator_component_sha256, fix.components["environment"],
            trial_id=trial_id, candidate_digest=candidate_digest,
        ),
    })
    assert status == 200, body
    job_id = managed_job_id(trial_id)
    envelope = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0} | (resources or {})
    arm_fix = SimpleNamespace(**vars(fix) | {
        "trial_id": trial_id, "campaign_id": execution.campaign_id,
        "candidate_digest": candidate_digest, "protocol": execution.protocol,
        "protocol_bytes": execution.protocol.protocol_bytes(),
        "evaluator_sha": frozen.study.evaluator_component_sha256,
        "evaluator_artifact": frozen.study.evaluator_artifact_sha256,
        "worker_id": f"transfer-{uuid4()}",
    })
    output = canonical_json({"trial_id": str(trial_id), "metrics": {"score": score}, "outcome": outcome}).encode()
    output_sha = register_artifact_bytes(fix, output, "output.json")
    receipt_bytes = _receipt_bytes(
        arm_fix, metrics={"score": score}, output_sha256=output_sha,
        candidate_manifest=sha256({"research/strategies/recursive.py": hashlib.sha256(source).hexdigest()}),
        evaluator_manifest=sha256({"evaluator": arm_fix.evaluator_artifact}),
        verdict="qualified" if outcome == "succeeded" else "candidate_failed",
    )
    receipt_sha = register_artifact_bytes(fix, receipt_bytes, "receipt.json")
    store = execution.native.store
    scope = {"workspace_id": fix.workspace_id, "actor_id": fix.actor_id}
    enqueued = enqueue_job(store, operation_id=str(uuid4()), **scope, job_id=job_id,
                           work_id=execution.native.work_id, kind="compute", trial_id=trial_id,
                           required_capabilities=[MANAGED_CAPABILITY], resources=envelope, lease_seconds=60)
    assert enqueued["status"] == "applied", enqueued
    registered = register_worker(store, operation_id=str(uuid4()), **scope, worker_id=arm_fix.worker_id,
                                 component_sha256=execution.worker_component,
                                 capabilities=[MANAGED_CAPABILITY], capacity=max(10, sum(envelope.values()) * 2))
    assert registered["status"] == "applied", registered
    claimed = claim_job(store, operation_id=str(uuid4()), **scope, worker_id=arm_fix.worker_id)
    assert claimed["status"] == "applied" and claimed["job"]["job_id"] == job_id, claimed
    arm_fix.fence = claimed["job"]["fence"]
    _settle_managed(arm_fix, receipt_bytes, outcome=outcome)
    payload = {"receipt_sha256": receipt_sha, "verdict": "qualified" if outcome == "succeeded" else "candidate_failed",
               "metrics": {"score": score}, "exit_code": 0 if outcome == "succeeded" else 1,
               "duration_seconds": 0.25, "harness_sha256": arm_fix.evaluator_artifact}
    status, body = _command(fix.service, fix.workspace_id, {
        "type": "record_research_observation", "payload": {
            "observation_id": str(uuid5(NAMESPACE_URL, f"omp-job:{job_id}")),
            "campaign_id": str(execution.campaign_id), "trial_id": str(trial_id),
            "issuer_kind": "legacy_autoresearch", "source_ref": f"job:{job_id}",
            "execution_status": "completed" if outcome == "succeeded" else "crashed",
            "payload": payload, "payload_sha256": sha256(payload),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    assert status == 200, body
    return SealedArm(trial_id, job_id, execution.campaign_id, execution.native.work_id,
                     candidate_digest, execution.protocol, execution.protocol.protocol_bytes(),
                     TrialReceipt.model_validate_json(receipt_bytes), receipt_bytes, receipt_sha, score,
                     envelope, execution.native.data_dir, "", arm_fix.worker_id)
