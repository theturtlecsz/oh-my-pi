"""Native job projection and settled-trial gate (R03, OMP-324-s07).

Proves trial_execution projection, eligibility reasons, and deliverable binding gating:
- no-job trial queued/ineligible with reason ["no_job"], deliverable binding unchanged;
- leased job projected as running, deliverable binding over HTTP returns 409 completion_blocked;
- succeeded lacking audit receipt -> missing_audit_receipt;
- succeeded lacking release receipt -> missing_release_receipt;
- succeeded with untrusted audit receipt -> untrusted_receipt;
- succeeded with issuer outside campaign manifest -> issuer_not_in_manifest;
- failed job -> ineligible;
- unsettled child jobs -> unsettled_jobs;
- succeeded with trusted audit + release from manifest -> eligible, binding succeeds;
- each projected status passes validate_instance("trial", ...);
- missing jobs table or trial_id column -> queued, [], ineligible, ["no_job"].
"""

from __future__ import annotations

import os
import secrets
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work.contracts.r02.validate import validate_instance
from omp_work.jobs.admission import claim_job, enqueue_job
from omp_work.jobs.lease import settle_job
from omp_work.jobs.projection import trial_execution
from omp_work.jobs.store import NativeJobStore, register_worker
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


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _connect(native_jobs):
    return psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    )


def _exec(native_jobs, trial_id: UUID | str) -> dict[str, object]:
    store = _store(native_jobs)
    with store.transaction(native_jobs.workspace_id, native_jobs.actor_id) as cur:
        return trial_execution(cur, native_jobs.workspace_id, trial_id)


def _cap() -> str:
    return "isolate.a" + uuid4().hex[:12]


def _worker(
    fix: SimpleNamespace,
    store: NativeJobStore,
) -> str:
    worker_id = f"w-{uuid4()}"
    registered = register_worker(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
        component_sha256=fix.worker_component,
        capabilities=[fix.cap],
        capacity=10,
    )
    assert registered["status"] == "applied", registered
    return worker_id


def _make_fixture(native_jobs, candidate_digest: str = "c" * 64) -> SimpleNamespace:
    """Create an isolated work item, admitted campaign, proposed trial, and candidate."""
    service = native_jobs.service
    ws_id = native_jobs.workspace_id
    item = _create(service, ws_id, f"trial-gate-{uuid4()}")
    spec, spec_sha = _sample_spec()

    cap = _cap()
    worker_component = _register_component(
        service,
        ws_id,
        "worker",
        name=f"worker-{uuid4()}",
        capabilities=(cap,),
    )
    manifest, manifest_sha = _manifest(
        workers=(worker_component,),
        audits=(native_jobs.components["audit"],),
        releases=(native_jobs.components["release"],),
        evaluators=(native_jobs.components["evaluator"],),
        environments=(native_jobs.components["environment"],),
    )

    camp_id = uuid4()
    status, body = _command(
        service,
        ws_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(camp_id),
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
                camp_id,
                item["work_id"],
                item["revision_id"],
                spec_sha,
                native_jobs.components["policy"],
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
                camp_id,
                item["work_id"],
                native_jobs.components["policy"],
                native_jobs.components["evaluator"],
                native_jobs.components["environment"],
                trial_id=trial_id,
                candidate_digest=candidate_digest,
            ),
        },
    )
    assert status == 200, body

    # Setup planned then finalized candidate matching candidate_digest
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
                "commit_sha": "a" * 40,
            },
        },
    )
    assert status == 200, body

    return SimpleNamespace(
        service=service,
        workspace_id=ws_id,
        actor_id=OWNER,
        item=item,
        campaign_id=camp_id,
        trial_id=trial_id,
        candidate_digest=candidate_digest,
        final_cand_id=final_cand_id,
        components=native_jobs.components,
        cap=cap,
        worker_component=worker_component,
        manifest=manifest,
        manifest_sha=manifest_sha,
    )


def _bind_deliverable(fix: SimpleNamespace) -> tuple[int, dict[str, object]]:
    binding_ident = {
        "candidate_digest": fix.candidate_digest,
        "campaign_id": str(fix.campaign_id),
        "native_candidate_id": str(fix.final_cand_id),
        "revision_id": str(fix.item["revision_id"]),
        "trial_id": str(fix.trial_id),
        "work_id": str(fix.item["work_id"]),
        "workspace_id": str(fix.workspace_id),
    }
    binding_sha = sha256(binding_ident)
    payload = {
        "trial_id": str(fix.trial_id),
        "campaign_id": str(fix.campaign_id),
        "work_id": str(fix.item["work_id"]),
        "revision_id": str(fix.item["revision_id"]),
        "candidate_digest": fix.candidate_digest,
        "native_candidate_id": str(fix.final_cand_id),
        "binding_sha256": binding_sha,
    }
    return _command(
        fix.service,
        fix.workspace_id,
        {"type": "bind_research_deliverable", "payload": payload},
    )


