"""Test support and fixture utilities for recursive transfer evidence (OMP-319-s03-s02).

Imports managed-gate helpers, registers canonical task datasets, seals managed trials
across dual calls / dual arms, and populates artifact caches for replay tests.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
from psycopg.rows import dict_row

from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.lease import settle_job
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.research.managed import MANAGED_CAPABILITY, MANAGED_POLICY
from omp_work.research.receipt import (
    EvaluationProtocol,
    ReceiptExpectation,
    TrialReceipt,
    managed_job_id,
)
from omp_work.v1.canonical import canonical_json, sha256
from test_research_contract import (
    _admit_payload,
    _manifest,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_research_managed_gate import (
    _default_protocol,
    _evaluator_descriptor,
    _policy_descriptor,
    _store,
)
from test_workflow_service import OWNER, _command, _create

__all__ = [
    "SealedArm",
    "cache_artifact",
    "register_artifact_bytes",
    "register_task_dataset",
    "seal_managed_trial",
]

_DEFAULT_RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}


@dataclass(frozen=True)
class SealedArm:
    """Descriptor and credentials for a sealed managed research trial."""

    trial_id: UUID
    job_id: str
    campaign_id: UUID
    work_id: UUID
    candidate_digest: str
    protocol: EvaluationProtocol
    protocol_bytes: bytes
    receipt: TrialReceipt
    receipt_bytes: bytes
    receipt_sha256: str
    metric: float
    resources: dict[str, int]
    data_dir: Path
    dataset_id: str
    worker_id: str = ""

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __str__(self) -> str:
        return str(self.trial_id)


def register_artifact_bytes(
    native_jobs: SimpleNamespace,
    data: bytes,
    name: str,
    *,
    issuer_kind: str = "legacy_autoresearch",
) -> str:
    """Store raw artifact bytes into custody with a declared issuer_kind."""
    digest = hashlib.sha256(data).hexdigest()
    media_type = (
        "application/x-tar"
        if name.endswith(".tar")
        else ("application/json" if name.endswith(".json") else "application/octet-stream")
    )
    manifest = {
        "contract_version": "research-artifact.v1",
        "artifact_sha256": digest,
        "size_bytes": len(data),
        "media_type": media_type,
        "name": name,
        "access_class": "workspace",
        "issuer_kind": issuer_kind,
        "source_ref": f"managed/{name}",
        "valid_until": None,
    }
    status, body = _command(
        native_jobs.service,
        native_jobs.workspace_id,
        {
            "type": "register_research_artifact",
            "payload": {
                "manifest_sha256": sha256(manifest),
                "manifest": manifest,
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        },
    )
    assert status == 200, body
    return digest


def register_task_dataset(
    native_jobs: SimpleNamespace, task_id: str, payload: Any
) -> str:
    """Store canonical {"task_id", "payload"} into custody; return sha256(bytes)."""
    dataset_obj = {"task_id": task_id, "payload": payload}
    raw_bytes = canonical_json(dataset_obj).encode("utf-8")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    register_artifact_bytes(
        native_jobs,
        raw_bytes,
        f"dataset-{task_id}.json",
        issuer_kind="legacy_autoresearch",
    )
    return digest


def cache_artifact(
    native_jobs: SimpleNamespace, artifact_sha256: str, cache_key: str
) -> None:
    """Write an entry directly into omp_research.artifact_cache."""
    status, body = _command(
        native_jobs.service,
        native_jobs.workspace_id,
        {
            "type": "record_research_cache",
            "payload": {
                "cache_key": cache_key,
                "artifact_sha256": artifact_sha256,
            },
        },
    )
    assert status == 200, body


def seal_managed_trial(
    native_jobs: SimpleNamespace,
    *,
    source_bytes: bytes,
    metric: float | dict[str, float],
    resources: dict[str, int] | None = None,
    protocol: EvaluationProtocol,
    dataset_id: str,
    negative_control: str = "rejected",
    issuer_kind: str = "legacy_autoresearch",
    trust: str = "trusted",
    outcome: str = "succeeded",
    settle: bool = True,
    campaign_id: UUID | None = None,
    work_id: UUID | None = None,
) -> SealedArm:
    """Tar research/strategies/recursive.py, register custody, settle job, and record observation.

    Callable multiple times for separate arms (e.g. baseline and candidate).
    """
    service = native_jobs.service
    ws_id = native_jobs.workspace_id
    work_uuid = work_id or UUID(str(native_jobs.item["work_id"]))
    revision_id = native_jobs.item["revision_id"]
    trial_res = dict(_DEFAULT_RESOURCES)
    if resources:
        trial_res.update(resources)

    # 1. Package source into tar archive at research/strategies/recursive.py
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        tarinfo = tarfile.TarInfo(name="research/strategies/recursive.py")
        tarinfo.size = len(source_bytes)
        tarinfo.mtime = 0
        tar.addfile(tarinfo, io.BytesIO(source_bytes))
    tar_bytes = tar_buf.getvalue()
    candidate_digest = hashlib.sha256(tar_bytes).hexdigest()
    register_artifact_bytes(
        native_jobs, tar_bytes, f"candidate-{candidate_digest}.tar", issuer_kind=issuer_kind
    )

    # 2. Register protocol artifact
    protocol_bytes = protocol.protocol_bytes()
    protocol_sha = hashlib.sha256(protocol_bytes).hexdigest()
    register_artifact_bytes(
        native_jobs, protocol_bytes, f"protocol-{protocol_sha}.json", issuer_kind=issuer_kind
    )

    # 3. Register evaluator component
    eval_desc, evaluator_sha = _evaluator_descriptor()
    evaluator_artifact = str(eval_desc["artifact_sha256"])
    status, body = _command(
        service,
        ws_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": evaluator_sha,
                "descriptor": eval_desc,
            },
        },
    )
    assert status == 200, body

    # 4. Register worker component holding MANAGED_CAPABILITY
    worker_comp = _register_component(
        service,
        ws_id,
        "worker",
        name=f"worker-{uuid4()}",
        capabilities=(MANAGED_CAPABILITY,),
    )

    # 5. Register policy component with MANAGED_POLICY
    pol_desc, pol_sha = _policy_descriptor(sha256({"managed-policy": uuid4().hex}))
    status, body = _command(
        service,
        ws_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": pol_sha,
                "descriptor": pol_desc,
            },
        },
    )
    assert status == 200, body

    # 6. Build campaign manifest and create/admit managed campaign
    manifest, manifest_sha = _manifest(
        workers=(worker_comp,),
        evaluators=(evaluator_sha,),
        audits=(native_jobs.components["audit"],),
        releases=(native_jobs.components["release"],),
        environments=(native_jobs.components["environment"],),
    )

    if campaign_id is None:
        campaign_id = uuid4()
        spec, _ = _sample_spec()
        spec["evaluation_protocol_sha256"] = protocol_sha
        spec_sha = sha256(spec)
        status, body = _command(
            service,
            ws_id,
            {
                "type": "create_research_campaign",
                "payload": {
                    "campaign_id": str(campaign_id),
                    "work_id": str(work_uuid),
                    "revision_id": str(revision_id),
                    "domain": "machine_learning",
                    "spec": spec,
                    "spec_sha256": spec_sha,
                },
            },
        )
        assert status == 200, body
        status, body = _command(
            service,
            ws_id,
            {
                "type": "admit_research_campaign",
                "payload": _admit_payload(
                    campaign_id,
                    str(work_uuid),
                    str(revision_id),
                    spec_sha,
                    pol_sha,
                    manifest,
                    manifest_sha,
                ),
            },
        )
        assert status == 200, body

    # 7. Propose research trial
    trial_id = uuid4()
    status, body = _command(
        service,
        ws_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                str(work_uuid),
                pol_sha,
                evaluator_sha,
                native_jobs.components["environment"],
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )
    assert status == 200, body

    # 8. Build TrialReceipt and register receipt artifact
    job_id = managed_job_id(trial_id)
    metrics_dict = (
        dict(metric)
        if isinstance(metric, dict)
        else {protocol.primary: float(metric)}
    )
    primary_metric_val = float(metrics_dict[protocol.primary])
    verdict = (
        "qualified"
        if (outcome == "succeeded" and trust == "trusted")
        else "candidate_failed"
    )

    expected = ReceiptExpectation(
        trial_id=str(trial_id),
        campaign_id=str(campaign_id),
        job_id=job_id,
        candidate_digest=candidate_digest,
        evaluator_component_sha256=evaluator_sha,
        evaluator_artifact_sha256=evaluator_artifact,
    )
    receipt = TrialReceipt(
        **expected.model_dump(),
        protocol_sha256=protocol_sha,
        confirmation_sha256=protocol.confirmation_sha256,
        candidate_manifest="1" * 64,
        evaluator_manifest="2" * 64,
        output_sha256="3" * 64,
        negative_control=negative_control,
        metrics=metrics_dict,
        verdict=verdict,
    )
    receipt_bytes = receipt.receipt_bytes()
    receipt_sha = hashlib.sha256(receipt_bytes).hexdigest()
    register_artifact_bytes(
        native_jobs, receipt_bytes, f"receipt-{receipt_sha}.json", issuer_kind=issuer_kind
    )

    # 9. Enqueue job, register worker with MANAGED_CAPABILITY, and claim
    store = _store(native_jobs)
    enqueued = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=native_jobs.actor_id,
        job_id=job_id,
        work_id=work_uuid,
        kind="compute",
        required_capabilities=[MANAGED_CAPABILITY],
        resources=trial_res,
        lease_seconds=60,
        trial_id=trial_id,
    )
    assert enqueued["status"] == "applied", enqueued

    total_weight = sum(int(trial_res.get(k, 0)) for k in ("cpu", "memory_mib", "gpu", "model_calls"))
    worker_id = f"w-{uuid4()}"
    register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
        capabilities=[MANAGED_CAPABILITY],
        capacity=max(10, total_weight * 2),
        component_sha256=worker_comp,
    )
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=native_jobs.actor_id,
        worker_id=worker_id,
    )
    assert claimed["status"] == "applied", claimed
    job_info = claimed.get("job")
    assert job_info is not None
    fence = int(job_info["fence"])

    # 10. Settle job if settle=True
    if settle:
        receipts: list[dict[str, object]] = [
            {
                "role": "evaluator",
                "issuer_component_sha256": evaluator_sha,
                "evidence": {
                    "id": f"ev-{job_id}",
                    "kind": "receipt",
                    "content_digest": receipt_sha,
                    "trust": trust,
                    "metrics": receipt.metrics,
                },
            }
        ]
        settled = settle_job(
            store,
            operation_id=str(uuid4()),
            workspace_id=ws_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            worker_id=worker_id,
            fence=fence,
            outcome=outcome,
            receipts=receipts,
        )
        assert settled["status"] == "applied", settled

    # 11. Record observation
    obs_payload = {
        "receipt_sha256": receipt_sha,
        "verdict": receipt.verdict,
        "metrics": receipt.metrics,
        "exit_code": 0 if outcome == "succeeded" else 1,
        "duration_seconds": 0.25,
        "harness_sha256": evaluator_artifact,
    }
    status, body = _command(
        service,
        ws_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid5(NAMESPACE_URL, f"omp-job:{job_id}")),
                "campaign_id": str(campaign_id),
                "trial_id": str(trial_id),
                "issuer_kind": issuer_kind,
                "source_ref": f"job:{job_id}",
                "execution_status": "completed" if outcome == "succeeded" else "crashed",
                "payload": obs_payload,
                "payload_sha256": sha256(obs_payload),
                "observed_at": "2026-01-01T00:00:00+00:00",
            },
        },
    )
    assert status == 200, body

    data_dir = (
        service.config.data_dir
        if isinstance(service.config.data_dir, Path)
        else Path(str(service.config.data_dir))
    )

    return SealedArm(
        trial_id=trial_id,
        job_id=job_id,
        campaign_id=campaign_id,
        work_id=work_uuid,
        candidate_digest=candidate_digest,
        protocol=protocol,
        protocol_bytes=protocol_bytes,
        receipt=receipt,
        receipt_bytes=receipt_bytes,
        receipt_sha256=receipt_sha,
        metric=primary_metric_val,
        resources=trial_res,
        data_dir=data_dir,
        dataset_id=dataset_id,
        worker_id=worker_id,
    )
