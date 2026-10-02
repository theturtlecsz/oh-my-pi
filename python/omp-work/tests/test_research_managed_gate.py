"""Managed trial ingestion and qualification checks (R06/R07, OMP-315-s05).

Trust comes from the settlement and the worker's registered capability, never
from artifact bytes. Each test verifies a concrete boundary:
- qualified, sound managed trial returns the parsed receipt and empty reasons;
- trial without a settled managed job returns missing_managed_receipt;
- worker stripped of MANAGED_CAPABILITY in SQL after settling returns untrusted_issuer;
- receipt with another confirmation_sha256 returns wrong_confirmation;
- receipt bound to another job_id returns wrong_job;
- qualified receipt on a failed settlement returns the receipt, contradictory_evidence, and no missing_managed_receipt;
- missing custody artifacts return missing_managed_receipt or missing_protocol;
- observation differences return contradictory_evidence, missing_observation, or malformed_observation;
- settlement outcome/trust mismatch with receipt verdict returns contradictory_evidence;
- managed_trials filters out non-managed campaigns and returns (trial, receipt, reasons).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.lease import settle_job
from omp_work.jobs.store import NativeJobStore, register_worker
from omp_work.research.managed import (
    is_managed_campaign,
    managed_trials,
    verify_managed_trial,
)
from omp_work.research.receipt import (
    MANAGED_CAPABILITY,
    MANAGED_POLICY,
    EvaluationProtocol,
    TrialReceipt,
    managed_job_id,
)
from omp_work.v1.canonical import sha256
from test_research_contract import (
    _admit_payload,
    _manifest,
    _receipt as _append_receipt,
    _register_component,
    _sample_spec,
    _trial_payload,
)
from test_workflow_service import OWNER, _command, _create

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

RESOURCES = {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0}
OTHER_CONFIRMATION = hashlib.sha256(b"other-confirmation").hexdigest()


def _store(fix: SimpleNamespace) -> NativeJobStore:
    return NativeJobStore(fix.service.config)


def _connect(fix: SimpleNamespace):
    return psycopg.connect(
        **fix.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _policy_descriptor(artifact: str) -> tuple[dict[str, object], str]:
    descriptor: dict[str, object] = {
        "contract_version": "research-component.v1",
        "kind": "policy",
        "name": MANAGED_POLICY,
        "version": "1",
        "artifact_sha256": artifact,
        "roles": [],
        "capabilities": [],
    }
    return descriptor, sha256(descriptor)


def _evaluator_descriptor() -> tuple[dict[str, object], str]:
    descriptor: dict[str, object] = {
        "contract_version": "research-component.v1",
        "kind": "evaluator",
        "name": f"managed-evaluator-{uuid4()}",
        "version": "1",
        "artifact_sha256": sha256({"managed-evaluator": uuid4().hex}),
        "roles": [],
        "capabilities": [],
    }
    return descriptor, sha256(descriptor)


def _default_protocol() -> EvaluationProtocol:
    return EvaluationProtocol.model_validate(
        {
            "contract_version": "research-protocol.v1",
            "metrics": [{"name": "score", "direction": "higher"}],
            "primary": "score",
            "tolerance": {},
            "candidate_command": ["python", "candidate.py"],
            "evaluator_command": ["python", "evaluator.py"],
            "outputs": ["out/score.json"],
            "requires": ["python3"],
            "confirmation_sha256": secrets.token_hex(32),
            "timeout_seconds": 60,
        }
    )


def _register_artifact_bytes(fix: SimpleNamespace, data: bytes, name: str) -> str:
    digest = hashlib.sha256(data).hexdigest()
    manifest = {
        "contract_version": "research-artifact.v1",
        "artifact_sha256": digest,
        "size_bytes": len(data),
        "media_type": "application/json",
        "name": name,
        "access_class": "workspace",
        "issuer_kind": "candidate_authored",
        "source_ref": f"managed/{name}",
        "valid_until": None,
    }
    status, body = _command(
        fix.service,
        fix.workspace_id,
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


def _record_observation(
    fix: SimpleNamespace,
    receipt_bytes: bytes,
    *,
    job_id: str | None = None,
    mode: str = "matching",
    payload_override: dict[str, object] | None = None,
) -> None:
    if mode == "absent":
        return
    job_id = job_id or managed_job_id(fix.trial_id)
    receipt = json.loads(receipt_bytes)
    if payload_override is not None:
        payload = payload_override
    elif mode == "malformed":
        payload = {
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "metrics": receipt["metrics"],
        }
    elif mode == "contradictory":
        payload = {
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "verdict": receipt["verdict"],
            "metrics": {"score": 0.0},
        }
    else:
        payload = {
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "verdict": receipt["verdict"],
            "metrics": receipt["metrics"],
            "exit_code": 0,
            "duration_seconds": 0.25,
            "harness_sha256": fix.evaluator_artifact,
        }
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "record_research_observation",
            "payload": {
                "observation_id": str(uuid5(NAMESPACE_URL, f"omp-job:{job_id}")),
                "campaign_id": str(fix.campaign_id),
                "trial_id": str(fix.trial_id),
                "issuer_kind": "legacy_autoresearch",
                "source_ref": f"job:{job_id}",
                "execution_status": "completed",
                "payload": payload,
                "payload_sha256": sha256(payload),
                "observed_at": datetime.now(UTC).isoformat(),
            },
        },
    )
    assert status == 200, body


def _settle_managed(
    fix: SimpleNamespace,
    receipt_bytes: bytes,
    *,
    outcome: str = "succeeded",
    trust: str = "trusted",
) -> dict[str, object]:
    receipt = json.loads(receipt_bytes)
    receipts: list[dict[str, object]] = [
        {
            "role": "evaluator",
            "issuer_component_sha256": fix.evaluator_sha,
            "evidence": {
                "id": f"ev-managed:{managed_job_id(fix.trial_id)}",
                "kind": "receipt",
                "content_digest": hashlib.sha256(receipt_bytes).hexdigest(),
                "trust": trust,
                "metrics": receipt["metrics"],
            },
        }
    ]
    settled = settle_job(
        _store(fix),
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        job_id=managed_job_id(fix.trial_id),
        worker_id=fix.worker_id,
        fence=fix.fence,
        outcome=outcome,
        receipts=receipts,
    )
    assert settled["status"] == "applied", settled
    return settled


def _receipt_payload(fix: SimpleNamespace, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "trial_id": str(fix.trial_id),
        "campaign_id": str(fix.campaign_id),
        "job_id": managed_job_id(fix.trial_id),
        "candidate_digest": fix.candidate_digest,
        "evaluator_component_sha256": fix.evaluator_sha,
        "evaluator_artifact_sha256": fix.evaluator_artifact,
        "protocol_sha256": hashlib.sha256(fix.protocol_bytes).hexdigest(),
        "confirmation_sha256": fix.protocol.confirmation_sha256,
        "candidate_manifest": "d" * 64,
        "evaluator_manifest": "e" * 64,
        "output_sha256": "f" * 64,
        "negative_control": "rejected",
        "metrics": {"score": 0.9},
        "verdict": "qualified",
        "failure_reason": None,
        "evidence": {"references": []},
    }
    payload.update(overrides)
    return payload


def _receipt_bytes(fix: SimpleNamespace, **overrides: object) -> bytes:
    return TrialReceipt.model_validate(_receipt_payload(fix, **overrides)).receipt_bytes()


def _verify(fix: SimpleNamespace):
    store = _store(fix)
    with store.transaction(fix.workspace_id, fix.actor_id) as cur:
        return verify_managed_trial(
            cur, fix.workspace_id, fix.trial_id, data_dir=fix.data_dir
        )


def _commit(
    fix: SimpleNamespace,
    *,
    receipt_bytes: bytes,
    register_receipt: bool = True,
    register_protocol: bool = True,
    outcome: str = "succeeded",
    trust: str = "trusted",
    observation: str = "matching",
    observation_payload: dict[str, object] | None = None,
) -> tuple[TrialReceipt | None, list[str]]:
    if register_protocol:
        _register_artifact_bytes(fix, fix.protocol_bytes, "protocol.json")
    if register_receipt:
        _register_artifact_bytes(fix, receipt_bytes, "receipt.json")
    _settle_managed(fix, receipt_bytes, outcome=outcome, trust=trust)
    if observation != "absent":
        _record_observation(
            fix,
            receipt_bytes,
            mode=observation,
            payload_override=observation_payload,
        )
    return _verify(fix)


def _make_fixture(
    native_jobs: SimpleNamespace,
    *,
    managed: bool = True,
    candidate_digest: str = "c" * 64,
    enqueue_and_claim: bool = True,
) -> SimpleNamespace:
    service = native_jobs.service
    ws_id = native_jobs.workspace_id
    item = _create(service, ws_id, f"managed-gate-{uuid4()}")
    spec, _ = _sample_spec()

    protocol = _default_protocol()
    protocol_bytes = protocol.protocol_bytes()
    spec["evaluation_protocol_sha256"] = hashlib.sha256(protocol_bytes).hexdigest()
    spec_sha = sha256(spec)

    evaluator_descriptor, evaluator_sha = _evaluator_descriptor()
    status, body = _command(
        service,
        ws_id,
        {
            "type": "register_research_component",
            "payload": {
                "component_sha256": evaluator_sha,
                "descriptor": evaluator_descriptor,
            },
        },
    )
    assert status == 200, body
    evaluator_artifact = str(evaluator_descriptor["artifact_sha256"])

    worker_component = _register_component(
        service,
        ws_id,
        "worker",
        name=f"managed-worker-{uuid4()}",
        capabilities=(MANAGED_CAPABILITY,),
    )
    manifest, manifest_sha = _manifest(
        workers=(worker_component,),
        evaluators=(evaluator_sha,),
        audits=(native_jobs.components["audit"],),
        releases=(native_jobs.components["release"],),
        environments=(native_jobs.components["environment"],),
    )
    if managed:
        policy_descriptor, policy_sha = _policy_descriptor(
            sha256({"managed-policy": uuid4().hex})
        )
        status, body = _command(
            service,
            ws_id,
            {
                "type": "register_research_component",
                "payload": {
                    "component_sha256": policy_sha,
                    "descriptor": policy_descriptor,
                },
            },
        )
        assert status == 200, body
    else:
        policy_sha = native_jobs.components["policy"]

    campaign_id = uuid4()
    status, body = _command(
        service,
        ws_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
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
                item["work_id"],
                item["revision_id"],
                spec_sha,
                policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body

    trial_id = uuid4()
    status, body = _command(
        service,
        ws_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                campaign_id,
                item["work_id"],
                policy_sha,
                evaluator_sha,
                native_jobs.components["environment"],
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )
    assert status == 200, body

    receipt = _append_receipt(
        item["work_id"],
        item["revision_id"],
        str(uuid4()),
        "plan",
        body={"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"},
        candidate_sha256=secrets.token_hex(32),
    )
    status, body = _command(
        service,
        ws_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    planned_cand_id = UUID(body["result"]["receipt"]["candidate_id"])

    final_cand_id = str(uuid4())
    status, body = _command(
        service,
        ws_id,
        {
            "type": "finalize_candidate",
            "payload": {
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "planned_candidate_id": str(planned_cand_id),
                "candidate_id": final_cand_id,
                "candidate_sha256": candidate_digest,
                "commit_sha": secrets.token_hex(20),
            },
        },
    )
    assert status == 200, body

    fix = SimpleNamespace(
        service=service,
        workspace_id=ws_id,
        actor_id=OWNER,
        data_dir=service.config.data_dir,
        item=item,
        campaign_id=campaign_id,
        trial_id=trial_id,
        candidate_digest=candidate_digest,
        final_cand_id=final_cand_id,
        components=native_jobs.components,
        worker_component=worker_component,
        evaluator_sha=evaluator_sha,
        evaluator_artifact=evaluator_artifact,
        policy_sha=policy_sha,
        protocol=protocol,
        protocol_bytes=protocol_bytes,
        worker_id=None,
        fence=None,
    )
    if not enqueue_and_claim:
        return fix

    store = _store(fix)
    enqueued = enqueue_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=OWNER,
        job_id=managed_job_id(trial_id),
        work_id=UUID(item["work_id"]),
        kind="compute",
        required_capabilities=[MANAGED_CAPABILITY],
        resources=RESOURCES,
        lease_seconds=60,
        trial_id=trial_id,
    )
    assert enqueued["status"] == "applied", enqueued

    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=OWNER,
        worker_id=worker_id,
        component_sha256=worker_component,
        capabilities=[MANAGED_CAPABILITY],
        capacity=10,
    )
    assert registered["status"] == "applied", registered

    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=ws_id,
        actor_id=OWNER,
        worker_id=worker_id,
    )
    assert claimed["status"] == "applied", claimed
    assert claimed["job"] is not None
    assert claimed["job"]["job_id"] == managed_job_id(trial_id)

    fix.worker_id = worker_id
    fix.fence = claimed["job"]["fence"]
    return fix


def test_qualified_settled_trial_returns_receipt_and_no_reasons(native_jobs) -> None:
    """A settled qualified trial with all valid bindings returns receipt and empty reasons."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(fix, receipt_bytes=_receipt_bytes(fix))

    assert reasons == []
    assert receipt is not None
    assert receipt.verdict == "qualified"