def _enqueue(
    fix: SimpleNamespace,
    store: NativeJobStore,
    **overrides: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "operation_id": str(uuid4()),
        "workspace_id": fix.workspace_id,
        "actor_id": fix.actor_id,
        "job_id": f"job-{uuid4()}",
        "work_id": UUID(fix.item["work_id"]),
        "trial_id": fix.trial_id,
        "kind": "compute",
        "required_capabilities": [fix.cap],
        "resources": {"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        "lease_seconds": 60,
    }
    body.update(overrides)
    return enqueue_job(store, **body)


def _evidence(trust: str = "trusted", **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": f"ev-{uuid4()}",
        "kind": "receipt",
        "content_digest": "abcd1234ef",
        "trust": trust,
    }
    body.update(overrides)
    return body


def _receipt(
    fix: SimpleNamespace,
    role: str = "audit",
    trust: str = "trusted",
    issuer: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "role": role,
        "issuer_component_sha256": issuer or fix.components[role],
        "evidence": _evidence(trust=trust),
    }
    body.update(overrides)
    return body


def _settle(
    fix: SimpleNamespace,
    store: NativeJobStore,
    job_id: str,
    worker_id: str,
    fence: int,
    outcome: str = "succeeded",
    receipts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    default_receipts = [
        _receipt(fix, "audit", trust="trusted"),
        _receipt(fix, "release", trust="trusted"),
    ]
    return settle_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        job_id=job_id,
        worker_id=worker_id,
        fence=fence,
        outcome=outcome,
        receipts=receipts if receipts is not None else default_receipts,
    )


def test_no_job_trial_queued_ineligible_and_binding_unchanged(native_jobs):
    """A trial with no native jobs is queued and ineligible; deliverable binding succeeds."""
    fix = _make_fixture(native_jobs)
    proj = _exec(fix, fix.trial_id)

    assert proj["trial_id"] == str(fix.trial_id)
    assert proj["status"] == "queued"
    assert proj["jobs"] == []
    assert proj["eligible"] is False
    assert proj["reasons"] == ["no_job"]

    status, body = _bind_deliverable(fix)
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["deliverable_binding"]["trial_id"] == str(fix.trial_id)


def test_leased_job_running_and_http_completion_blocked(native_jobs):
    """Leased job projects as running; deliverable binding over HTTP returns 409 completion_blocked."""
    fix = _make_fixture(native_jobs)
    store = _store(fix)
    enqueued = _enqueue(fix, store)
    assert enqueued["status"] == "applied"

    worker_id = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
    )
    assert claimed["status"] == "applied"
    assert claimed["job"] is not None

    proj = _exec(fix, fix.trial_id)
    assert proj["trial_id"] == str(fix.trial_id)
    assert proj["status"] == "running"
    assert len(proj["jobs"]) == 1
    assert proj["eligible"] is False
    assert "running" in proj["reasons"]
    assert "unsettled_jobs" in proj["reasons"]

    status, body = _bind_deliverable(fix)
    assert status == 409, body
    assert body["error"]["code"] == "completion_blocked"
    diagnostics = body["error"]["diagnostics"]
    assert "running" in diagnostics or "unsettled_jobs" in diagnostics


def test_succeeded_lacking_audit_or_release_receipt(native_jobs):
    """Succeeded job lacking audit or release receipt reports missing_audit/release_receipt."""
    store = _store(native_jobs)

    # 1. Missing audit receipt
    fix_a = _make_fixture(native_jobs)
    _enqueue(fix_a, store)
    worker_a = _worker(fix_a, store)
    claimed_a = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix_a.workspace_id,
        actor_id=fix_a.actor_id,
        worker_id=worker_a,
    )
    job_a = claimed_a["job"]
    assert job_a is not None
    _settle(
        fix_a,
        store,
        job_a["job_id"],
        worker_a,
        job_a["fence"],
        receipts=[_receipt(fix_a, "release", trust="trusted")],
    )

    proj_a = _exec(fix_a, fix_a.trial_id)
    assert proj_a["status"] == "succeeded"
    assert proj_a["eligible"] is False
    assert "missing_audit_receipt" in proj_a["reasons"]
    assert "missing_release_receipt" not in proj_a["reasons"]

    # 2. Missing release receipt
    fix_b = _make_fixture(native_jobs)
    _enqueue(fix_b, store)
    worker_b = _worker(fix_b, store)
    claimed_b = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix_b.workspace_id,
        actor_id=fix_b.actor_id,
        worker_id=worker_b,
    )
    job_b = claimed_b["job"]
    assert job_b is not None
    _settle(
        fix_b,
        store,
        job_b["job_id"],
        worker_b,
        job_b["fence"],
        receipts=[_receipt(fix_b, "audit", trust="trusted")],
    )

    proj_b = _exec(fix_b, fix_b.trial_id)
    assert proj_b["status"] == "succeeded"
    assert proj_b["eligible"] is False
    assert "missing_release_receipt" in proj_b["reasons"]
    assert "missing_audit_receipt" not in proj_b["reasons"]