def test_no_job_is_missing_managed_receipt(native_jobs) -> None:
    """A trial without a settled managed job reports missing_managed_receipt."""
    fix = _make_fixture(native_jobs, enqueue_and_claim=False)
    receipt, reasons = _verify(fix)

    assert receipt is None
    assert reasons == ["missing_managed_receipt"]


def test_unsettled_job_is_missing_managed_receipt(native_jobs) -> None:
    """A trial with an admitted but unsettled job reports missing_managed_receipt."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _verify(fix)

    assert receipt is None
    assert reasons == ["missing_managed_receipt"]


def test_worker_capability_cleared_after_settling_is_untrusted_issuer(native_jobs) -> None:
    """A worker whose MANAGED_CAPABILITY is cleared in SQL cannot settle trust."""
    fix = _make_fixture(native_jobs)
    _commit(fix, receipt_bytes=_receipt_bytes(fix))

    with _connect(fix) as conn:
        conn.execute(
            "UPDATE omp_jobs.workers SET capabilities='[]'::jsonb WHERE workspace_id=%s AND worker_id=%s",
            (fix.workspace_id, fix.worker_id),
        )

    receipt, reasons = _verify(fix)
    assert receipt is None
    assert reasons == ["untrusted_issuer"]


def test_receipt_with_other_confirmation_is_wrong_confirmation(native_jobs) -> None:
    """A receipt bound to a different confirmation than the sealed protocol is refused."""
    fix = _make_fixture(native_jobs)
    receipt_bytes = _receipt_bytes(fix, confirmation_sha256=OTHER_CONFIRMATION)
    receipt, reasons = _commit(fix, receipt_bytes=receipt_bytes)

    assert receipt is not None
    assert reasons == ["wrong_confirmation"]


def test_receipt_bound_to_other_job_is_wrong_job(native_jobs) -> None:
    """A receipt that binds a different job id is refused rather than accepted."""
    fix = _make_fixture(native_jobs)
    receipt_bytes = _receipt_bytes(fix, job_id=managed_job_id(uuid4()))
    receipt, reasons = _commit(fix, receipt_bytes=receipt_bytes)

    assert receipt is not None
    assert reasons == ["wrong_job"]


def test_qualified_on_failed_settlement_has_receipt_and_no_missing_managed_receipt(
    native_jobs,
) -> None:
    """A qualified receipt on a failed settlement is not rejected as missing_managed_receipt."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix, receipt_bytes=_receipt_bytes(fix), outcome="failed", trust="trusted"
    )

    assert receipt is not None
    assert receipt.verdict == "qualified"
    assert "missing_managed_receipt" not in reasons
    assert reasons == ["contradictory_evidence"]


def test_missing_receipt_custody_is_missing_managed_receipt(native_jobs) -> None:
    """A settlement naming receipt bytes that were never installed is missing its receipt."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix,
        receipt_bytes=_receipt_bytes(fix),
        register_receipt=False,
    )

    assert receipt is None
    assert reasons == ["missing_managed_receipt"]


def test_missing_protocol_custody_is_missing_protocol(native_jobs) -> None:
    """A campaign whose sealed protocol bytes are absent is missing its protocol."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix, receipt_bytes=_receipt_bytes(fix), register_protocol=False
    )

    assert receipt is None
    assert reasons == ["missing_protocol"]


def test_is_managed_campaign(native_jobs) -> None:
    """Campaigns with MANAGED_POLICY policy are managed; others are not."""
    fix = _make_fixture(native_jobs, managed=True)
    non_managed_fix = _make_fixture(native_jobs, managed=False, enqueue_and_claim=False)

    store = _store(fix)
    with store.transaction(fix.workspace_id, fix.actor_id) as cur:
        assert is_managed_campaign(
            cur, fix.workspace_id, {"policy_sha256": fix.policy_sha}
        )
        assert not is_managed_campaign(
            cur,
            non_managed_fix.workspace_id,
            {"policy_sha256": non_managed_fix.policy_sha},
        )