def test_succeeded_with_untrusted_receipt(native_jobs):
    """Succeeded job with untrusted receipt reports untrusted_receipt reason."""
    fix = _make_fixture(native_jobs)
    store = _store(fix)
    _enqueue(fix, store)
    worker_id = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
    )
    job = claimed["job"]
    assert job is not None

    _settle(
        fix,
        store,
        job["job_id"],
        worker_id,
        job["fence"],
        receipts=[
            _receipt(fix, "audit", trust="verified"),
            _receipt(fix, "release", trust="trusted"),
        ],
    )

    proj = _exec(fix, fix.trial_id)
    assert proj["status"] == "succeeded"
    assert proj["eligible"] is False
    assert "untrusted_receipt" in proj["reasons"]


def test_succeeded_with_issuer_outside_manifest(native_jobs):
    """Succeeded job whose issuer is not in campaign manifest reports issuer_not_in_manifest."""
    fix = _make_fixture(native_jobs)
    unlisted_audit = _register_component(
        fix.service,
        fix.workspace_id,
        "audit",
        name=f"unlisted-audit-{uuid4()}",
    )
    store = _store(fix)
    _enqueue(fix, store)
    worker_id = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
    )
    job = claimed["job"]
    assert job is not None

    _settle(
        fix,
        store,
        job["job_id"],
        worker_id,
        job["fence"],
        receipts=[
            _receipt(fix, "audit", trust="trusted", issuer=unlisted_audit),
            _receipt(fix, "release", trust="trusted"),
        ],
    )

    proj = _exec(fix, fix.trial_id)
    assert proj["status"] == "succeeded"
    assert proj["eligible"] is False
    assert "issuer_not_in_manifest" in proj["reasons"]


def test_failed_job_ineligible(native_jobs):
    """Failed job projects as failed and ineligible."""
    fix = _make_fixture(native_jobs)
    store = _store(fix)
    _enqueue(fix, store)
    worker_id = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
    )
    job = claimed["job"]
    assert job is not None

    _settle(
        fix,
        store,
        job["job_id"],
        worker_id,
        job["fence"],
        outcome="failed",
        receipts=[],
    )

    proj = _exec(fix, fix.trial_id)
    assert proj["status"] == "failed"
    assert proj["eligible"] is False
    assert "failed" in proj["reasons"]


def test_unsettled_child_job_adds_unsettled_jobs_reason(native_jobs):
    """Root job sealed succeeded but child job backlog adds unsettled_jobs."""
    fix = _make_fixture(native_jobs)
    store = _store(fix)
    root_enq = _enqueue(fix, store)
    root_job_id = str(root_enq["job"]["job_id"])

    worker_root = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_root,
    )
    assert claimed["job"]["job_id"] == root_job_id
    _settle(fix, store, root_job_id, worker_root, claimed["job"]["fence"])

    # Enqueue a child job with parent_job_id set
    _enqueue(fix, store, parent_job_id=root_job_id)

    proj = _exec(fix, fix.trial_id)
    assert proj["status"] == "succeeded"
    assert proj["eligible"] is False
    assert "unsettled_jobs" in proj["reasons"]