def test_observation_metrics_differ_is_contradictory_evidence(native_jobs) -> None:
    """A legacy observation whose metrics differ from the receipt is contradictory evidence."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix, receipt_bytes=_receipt_bytes(fix), observation="contradictory"
    )

    assert receipt is not None
    assert reasons == ["contradictory_evidence"]


def test_observation_absent_is_missing_observation(native_jobs) -> None:
    """A settled receipt with no legacy observation reports missing_observation."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix, receipt_bytes=_receipt_bytes(fix), observation="absent"
    )

    assert receipt is not None
    assert reasons == ["missing_observation"]


def test_observation_payload_without_verdict_is_malformed_observation(native_jobs) -> None:
    """A legacy observation payload lacking verdict reports malformed_observation."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix, receipt_bytes=_receipt_bytes(fix), observation="malformed"
    )

    assert receipt is not None
    assert reasons == ["malformed_observation"]


def test_qualified_succeeded_trust_verified_is_contradictory_evidence(native_jobs) -> None:
    """A qualified verdict on a succeeded job with trust verified reports contradictory_evidence."""
    fix = _make_fixture(native_jobs)
    receipt, reasons = _commit(
        fix,
        receipt_bytes=_receipt_bytes(fix, verdict="qualified"),
        outcome="succeeded",
        trust="verified",
    )

    assert receipt is not None
    assert receipt.verdict == "qualified"
    assert reasons == ["contradictory_evidence"]


def test_candidate_failed_succeeded_trusted_is_contradictory_evidence(native_jobs) -> None:
    """A candidate_failed verdict on a succeeded trusted job reports contradictory_evidence."""
    fix = _make_fixture(native_jobs)
    receipt_bytes = _receipt_bytes(
        fix,
        verdict="candidate_failed",
        failure_reason="metric fell below baseline",
        metrics={"score": 0.2},
    )
    receipt, reasons = _commit(
        fix,
        receipt_bytes=receipt_bytes,
        outcome="succeeded",
        trust="trusted",
    )

    assert receipt is not None
    assert receipt.verdict == "candidate_failed"
    assert reasons == ["contradictory_evidence"]


def test_candidate_failed_failed_untrusted_with_matching_observation(native_jobs) -> None:
    """A candidate_failed verdict on a failed untrusted settlement with matching observation has empty reasons."""
    fix = _make_fixture(native_jobs)
    receipt_bytes = _receipt_bytes(
        fix,
        verdict="candidate_failed",
        failure_reason="metric fell below baseline",
        metrics={"score": 0.2},
    )
    receipt, reasons = _commit(
        fix,
        receipt_bytes=receipt_bytes,
        outcome="failed",
        trust="untrusted",
    )

    assert receipt is not None
    assert receipt.verdict == "candidate_failed"
    assert reasons == []


def test_managed_trials_returns_only_managed_campaign_trials(native_jobs) -> None:
    """managed_trials returns only trials from managed campaigns, with receipt and reasons."""
    fix = _make_fixture(native_jobs)
    receipt_bytes = _receipt_bytes(fix)
    _commit(fix, receipt_bytes=receipt_bytes)

    # Propose two more unsettled trials in the first managed campaign
    trial_2_id = UUID("33333333-3333-3333-3333-333333333333")
    trial_4_id = UUID("22222222-2222-2222-2222-222222222222")
    for t_id, digest in ((trial_2_id, "2" * 64), (trial_4_id, "4" * 64)):
        status, body = _command(
            fix.service,
            fix.workspace_id,
            {
                "type": "propose_research_trial",
                "payload": _trial_payload(
                    fix.campaign_id,
                    fix.item["work_id"],
                    fix.policy_sha,
                    fix.evaluator_sha,
                    fix.components["environment"],
                    trial_id=t_id,
                    candidate_digest=digest,
                ),
            },
        )
        assert status == 200, body

    manifest, manifest_sha = _manifest(
        workers=(fix.worker_component,),
        evaluators=(fix.evaluator_sha,),
        audits=(fix.components["audit"],),
        releases=(fix.components["release"],),
        environments=(fix.components["environment"],),
    )

    # Create a second managed campaign to prove multi-campaign inclusion and cross-campaign ordering
    managed_2_spec, managed_2_spec_sha = _sample_spec()
    managed_campaign_2_id = uuid4()
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(managed_campaign_2_id),
                "work_id": fix.item["work_id"],
                "revision_id": fix.item["revision_id"],
                "domain": "machine_learning",
                "spec": managed_2_spec,
                "spec_sha256": managed_2_spec_sha,
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                managed_campaign_2_id,
                fix.item["work_id"],
                fix.item["revision_id"],
                managed_2_spec_sha,
                fix.policy_sha,
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body

    # Propose an unsettled trial in the second managed campaign
    trial_3_id = UUID("11111111-1111-1111-1111-111111111111")
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                managed_campaign_2_id,
                fix.item["work_id"],
                fix.policy_sha,
                fix.evaluator_sha,
                fix.components["environment"],
                trial_id=trial_3_id,
                candidate_digest="3" * 64,
            ),
        },
    )
    assert status == 200, body

    # Create a non-managed campaign with a trial for the same work_id to prove exclusion
    non_managed_spec, non_managed_spec_sha = _sample_spec()
    non_managed_campaign_id = uuid4()
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(non_managed_campaign_id),
                "work_id": fix.item["work_id"],
                "revision_id": fix.item["revision_id"],
                "domain": "machine_learning",
                "spec": non_managed_spec,
                "spec_sha256": non_managed_spec_sha,
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                non_managed_campaign_id,
                fix.item["work_id"],
                fix.item["revision_id"],
                non_managed_spec_sha,
                fix.components["policy"],
                manifest,
                manifest_sha,
            ),
        },
    )
    assert status == 200, body
    status, body = _command(
        fix.service,
        fix.workspace_id,
        {
            "type": "propose_research_trial",
            "payload": _trial_payload(
                non_managed_campaign_id,
                fix.item["work_id"],
                fix.components["policy"],
                fix.evaluator_sha,
                fix.components["environment"],
                trial_id=uuid4(),
                candidate_digest="e" * 64,
            ),
        },
    )
    assert status == 200, body

    # Set timestamps: fix.trial_id is earlier; trial_2_id, trial_3_id, trial_4_id share exact proposed_at to prove trial_id tie-break
    t_early = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t_tied = datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)
    store = _store(fix)
    with store.transaction(fix.workspace_id, fix.actor_id) as cur:
        cur.execute(
            "UPDATE omp_research.trials SET proposed_at=%s WHERE trial_id=%s",
            (t_early, fix.trial_id),
        )
        cur.execute(
            "UPDATE omp_research.trials SET proposed_at=%s WHERE trial_id = ANY(%s)",
            (t_tied, [trial_2_id, trial_3_id, trial_4_id]),
        )
        results = managed_trials(
            cur, fix.workspace_id, fix.item["work_id"], data_dir=fix.data_dir
        )

    # 4 trials from the managed campaigns; non-managed campaign is excluded
    assert len(results) == 4

    # Trial 1: earliest proposed_at, qualified and trusted
    trial_1, receipt_1, reasons_1 = results[0]
    assert str(trial_1["trial_id"]) == str(fix.trial_id)
    assert str(trial_1["campaign_id"]) == str(fix.campaign_id)
    assert receipt_1 is not None
    assert receipt_1.verdict == "qualified"
    assert reasons_1 == []

    # Trials with tied proposed_at are strictly ordered by trial_id:
    # trial_3_id (1111...) < trial_4_id (2222...) < trial_2_id (3333...)
    # This also proves cross-campaign ordering: trial_3_id is from managed_campaign_2_id.
    assert str(results[1][0]["trial_id"]) == str(trial_3_id)
    assert str(results[1][0]["campaign_id"]) == str(managed_campaign_2_id)
    assert results[1][1] is None
    assert results[1][2] == ["missing_managed_receipt"]

    assert str(results[2][0]["trial_id"]) == str(trial_4_id)
    assert str(results[2][0]["campaign_id"]) == str(fix.campaign_id)
    assert results[2][1] is None
    assert results[2][2] == ["missing_managed_receipt"]

    assert str(results[3][0]["trial_id"]) == str(trial_2_id)
    assert str(results[3][0]["campaign_id"]) == str(fix.campaign_id)
    assert results[3][1] is None
    assert results[3][2] == ["missing_managed_receipt"]