def test_succeeded_with_trusted_audit_and_release_eligible_and_binding_succeeds(native_jobs):
    """Succeeded trial with trusted receipts from manifest is eligible and binds deliverable."""
    fix = _make_fixture(native_jobs)
    store = _store(fix)
    _enqueue(fix, store)
    worker_id = _worker(fix, store)
    claimed = claim_job(
        store,
        operation_id=str(uuid4()),
        workspace_id=fix.workspace_id,
        actor_id=fix.actor_id,
        worker_id=worker_id,
    )
    job = claimed["job"]
    assert job is not None

    _settle(
        fix,
        store,
        job["job_id"],
        worker_id,
        job["fence"],
        outcome="succeeded",
        receipts=[
            _receipt(fix, "audit", trust="trusted"),
            _receipt(fix, "release", trust="trusted"),
        ],
    )

    proj = _exec(fix, fix.trial_id)
    assert proj["trial_id"] == str(fix.trial_id)
    assert proj["status"] == "succeeded"
    assert proj["eligible"] is True
    assert proj["reasons"] == []

    status, body = _bind_deliverable(fix)
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["deliverable_binding"]["trial_id"] == str(fix.trial_id)


def test_each_projected_status_passes_validate_instance_trial(native_jobs):
    """Every projected status belongs to the five R02 trial statuses and passes schema validation."""
    store = _store(native_jobs)

    # 1. queued (backlog root job)
    fix_q = _make_fixture(native_jobs)
    _enqueue(fix_q, store)
    proj_q = _exec(fix_q, fix_q.trial_id)
    assert proj_q["status"] == "queued"
    validate_instance("trial", {"id": str(fix_q.trial_id), "campaign_id": str(fix_q.campaign_id), "revision": 1, "status": proj_q["status"]})

    # 2. running (admitted root job)
    fix_r = _make_fixture(native_jobs)
    _enqueue(fix_r, store)
    worker_r = _worker(fix_r, store)
    claim_job(store, operation_id=str(uuid4()), workspace_id=fix_r.workspace_id, actor_id=fix_r.actor_id, worker_id=worker_r)
    proj_r = _exec(fix_r, fix_r.trial_id)
    assert proj_r["status"] == "running"
    validate_instance("trial", {"id": str(fix_r.trial_id), "campaign_id": str(fix_r.campaign_id), "revision": 1, "status": proj_r["status"]})

    # 3. succeeded (sealed root job)
    fix_s = _make_fixture(native_jobs)
    _enqueue(fix_s, store)
    worker_s = _worker(fix_s, store)
    claimed_s = claim_job(store, operation_id=str(uuid4()), workspace_id=fix_s.workspace_id, actor_id=fix_s.actor_id, worker_id=worker_s)
    _settle(fix_s, store, claimed_s["job"]["job_id"], worker_s, claimed_s["job"]["fence"])
    proj_s = _exec(fix_s, fix_s.trial_id)
    assert proj_s["status"] == "succeeded"
    validate_instance("trial", {"id": str(fix_s.trial_id), "campaign_id": str(fix_s.campaign_id), "revision": 1, "status": proj_s["status"]})

    # 4. failed (failed root job)
    fix_f = _make_fixture(native_jobs)
    _enqueue(fix_f, store)
    worker_f = _worker(fix_f, store)
    claimed_f = claim_job(store, operation_id=str(uuid4()), workspace_id=fix_f.workspace_id, actor_id=fix_f.actor_id, worker_id=worker_f)
    _settle(fix_f, store, claimed_f["job"]["job_id"], worker_f, claimed_f["job"]["fence"], outcome="failed", receipts=[])
    proj_f = _exec(fix_f, fix_f.trial_id)
    assert proj_f["status"] == "failed"
    validate_instance("trial", {"id": str(fix_f.trial_id), "campaign_id": str(fix_f.campaign_id), "revision": 1, "status": proj_f["status"]})

    # 5. cancelled (cancelled root job)
    fix_c = _make_fixture(native_jobs)
    cancel_enq = _enqueue(fix_c, store)
    cancel_job_id = cancel_enq["job"]["job_id"]
    with _connect(fix_c) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET status='cancelled', cancel_reason='test', cancelled_at=clock_timestamp() WHERE job_id=%s",
            (cancel_job_id,),
        )
    proj_c = _exec(fix_c, fix_c.trial_id)
    assert proj_c["status"] == "cancelled"
    validate_instance("trial", {"id": str(fix_c.trial_id), "campaign_id": str(fix_c.campaign_id), "revision": 1, "status": proj_c["status"]})


def test_missing_jobs_table_or_trial_id_column(native_jobs):
    """When the jobs relation or trial_id column is absent, returns queued, [], False, ['no_job']."""
    fix = _make_fixture(native_jobs)
    with patch("omp_work.jobs.projection._has_trial_jobs", return_value=False):
        proj = _exec(fix, fix.trial_id)

    assert proj["trial_id"] == str(fix.trial_id)
    assert proj["status"] == "queued"
    assert proj["jobs"] == []
    assert proj["eligible"] is False
    assert proj["reasons"] == ["no_job"]
