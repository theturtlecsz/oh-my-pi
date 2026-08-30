from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.operations.fingerprints import service_runtime_fingerprint
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

OWNER = uuid4()

PASS_REPORT = "VERDICT: PASS\nFINDINGS\n(none)\nACCEPTANCE COVERAGE\nAC-1 covered\nOUT OF SCOPE\nnone\nCHECKS RUN\nbun test → exit 0\nREMAINING QUESTIONS\nnone"
NEEDS_FIX_REPORT = PASS_REPORT.replace("VERDICT: PASS", "VERDICT: NEEDS_FIX").replace(
    "(none)",
    "- [major] AC-1 src/x.ts:1 evidence: broken; impact: wrong; minimal fix: revert",
)
BLOCKED_REPORT = PASS_REPORT.replace("VERDICT: PASS", "VERDICT: BLOCKED")


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("workflow-service")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()
        capabilities = root / "capabilities"
        capabilities.mkdir(mode=0o700)
        owner = capabilities / "owner.json"
        owner.write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(OWNER),
                    "actor_kind": "owner",
                    "workspaces": [],
                    "scopes": [
                        "work.read",
                        "work.mutate",
                        "work.approve",
                        "work.close",
                        "work.execute",
                    ],
                }
            )
        )
        owner.chmod(0o600)
        yield SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )


def _grant(service, workspace_id) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    if str(workspace_id) not in data["workspaces"]:
        data["workspaces"].append(str(workspace_id))
        owner.write_text(json.dumps(data))
        owner.chmod(0o600)


def _seed_project(
    service, workspace_id, project_id, name: str = "Test Project"
) -> None:
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.projects(project_id, workspace_id, key, name, kind, provenance) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    workspace_id,
                    f"PROJ-{str(project_id)[:4]}",
                    name,
                    "surface",
                    json.dumps({"source": "test"}),
                ),
            )


def _owner_headers(workspace_id) -> dict[str, str]:
    return {
        "Authorization": "Bearer owner-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _command(
    service,
    workspace_id,
    command: dict,
    *,
    token: str = "owner-token",
    operation_id=None,
) -> tuple[int, dict]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    response = service.client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
        json=envelope,
    )
    return response.status_code, response.json()


def _batch(items: list[dict], relations: list[dict] | None = None) -> dict:
    return {
        "type": "create_work_batch",
        "payload": {"items": items, "relations": relations or []},
    }


def _receipt(
    work_id, revision_id, candidate_id, kind: str, *, body: dict | None = None, **extra
) -> dict:
    body = body if body is not None else {"body": f"{kind} evidence body"}
    return {
        "receipt_id": str(uuid4()),
        "work_id": str(work_id),
        "revision_id": str(revision_id),
        "candidate_id": str(candidate_id),
        "kind": kind,
        "payload": body,
        "payload_sha256": sha256(body),
        "issuer": "owner",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def _create(service, workspace_id, title: str = "item", **extra) -> dict:
    status, body = _command(
        service, workspace_id, _batch([{"client_ref": "root", "title": title, **extra}])
    )
    assert status == 200, body
    return body["result"]["items"][0]


def _plan(service, workspace_id, item: dict, candidate_hash: str | None = None) -> dict:
    receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        str(uuid4()),
        "plan",
        body={"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"},
        candidate_sha256=candidate_hash or secrets.token_hex(32),
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    return body["result"]["receipt"]


def _finalize(
    service,
    workspace_id,
    item: dict,
    planned_id: str,
    *,
    commit: str | None = None,
    final_id=None,
    candidate_hash: str | None = None,
) -> tuple[int, dict]:
    payload = {
        "work_id": item["work_id"],
        "revision_id": item["revision_id"],
        "planned_candidate_id": planned_id,
        "candidate_id": str(final_id or uuid4()),
        "candidate_sha256": candidate_hash or secrets.token_hex(32),
        "commit_sha": commit or secrets.token_hex(20),
    }
    return _command(
        service, workspace_id, {"type": "finalize_candidate", "payload": payload}
    )


def _begin(
    service,
    workspace_id,
    item: dict,
    *,
    authorization_ref: str | None = None,
    attempt_id=None,
    identity: dict | None = None,
    operation_id=None,
) -> tuple[int, dict]:
    payload = {
        "work_id": item["work_id"],
        "attempt_id": str(attempt_id or uuid4()),
        "authorization_ref": authorization_ref or f"summary:{uuid4()}",
        "owner_session_id": "session-test",
        "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
        "owner_session_start_commit": "e" * 40,
        "repository": "/repo",
        "diff_sha256": secrets.token_hex(32),
        "starting_dirty_paths": [],
        **(identity or {}),
    }
    return _command(
        service,
        workspace_id,
        {"type": "begin_close_attempt", "payload": payload},
        operation_id=operation_id,
    )


def _verify_and_seal(
    service, workspace_id, item: dict, final: dict, attempt: dict
) -> dict:
    """Append verification, then seal — returns the applied seal result."""
    binding = {
        "candidate_sha256": final["candidate_sha256"],
        "candidate_commit": final["commit_sha"],
    }
    verification = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "verification",
        **binding,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": verification}},
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_audit_manifest",
            "payload": {
                "attempt_id": attempt["attempt_id"],
                "verification_receipt_id": verification["receipt_id"],
            },
        },
    )
    assert status == 200, body
    return body["result"]


def _reserve(
    service, workspace_id, attempt_id: str, task_sha256: str
) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {
            "type": "reserve_auditor_launch",
            "payload": {
                "attempt_id": attempt_id,
                "task_sha256": task_sha256,
                "tool_call_id": f"tc-{uuid4()}",
            },
        },
    )


def _cancel(service, workspace_id, attempt_id: str, launch_id: str) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {
            "type": "cancel_auditor_launch",
            "payload": {"attempt_id": attempt_id, "launch_id": launch_id},
        },
    )


def _settle(
    service,
    workspace_id,
    attempt_id: str,
    launch_id: str,
    payload_value=None,
    *,
    failed: bool = False,
) -> tuple[int, dict]:
    payload: dict = {"attempt_id": attempt_id, "launch_id": launch_id}
    if failed:
        payload["transport_failed"] = True
    else:
        payload["transport_payload"] = payload_value
    return _command(
        service, workspace_id, {"type": "settle_auditor_launch", "payload": payload}
    )


def _attest(
    service,
    workspace_id,
    event: dict,
    status_value: str = "delivered",
    *,
    authorization_ref: str | None = None,
) -> tuple[int, dict]:
    payload = {
        "event_id": event["event_id"],
        "owner_session_id": "session-test",
        "rendered_sha256": event["rendered_sha256"],
        "status": status_value,
    }
    if authorization_ref is not None:
        payload["authorization_ref"] = authorization_ref
    return _command(
        service,
        workspace_id,
        {"type": "attest_checkpoint_delivery", "payload": payload},
    )


def _drain_deliveries(service, workspace_id, key: str = "OMP-1") -> None:
    """Deliver every unresolved requires_delivery event so close gates pass."""
    view = service.client.get(
        f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    latest: dict[str, tuple[int, str]] = {}
    for delivery in view["checkpoint_deliveries"]:
        prior = latest.get(delivery["event_id"])
        if prior is None or delivery["delivery_sequence"] > prior[0]:
            latest[delivery["event_id"]] = (
                delivery["delivery_sequence"],
                delivery["status"],
            )
    for event in view["close_attempt_events"]:
        if not event["requires_delivery"]:
            continue
        state = latest.get(event["event_id"])
        if state is not None and state[1] in ("delivered", "waived"):
            continue
        status, body = _attest(service, workspace_id, event)
        assert status == 200 and body["result"]["status"] == "applied", body


def _audited_attempt(
    service, workspace_id, title: str = "close target"
) -> tuple[dict, dict, dict]:
    """Full happy path through PASS settle: returns (item, final, attempt-after-settle)."""
    item = _create(service, workspace_id, title)
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied", body
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_id,
        json.dumps({"verdict": "PASS", "report": PASS_REPORT}),
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["verdict"] == "PASS"
    ), body
    return item, final, body["result"]["attempt"]


def _record_review(
    service,
    workspace_id,
    item: dict,
    final: dict,
    attempt: dict,
    *,
    authorization_ref: str | None = None,
    operation_id=None,
    review_body: dict | None = None,
) -> tuple[int, dict]:
    closeout = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "closeout",
        body=review_body,
    )
    return _command(
        service,
        workspace_id,
        {
            "type": "record_closeout_review",
            "payload": {
                "receipt": closeout,
                "attempt_id": attempt["attempt_id"],
                "authorization_ref": authorization_ref or attempt["authorization_ref"],
            },
        },
        operation_id=operation_id,
    )


def _complete(
    service,
    workspace_id,
    item: dict,
    final: dict,
    attempt_id: str,
    *,
    done_ref: str | None = None,
    satisfied: list[str] | None = None,
    cancellations: list[dict] | None = None,
    key: str = "OMP-1",
    operation_id=None,
) -> tuple[int, dict]:
    workflow = service.client.get(
        f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    completion = {
        "work_id": item["work_id"],
        "current_revision_id": item["revision_id"],
        "candidate": workflow["item"]["candidate"],
        "receipts": [
            receipt
            for receipt in workflow["receipts"]
            if receipt["candidate_id"] == final["candidate_id"]
        ],
        "closeout_requested": True,
    }
    payload = {
        "input": completion,
        "attempt_id": attempt_id,
        "done_authorization_ref": done_ref or f"done:{uuid4()}",
        **({"satisfied_work_ids": satisfied} if satisfied else {}),
        **({"cancellations": cancellations} if cancellations else {}),
    }
    return _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": payload},
        operation_id=operation_id,
    )


def test_rich_batch_atomicity_rollback_and_replay(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    batch = _batch(
        [
            {
                "client_ref": "parent",
                "title": "Parent",
                "scope": "world",
                "acceptance_criteria": ["children exist"],
            },
            {"client_ref": "child-a", "title": "Child A", "state": "NOW"},
            {"client_ref": "child-b", "title": "Child B"},
        ],
        [
            {"source_ref": "child-a", "target_ref": "parent", "kind": "parent"},
            {"source_ref": "child-b", "target_ref": "parent", "kind": "parent"},
            {"source_ref": "child-a", "target_ref": "child-b", "kind": "blocks"},
        ],
    )
    operation_id = uuid4()
    status, body = _command(service, workspace_id, batch, operation_id=operation_id)
    assert status == 200, body
    items = body["result"]["items"]
    assert [item["key"] for item in items] == ["OMP-1", "OMP-2", "OMP-3"]
    status, replay = _command(service, workspace_id, batch, operation_id=operation_id)
    assert (
        status == 200
        and replay["receipt"]["state"] == "replayed"
        and replay["result"] == body["result"]
    )
    status, body = _command(
        service,
        workspace_id,
        _batch(
            [{"client_ref": "a", "title": "A"}, {"client_ref": "b", "title": "B"}],
            [
                {"source_ref": "a", "target_ref": "b", "kind": "blocks"},
                {"source_ref": "b", "target_ref": "a", "kind": "blocks"},
            ],
        ),
    )
    assert status == 400 and body["error"]["code"] == "relation_cycle"


def test_begin_refuses_without_final_candidate_or_plan(service) -> None:
    # Scenario: keep-open without plan/authorization — typed refusals, never DONE.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "no plan")
    status, body = _begin(service, workspace_id, item)
    assert status == 200, body
    result = body["result"]
    assert (
        result["status"] == "refused"
        and result["event"]["reason_code"] == "candidate_not_final"
    )
    assert result["event"]["requires_delivery"] is True
    # audit appends are ALWAYS refused — receipts are settle-minted only.
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    binding = {
        "candidate_sha256": final["candidate_sha256"],
        "candidate_commit": final["commit_sha"],
    }
    forged = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "audit",
        independent=True,
        verdict="PASS",
        **binding,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": forged}},
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"
    # record_closeout_review without an audited attempt refuses too.
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    attempt = body["result"]["attempt"]
    status, body = _record_review(
        service, workspace_id, item, {"candidate_id": str(uuid4())}, attempt
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "attempt_not_audited"
    )


def test_manifest_falls_back_to_description_acceptance_criteria(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(
        service,
        workspace_id,
        description="Context\n\n## Acceptance criteria\n- preserves exact range\n2. reports every check\n\n## Verification\n- not acceptance",
    )
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200, body
    seal = _verify_and_seal(
        service, workspace_id, item, final, body["result"]["attempt"]
    )
    task = seal["manifest"]["task_body"]
    criteria = task.split("Acceptance criteria\n", 1)[1].split("\n\nStarting state", 1)[
        0
    ]
    assert criteria == "- AC-1: preserves exact range\n- AC-2: reports every check"
    assert "not acceptance" not in criteria


def test_cancelled_launch_preserves_budget_but_failed_settlement_burns(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id)
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200, body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied", body
    first = body["result"]["launch"]
    status, body = _cancel(
        service, workspace_id, attempt["attempt_id"], first["launch_id"]
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    cancelled = body["result"]
    assert cancelled["attempt"]["state"] == "audit_ready"
    assert cancelled["attempt"]["launch_count"] == 1
    assert cancelled["attempt"]["cancelled_launch_count"] == 1
    assert cancelled["event"]["remaining_launches"] == 3
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied", body
    second = body["result"]["launch"]
    assert second["launch_number"] == 2
    status, body = _settle(
        service, workspace_id, attempt["attempt_id"], second["launch_id"], failed=True
    )
    assert status == 200 and body["result"]["status"] == "refused", body
    burned = body["result"]
    assert burned["attempt"]["state"] == "audit_ready"
    assert burned["attempt"]["launch_count"] == 2
    assert burned["attempt"]["cancelled_launch_count"] == 1
    assert burned["event"]["remaining_launches"] == 2


def test_sealed_manifest_and_full_pass_flow_to_done(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id)
    # The sealed task is rendered by the workflow view, hash-pinned.
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    manifest = view["audit_manifest"]
    assert manifest is not None and manifest["task_sha256"] == text_sha256(
        manifest["task_body"]
    )
    assert (
        "Plan receipt SHA-256:" in manifest["task_body"]
        and f"Final commit: {final['commit_sha']}" in manifest["task_body"]
    )
    # The settle-minted audit receipt is the ONLY audit receipt, independent PASS.
    audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"]
    assert (
        len(audits) == 1
        and audits[0]["independent"] is True
        and audits[0]["verdict"] == "PASS"
    )
    assert audits[0]["issuer"] == "work-service/auditor-settle"
    assert audits[0]["payload"]["report"] == PASS_REPORT
    # Closeout review requires the audited attempt and atomically transitions to closeout_requested.
    # It refuses if deliveries are pending.
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "delivery_pending"
    )
    _drain_deliveries(service, workspace_id)
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["attempt"]["state"] == "closeout_requested"
    ), body
    assert body["result"]["event"]["event_type"] == "closeout_review_recorded"
    # Replay of record_closeout_review is idempotent applied
    status, replay = _record_review(service, workspace_id, item, final, attempt)
    assert (
        status == 200
        and replay["result"]["status"] == "applied"
        and replay["result"]["attempt"]["state"] == "closeout_requested"
    )
    # A different review body on already closeout_requested refuses typed already_requested
    status, diff_body = _record_review(
        service,
        workspace_id,
        item,
        final,
        attempt,
        review_body={"body": "different closeout review"},
        operation_id=uuid4(),
    )
    assert (
        status == 200
        and diff_body["result"]["status"] == "refused"
        and diff_body["result"]["event"]["reason_code"] == "already_requested"
    )
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    closeouts = [
        receipt for receipt in view["receipts"] if receipt["kind"] == "closeout"
    ]
    assert len(closeouts) == 1 and closeouts[0]["payload"] == {
        "body": "closeout evidence body"
    }
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final["commit_sha"],
    )
    status, _ = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200
    _drain_deliveries(service, workspace_id)
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=attempt["authorization_ref"],
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "done_authorization_not_fresh"
    )
    operation_id = uuid4()
    done_ref = f"done:{uuid4()}"
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=done_ref,
        operation_id=operation_id,
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["state"] == "DONE"
    ), body
    status, replay = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=done_ref,
        operation_id=operation_id,
    )
    assert (
        status == 200
        and replay["receipt"]["state"] == "replayed"
        and replay["result"] == body["result"]
    )
    # A REUSED done authorization on new work refuses.
    item2, final2, attempt2 = _audited_attempt(service, workspace_id, "second")
    _drain_deliveries(service, workspace_id, key="OMP-2")
    status, body = _record_review(service, workspace_id, item2, final2, attempt2)
    assert status == 200 and body["result"]["status"] == "applied"
    push2 = _receipt(
        item2["work_id"],
        item2["revision_id"],
        final2["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final2["commit_sha"],
    )
    status, _ = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": push2}},
    )
    assert status == 200
    status, body = _complete(
        service,
        workspace_id,
        item2,
        final2,
        attempt2["attempt_id"],
        done_ref=done_ref,
        key="OMP-2",
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "done_authorization_reused"
    )


def test_containment_push_receipt_completes_to_done(service) -> None:
    # OMP-99: a push receipt recording a newer same-branch tip (remote_commit)
    # plus the audited candidate (candidate_commit) clears push_unverified.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id)
    _drain_deliveries(service, workspace_id)
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    _drain_deliveries(service, workspace_id)
    tip = "a" * 40 if final["commit_sha"] != "a" * 40 else "b" * 40
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=tip,
        candidate_commit=final["commit_sha"],
    )
    status, _ = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=f"done:{uuid4()}",
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["state"] == "DONE"
    ), body


def test_reserve_mismatch_burns_nothing_and_budget_exhausts(service) -> None:
    # Scenarios: mismatch before spawn (zero burn), malformed transport,
    # transport failure before dispatch, budget exhaustion.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "budget target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]

    # Mismatched task bytes: refused, launch_count unchanged.
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], secrets.token_hex(32)
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "manifest_task_mismatch"
    )
    assert body["result"]["attempt"]["launch_count"] == 0

    # Launch 1: wrapper verdict contradicts the report line — mismatch burns, wrapper not trusted.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied"
    launch_1 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_1,
        {"verdict": "NEEDS_FIX", "report": PASS_REPORT},
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "report_wrapper_verdict_mismatch"
    )
    assert (
        body["result"]["attempt"]["state"] == "audit_ready"
        and body["result"]["attempt"]["launch_count"] == 1
    )

    # Launch 2: transport failed before any payload arrived — burns typed.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_2 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service, workspace_id, attempt["attempt_id"], launch_2, failed=True
    )
    assert (
        status == 200 and body["result"]["event"]["reason_code"] == "transport_failed"
    )
    assert (
        body["result"]["attempt"]["state"] == "audit_ready"
        and body["result"]["attempt"]["launch_count"] == 2
    )

    # Launch 3: verdict missing — burns the FINAL launch; the attempt exhausts.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_3 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service, workspace_id, attempt["attempt_id"], launch_3, "no verdict here"
    )
    assert status == 200 and body["result"]["event"]["reason_code"] == "verdict_missing"
    assert body["result"]["attempt"]["state"] == "budget_exhausted"
    assert body["result"]["event"]["requires_fresh_authorization"] is True

    # A fourth reserve refuses: the attempt is terminal.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "attempt_not_ready"
    )

    # Only a NEW literal /summary (fresh authorization) creates a replacement.
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["launch_count"] == 0


def test_raw_auditor_wrapper_settles_to_pass_receipt(service) -> None:
    # OMP-123: the task tool yield payload produces {"raw": "VERDICT: ..."}
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "raw wrapper target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied", body
    launch_id = body["result"]["launch"]["launch_id"]
    # Test exact serialized raw payload (including pretty-printed formatting)
    raw_payload = json.dumps({"raw": PASS_REPORT}, indent=2)
    status, body = _settle(
        service, workspace_id, attempt["attempt_id"], launch_id, raw_payload
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["verdict"] == "PASS"
    ), body
    assert body["result"]["attempt"]["state"] == "audited"
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"]
    assert (
        len(audits) == 1
        and audits[0]["independent"] is True
        and audits[0]["verdict"] == "PASS"
    )
    assert audits[0]["issuer"] == "work-service/auditor-settle"
    assert audits[0]["payload"]["report"] == PASS_REPORT


def test_raw_auditor_wrapper_refusals_and_budget(service) -> None:
    # OMP-123: ambiguous or malformed raw wrappers refuse with standard budget burn
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "raw wrapper refusal target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]

    # Launch 1: ambiguous raw + report keys refuses as report_wrapper_invalid
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied"
    launch_1 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_1,
        json.dumps({"raw": PASS_REPORT, "report": PASS_REPORT}),
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "report_wrapper_invalid"
    )
    assert (
        body["result"]["attempt"]["state"] == "audit_ready"
        and body["result"]["attempt"]["launch_count"] == 1
    )

    # Launch 2: non-string raw payload refuses as report_wrapper_invalid
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_2 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_2,
        json.dumps({"raw": 12345}),
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "report_wrapper_invalid"
    )
    assert (
        body["result"]["attempt"]["state"] == "audit_ready"
        and body["result"]["attempt"]["launch_count"] == 2
    )

    # Launch 3: verdict mismatch with raw payload burns the third launch to budget_exhausted
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_3 = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_3,
        json.dumps({"verdict": "NEEDS_FIX", "raw": PASS_REPORT}),
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "report_wrapper_verdict_mismatch"
    )
    assert body["result"]["attempt"]["state"] == "budget_exhausted"
    assert body["result"]["event"]["requires_fresh_authorization"] is True

    # No audit receipts were minted
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"] == []


def test_candidate_mutation_before_settle_supersedes_without_receipt(service) -> None:
    # Scenarios: stale report / mutation before receipt commit — drift at settle
    # supersedes the attempt and inserts NO audit receipt.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "drift target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"]
    )
    launch_id = body["result"]["launch"]["launch_id"]

    # Mutate the candidate while the auditor "runs": the item's current
    # candidate pointer moves under the frozen attempt identity. OMP-124: a
    # replan now supersedes the attempt at stamp time and candidate rows are
    # immutable, so the settle-time drift path is exercised by a direct
    # pointer move instead of replan + refinalize.
    moved_candidate = uuid4()
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as connection:
        connection.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,allocated_at) VALUES(%s,%s,%s,%s,%s,%s,now())",
            (
                moved_candidate,
                workspace_id,
                item["work_id"],
                item["revision_id"],
                secrets.token_hex(32),
                "f" * 40,
            ),
        )
        connection.execute(
            "UPDATE omp_work.work_items SET current_candidate_id=%s WHERE work_id=%s",
            (moved_candidate, item["work_id"]),
        )

    status, body = _settle(
        service, workspace_id, attempt["attempt_id"], launch_id, PASS_REPORT
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "candidate_drift"
    )
    assert body["result"]["attempt"]["state"] == "superseded"
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    settle_audits = [
        receipt
        for receipt in view["receipts"]
        if receipt["kind"] == "audit"
        and receipt["issuer"] == "work-service/auditor-settle"
    ]
    assert settle_audits == []


def test_duplicate_begins_one_live_attempt(service) -> None:
    # Scenario: concurrent duplicate attempts — same authorization is idempotent;
    # a different authorization supersedes; exactly one live attempt survives.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "duplicate begins")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200
    ref = f"summary:{uuid4()}"
    # The host derives attempt_id deterministically from the authorization and
    # freezes the whole identity payload — a legitimate retry is byte-identical.
    identity = {
        "attempt_id": str(uuid4()),
        "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
        "diff_sha256": secrets.token_hex(32),
    }
    status, body = _begin(
        service, workspace_id, item, authorization_ref=ref, identity=identity
    )
    assert status == 200 and body["result"]["status"] == "applied"
    first = body["result"]["attempt"]
    # Identical authorization under a DIFFERENT operation_id: returns stored outcome byte-for-byte.
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=ref,
        identity=identity,
        operation_id=uuid4(),
    )
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] == first["attempt_id"]
    # Same authorization but drifted identity (diff_sha256 changed): returns authorization_reuse_conflict
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=ref,
        identity={"diff_sha256": secrets.token_hex(32)},
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "authorization_reuse_conflict"
    # Fresh authorization against unfinished attempt supersedes it and starts a fresh attempt.
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] != first["attempt_id"]
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    live = [
        attempt
        for attempt in view["close_attempts"]
        if attempt["state"]
        in (
            "active",
            "audit_ready",
            "auditor_in_flight",
            "audited",
            "closeout_requested",
        )
    ]
    assert len(live) == 1
    superseded = [
        attempt
        for attempt in view["close_attempts"]
        if attempt["state"] == "superseded"
    ]
    assert (
        len(superseded) == 1
        and superseded[0]["terminal_reason"] == "superseded_by_new_summary"
    )
    # A terminal attempt's authorization can never be reused.
    status, body = _begin(service, workspace_id, item, authorization_ref=ref)
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "authorization_reuse_conflict"
    )


def test_replan_lands_on_unaudited_final_candidate(service) -> None:
    # OMP-124: an owner-approved plan always mints a new planned candidate on
    # the same revision — no failed-audit prerequisite, no stale_evidence.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replan target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    # The final candidate carries NO audit — the old gate refused this 409.
    replan = _plan(service, workspace_id, item)
    assert replan["candidate_id"] != final["candidate_id"]
    workflow = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert workflow["item"]["candidate"]["candidate_id"] == replan["candidate_id"]
    assert workflow["close_attempt_events"] == []


def test_replan_supersedes_live_attempt(service) -> None:
    # OMP-124: a replan supersedes the in-motion close attempt with the typed
    # terminal reason and preserves the one-live-attempt invariant.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replan supersede target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    old_attempt = body["result"]["attempt"]
    replan = _plan(service, workspace_id, item)
    assert replan["candidate_id"] != plan["candidate_id"]
    workflow = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    live = [
        attempt
        for attempt in workflow["close_attempts"]
        if attempt["state"]
        in (
            "active",
            "audit_ready",
            "auditor_in_flight",
            "audited",
            "closeout_requested",
        )
    ]
    assert live == []
    superseded = [
        attempt
        for attempt in workflow["close_attempts"]
        if attempt["state"] == "superseded"
    ]
    assert (
        len(superseded) == 1
        and superseded[0]["attempt_id"] == old_attempt["attempt_id"]
    )
    assert superseded[0]["terminal_reason"] == "superseded_by_new_plan"
    events = [
        event
        for event in workflow["close_attempt_events"]
        if event["event_type"] == "attempt_superseded"
    ]
    assert len(events) == 1
    assert (
        events[0]["reason_code"] == "superseded_by_new_plan"
        and events[0]["requires_delivery"] is True
    )
    # Finalize the new plan and begin again: exactly one live attempt survives.
    status, body = _finalize(service, workspace_id, item, replan["candidate_id"])
    assert status == 200, body
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    workflow = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    live = [
        attempt
        for attempt in workflow["close_attempts"]
        if attempt["state"]
        in (
            "active",
            "audit_ready",
            "auditor_in_flight",
            "audited",
            "closeout_requested",
        )
    ]
    assert len(live) == 1 and live[0]["attempt_id"] != old_attempt["attempt_id"]


def test_replan_refinalize_same_candidate_commit_succeeds(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replan refinalize target")
    candidate_hash = secrets.token_hex(32)
    commit = secrets.token_hex(20)
    plan1 = _plan(service, workspace_id, item)
    status, body = _finalize(
        service,
        workspace_id,
        item,
        plan1["candidate_id"],
        candidate_hash=candidate_hash,
        commit=commit,
    )
    assert status == 200, body
    final1 = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"

    # Replan without changing revision or code commit
    plan2 = _plan(service, workspace_id, item)
    assert plan2["candidate_id"] != plan1["candidate_id"]

    # Finalize again with the same candidate_hash and commit
    status, body = _finalize(
        service,
        workspace_id,
        item,
        plan2["candidate_id"],
        candidate_hash=candidate_hash,
        commit=commit,
    )
    assert status == 200, body
    final2 = body["result"]["candidate"]
    assert final2["candidate_id"] == final1["candidate_id"]

    # Begin close attempt succeeds under new plan
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"


def test_finalize_rejects_collision_with_planned_candidate(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "collision planned target")
    candidate_hash = secrets.token_hex(32)
    plan = _plan(service, workspace_id, item, candidate_hash=candidate_hash)
    # Attempting to finalize using the exact candidate_hash that was allocated to the planned row
    status, body = _finalize(
        service, workspace_id, item, plan["candidate_id"], candidate_hash=candidate_hash
    )
    assert status == 409 and body["error"]["code"] == "stale_evidence", body


def test_finalize_rejects_same_candidate_hash_different_commit(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "hash match commit mismatch target")
    candidate_hash = secrets.token_hex(32)
    commit1 = secrets.token_hex(20)
    commit2 = secrets.token_hex(20)
    plan1 = _plan(service, workspace_id, item)
    status, body = _finalize(
        service,
        workspace_id,
        item,
        plan1["candidate_id"],
        candidate_hash=candidate_hash,
        commit=commit1,
    )
    assert status == 200, body
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"

    # Replan, then attempt to finalize with the same candidate_hash but a different commit
    plan2 = _plan(service, workspace_id, item)
    status, body = _finalize(
        service,
        workspace_id,
        item,
        plan2["candidate_id"],
        candidate_hash=candidate_hash,
        commit=commit2,
    )
    assert status == 409 and body["error"]["code"] == "stale_evidence", body


def test_replan_supersedes_in_flight_attempt_after_revision_clear(service) -> None:
    # OMP-124 (Sol-xhigh escalation review): supersession is unconditional — a
    # live attempt is superseded even when a revision change has cleared the
    # item's current candidate, and its in-flight launch is orphaned.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "revise-clear supersede target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    old_attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, old_attempt)
    status, body = _reserve(
        service,
        workspace_id,
        old_attempt["attempt_id"],
        seal["manifest"]["task_sha256"],
    )
    assert status == 200 and body["result"]["status"] == "applied"
    launch_id = body["result"]["launch"]["launch_id"]
    # Revision change clears current_candidate_id; the live attempt survives.
    new_revision_id = uuid4()
    revision = {
        "revision_id": str(new_revision_id),
        "work_id": item["work_id"],
        "revision_number": 2,
        "title": "revise-clear supersede target",
        "description": "revised",
        "scope": "",
        "acceptance_criteria": [],
        "content_sha256": sha256(
            {"title": "revise-clear supersede target", "description": "revised"}
        ),
        "created_by": "owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "revise_work",
            "payload": {
                "work_id": item["work_id"],
                "expected_revision_id": item["revision_id"],
                "revision": revision,
            },
        },
    )
    assert status == 200 and body["result"]["changed"] is True, body
    replan = _plan(
        service,
        workspace_id,
        {"work_id": item["work_id"], "revision_id": str(new_revision_id)},
    )
    workflow = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert workflow["item"]["candidate"]["candidate_id"] == replan["candidate_id"]
    superseded = [
        attempt
        for attempt in workflow["close_attempts"]
        if attempt["state"] == "superseded"
    ]
    assert (
        len(superseded) == 1
        and superseded[0]["attempt_id"] == old_attempt["attempt_id"]
    )
    assert superseded[0]["terminal_reason"] == "superseded_by_new_plan"
    assert superseded[0]["in_flight_launch_id"] is None
    events = [
        event
        for event in workflow["close_attempt_events"]
        if event["event_type"] == "attempt_superseded"
    ]
    assert len(events) == 1 and events[0]["reason_code"] == "superseded_by_new_plan"
    # The orphaned launch can never settle.
    status, body = _settle(
        service, workspace_id, old_attempt["attempt_id"], launch_id, PASS_REPORT
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "launch_not_in_flight"


def test_remediation_required_blocks_closeout_until_fresh_summary(service) -> None:
    # Scenario: closeout after remediation — NEEDS_FIX terminal state refuses
    # record_closeout_review with a fresh-authorization requirement.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "remediation")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"]
    )
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_id,
        {"report": NEEDS_FIX_REPORT},
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["verdict"] == "NEEDS_FIX"
    )
    assert body["result"]["attempt"]["state"] == "remediation_required"
    assert body["result"]["event"]["requires_fresh_authorization"] is True
    assert body["result"]["event"]["legal_next_actions"] == [
        "fix the findings",
        "after fixing: if code changed, enter /plan then /summary; otherwise enter /summary",
    ]
    assert (
        "next: fix the findings; after fixing: if code changed, enter /plan then /summary; otherwise enter /summary"
        in body["result"]["event"]["rendered_text"]
    )
    status, body = _record_review(
        service, workspace_id, item, {"candidate_id": str(uuid4())}, attempt
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "attempt_not_audited"
    assert body["result"]["event"]["requires_fresh_authorization"] is True
    # The NEEDS_FIX receipt IS recorded (accepted report), but completion refuses.
    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"]
    assert len(audits) == 1 and audits[0]["verdict"] == "NEEDS_FIX"


def test_same_session_child_completion_valid_and_invalid(service) -> None:
    # Scenario: valid/invalid same-session receipt — one invalid child refuses
    # the WHOLE completion; valid children complete with the parent.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "parent work")
    # Children created after the owner session started, parent-linked.
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "c1", "title": "found+fixed child"}]),
    )
    child = body["result"]["items"][0]
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "c2", "title": "unreceipted child"}]),
    )
    stray = body["result"]["items"][0]
    for source in (child, stray):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "put_relation",
                "payload": {
                    "relation": {
                        "workspace_id": str(workspace_id),
                        "source_work_id": source["work_id"],
                        "target_work_id": item["work_id"],
                        "kind": "parent",
                        "active": True,
                    }
                },
            },
        )
        assert status == 200, body
    # Valid same-session receipt on the child, binding the parent attempt.
    link = {
        "attempt_id": attempt["attempt_id"],
        "owner_session_id": "session-test",
        "base_commit": "e" * 40,
        "fix_commit": final["commit_sha"],
        "candidate_sha256": final["candidate_sha256"],
        "finding": "child bug found in-session",
        "verification": "child fix proven in-session",
    }
    receipt = _receipt(
        child["work_id"],
        child["revision_id"],
        final["candidate_id"],
        "same_session_found_fixed",
        body=link,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body
    # A receipt binding the WRONG candidate refuses at append.
    bad_link = dict(link, candidate_sha256=secrets.token_hex(32))
    bad = _receipt(
        stray["work_id"],
        stray["revision_id"],
        final["candidate_id"],
        "same_session_found_fixed",
        body=bad_link,
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": bad}}
    )
    assert status == 409 and body["error"]["code"] == "stale_evidence"
    # Close ritual on the parent.
    _drain_deliveries(service, workspace_id)
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final["commit_sha"],
    )
    status, _ = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200
    _drain_deliveries(service, workspace_id)
    # Invalid child (no receipt) refuses the WHOLE completion — nothing moves.
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        satisfied=[child["work_id"], stray["work_id"]],
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "child_receipt_invalid"
    )
    assert "no same_session_found_fixed receipt" in body["result"]["event"]["reason"]
    tree = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)
    ).json()
    states = {entry["alias"]["key"]: entry["state"] for entry in tree["items"]}
    assert states["OMP-1"] != "DONE" and states["OMP-2"] != "DONE"
    # Valid child alone completes with the parent, atomically.
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        satisfied=[child["work_id"]],
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["completed_work_ids"] == [child["work_id"]]
    tree = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)
    ).json()
    states = {entry["alias"]["key"]: entry["state"] for entry in tree["items"]}
    assert (
        states["OMP-1"] == "DONE"
        and states["OMP-2"] == "DONE"
        and states["OMP-3"] != "DONE"
    )


def test_create_same_session_child_atomic(service) -> None:
    # OMP-139: the atomic filing lands child + parent edge + typed receipt in one
    # transaction; every refusal and an injected mid-transaction failure leave
    # NO child, edge, receipt, or alias behind.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "atomic parent")

    def _filing(**overrides) -> dict:
        payload = {
            "parent_work_id": item["work_id"],
            "attempt_id": attempt["attempt_id"],
            "owner_session_id": "session-test",
            "item": {
                "client_ref": "c",
                "title": overrides.pop("title", "atomic child fix"),
            },
            "finding": "bug found in the owner session",
            "verification": "fix proven in the owner session",
        }
        payload.update(overrides)
        return {"type": "create_same_session_child", "payload": payload}

    def _tree_counts() -> dict[str, str]:
        tree = service.client.get(
            f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)
        ).json()
        return {entry["alias"]["key"]: entry["state"] for entry in tree["items"]}

    # Success: child + edge + receipt in one command.
    status, body = _command(service, workspace_id, _filing())
    assert status == 200, body
    child = body["result"]["item"]
    receipt = body["result"]["receipt"]
    assert body["result"]["type"] == "create_same_session_child"
    assert child["state"] == "BACKLOG"
    assert receipt["kind"] == "same_session_found_fixed"
    assert receipt["work_id"] == child["work_id"]
    assert receipt["candidate_id"] == final["candidate_id"]
    assert receipt["payload"]["base_commit"] == "e" * 40
    assert receipt["payload"]["fix_commit"] == final["commit_sha"]
    assert receipt["payload"]["candidate_sha256"] == final["candidate_sha256"]
    view = service.client.get(
        f"/v1/work-items/{child['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    edges = [
        edge
        for edge in view["relations"]
        if edge["kind"] == "parent"
        and edge["active"]
        and edge["source_work_id"] == child["work_id"]
    ]
    assert len(edges) == 1 and edges[0]["target_work_id"] == item["work_id"]
    baseline = _tree_counts()
    assert (
        set(baseline) == {"OMP-1", child["key"]} and baseline[child["key"]] == "BACKLOG"
    )

    # Stale attempt: unknown attempt id refuses and creates nothing.
    status, body = _command(
        service,
        workspace_id,
        _filing(attempt_id=str(uuid4()), title="stale attempt child"),
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    # Stale session: wrong owner session refuses and creates nothing.
    status, body = _command(
        service,
        workspace_id,
        _filing(owner_session_id="session-imposter", title="stale session child"),
    )
    assert status == 409 and body["error"]["code"] == "stale_evidence", body
    # Malformed body: blank finding refuses at envelope validation.
    status, body = _command(
        service, workspace_id, _filing(finding="   ", title="blank finding child")
    )
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    assert _tree_counts() == baseline

    # Injected service failure mid-transaction: nothing survives, alias unchanged.
    from omp_work.v1.store import PostgresWorkStore, WorkStoreError

    original = PostgresWorkStore._create_items

    def boom(self, cur, ws, payload):
        original(self, cur, ws, payload)
        raise WorkStoreError("unavailable", ("injected failure after child insert",))

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PostgresWorkStore, "_create_items", boom)
        status, body = _command(service, workspace_id, _filing(title="doomed child"))
    assert status == 503 and body["error"]["code"] == "unavailable", body
    assert _tree_counts() == baseline
    view = service.client.get(
        f"/v1/work-items/{child['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert (
        len([r for r in view["receipts"] if r["kind"] == "same_session_found_fixed"])
        == 1
    )

    # The filed child completes atomically with the parent through the existing
    # OMP-52 completion logic — no extra authority path.
    _drain_deliveries(service, workspace_id)
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final["commit_sha"],
    )
    status, _ = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200
    _drain_deliveries(service, workspace_id)
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        satisfied=[child["work_id"]],
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["completed_work_ids"] == [child["work_id"]]
    states = _tree_counts()
    assert states["OMP-1"] == "DONE" and states[child["key"]] == "DONE"

    # Closed parent: a filing against a DONE parent refuses outright.
    status, body = _command(service, workspace_id, _filing(title="late child"))
    assert status == 400 and body["error"]["code"] == "invalid_request", body
    assert "closed" in " ".join(body["error"].get("diagnostics", [])), body
    assert _tree_counts() == states


def test_contract_mismatch_handshake_refuses_stale_hosts(service) -> None:
    # OMP-143: missing/wrong X-OMP-Contract-SHA256 refuses BEFORE the body is
    # parsed or the bearer is authenticated — a retired command type never
    # reaches discriminator validation, and nothing is written.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    operation_id = uuid4()
    retired_envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {"type": "request_closeout", "payload": {"work_id": str(uuid4())}},
    }
    missing = {
        "Authorization": "Bearer owner-token",
        "X-OMP-Workspace-ID": str(workspace_id),
    }
    response = service.client.post(
        "/v1/commands", headers=missing, json=retired_envelope
    )
    assert response.status_code == 409, response.text
    error = response.json()["error"]
    assert error["code"] == "contract_mismatch"
    assert error["diagnostics"][0] == "host contract digest: missing"
    assert error["diagnostics"][1] == f"service contract digest: {contract_sha256()}"
    assert error["diagnostics"][2] == "restart the OMP session"
    # Wrong digest: the same typed refusal, naming the stale digest.
    wrong = dict(missing, **{"X-OMP-Contract-SHA256": "0" * 64})
    response = service.client.post("/v1/commands", headers=wrong, json=retired_envelope)
    assert (
        response.status_code == 409
        and response.json()["error"]["code"] == "contract_mismatch"
    )
    assert (
        response.json()["error"]["diagnostics"][0]
        == f"host contract digest: {'0' * 64}"
    )
    # Authenticated reads refuse the same way; health probes stay exempt.
    read = service.client.get(f"/v1/workspaces/{workspace_id}/tree", headers=missing)
    assert (
        read.status_code == 409 and read.json()["error"]["code"] == "contract_mismatch"
    )
    assert service.client.get("/v1/health/live").status_code == 200
    # Nothing was written for the refused operation id: the same id with a
    # matching digest and a VALID body applies fresh — never replayed, no
    # idempotency row, no budget or event burned.
    refused_reuse = dict(
        retired_envelope,
        command=_batch([{"client_ref": "root", "title": "post-handshake item"}]),
    )
    response = service.client.post("/v1/commands", headers=missing, json=refused_reuse)
    assert response.status_code == 409
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "root", "title": "post-handshake item"}]),
        operation_id=operation_id,
    )
    assert status == 200 and body["receipt"]["state"] == "applied", body
    # Matching digest keeps ordinary behavior end to end.
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "root", "title": "ordinary item"}]),
    )
    assert status == 200 and body["result"]["items"][0]["state"] == "BACKLOG"


def test_seal_acceptance_criteria_fall_back_to_stored_verification_gates(
    service,
) -> None:
    # OMP-147 (decision 0007): no revision criteria + no `## Acceptance criteria`
    # anywhere → the plan receipt's stored verification array supplies the
    # criteria: seven gates become seven AC lines, stored order, no
    # "(none recorded)", and the section/task hashes cover exactly those bytes.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "fallback gates target")
    gates = [f"gate {index}: command {index} exits zero" for index in range(1, 8)]
    plan_body = "## Approach\n1. do the work\n\n## Verification\n" + "\n".join(
        f"{index}. {gate}" for index, gate in enumerate(gates, start=1)
    )
    plan_payload = {"body": plan_body, "verification": gates}
    plan_receipt = _receipt(
        item["work_id"],
        item["revision_id"],
        str(uuid4()),
        "plan",
        body=plan_payload,
        candidate_sha256=secrets.token_hex(32),
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": plan_receipt}},
    )
    assert status == 200, body
    plan = body["result"]["receipt"]
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task = seal["manifest"]["task_body"]
    expected_section = "\n".join(
        f"- AC-{index}: {gate}" for index, gate in enumerate(gates, start=1)
    )
    assert expected_section in task
    assert "(none recorded)" not in task
    assert task.count("- AC-") == 7
    section_hashes = (
        json.loads(seal["manifest"]["section_hashes"])
        if isinstance(seal["manifest"]["section_hashes"], str)
        else seal["manifest"]["section_hashes"]
    )
    assert section_hashes["Acceptance criteria"] == text_sha256(expected_section)
    assert seal["manifest"]["task_sha256"] == text_sha256(task)


def test_waiver_requires_failed_delivery(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "waiver target")
    # A begin refusal produces a deliverable event.
    status, body = _begin(service, workspace_id, item)
    event = body["result"]["event"]
    assert event["requires_delivery"] is True
    # Waiving before any failed delivery refuses.
    status, body = _attest(
        service, workspace_id, event, "waived", authorization_ref="waiver:cf-test"
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "waiver_requires_failed"
    )
    # Hash mismatch refuses.
    status, body = _attest(
        service, workspace_id, {**event, "rendered_sha256": secrets.token_hex(32)}
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "delivery_hash_mismatch"
    )
    # failed → waived succeeds; a second resolution refuses.
    status, body = _attest(service, workspace_id, event, "failed")
    assert status == 200 and body["result"]["status"] == "applied"
    status, body = _attest(
        service, workspace_id, event, "waived", authorization_ref="waiver:cf-test"
    )
    assert (
        status == 200
        and body["result"]["status"] == "applied"
        and body["result"]["delivery"]["status"] == "waived"
    )
    status, body = _attest(service, workspace_id, event)
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "delivery_already_resolved"
    )


def test_candidate_read_capability_is_bounded(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "bounded read")
    other = _create(service, workspace_id, "out of candidate")
    plan = _plan(service, workspace_id, item)
    _plan(service, workspace_id, other)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]

    reader = service.capabilities / "reader.json"
    reader.write_text(
        json.dumps(
            {
                "token": "reader-token",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [final["candidate_id"]],
            }
        )
    )
    reader.chmod(0o600)

    headers = {
        "Authorization": "Bearer reader-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=headers)
    assert workflow.status_code == 200
    assert workflow.json()["item"]["candidate"]["candidate_id"] == final["candidate_id"]
    assert (
        service.client.get("/v1/work-items/OMP-2/workflow", headers=headers).status_code
        == 403
    )
    assert (
        service.client.get("/v1/work-items/OMP-1", headers=headers).status_code == 403
    )
    assert (
        service.client.get(
            f"/v1/workspaces/{workspace_id}/tree", headers=headers
        ).status_code
        == 403
    )
    status, _ = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "x", "title": "nope"}]),
        token="reader-token",
    )
    assert status == 403
    # Close-ritual commands need work.close — a candidate reader has none.
    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": item["work_id"],
                "attempt_id": str(uuid4()),
                "authorization_ref": "summary:forged",
                "owner_session_id": "s",
                "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_start_commit": "e" * 40,
                "repository": "/r",
                "diff_sha256": secrets.token_hex(32),
                "starting_dirty_paths": [],
            },
        },
        token="reader-token",
    )
    assert status == 403


def test_stale_service_refuses_writes_and_still_reads(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OMP-89: once the on-disk source no longer matches the loaded snapshot,
    # every command is refused with a typed restart instruction and no side
    # effects; reads keep working so the ledger stays inspectable.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "pre-stale item")
    import omp_work.v1.server as server_module

    monkeypatch.setattr(server_module, "code_fingerprint", lambda: "deadbeef")
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "stale", "title": "must not land"}]),
    )
    assert status == 503 and body["error"]["code"] == "unavailable"
    assert any("service_stale" in diag for diag in body["error"]["diagnostics"])
    assert any("restart" in diag for diag in body["error"]["diagnostics"])
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    )
    assert workflow.status_code == 200
    monkeypatch.undo()
    status, _ = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "fresh", "title": "lands after restart-equivalent"}]),
    )
    assert status == 200


def _close_ritual(
    service,
    workspace_id,
    item: dict,
    final: dict,
    attempt: dict,
    *,
    done_ref: str | None = None,
    cancellations: list[dict] | None = None,
    operation_id=None,
) -> tuple[int, dict]:
    """Post-PASS closeout: record closeout review, drain, push, complete."""
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    _drain_deliveries(service, workspace_id, key=item["key"])
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final["commit_sha"],
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200, body
    return _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=done_ref,
        cancellations=cancellations,
        key=item["key"],
        operation_id=operation_id,
    )


def test_rider_batch_seals_audits_and_completes_with_primary(service) -> None:
    # OMP-93: riders sealed at begin ride the primary /done; their evidence is
    # rendered verbatim in the audited task body so the PASS attests them.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider_a = _create(service, workspace_id, "historical rider a")
    rider_b = _create(service, workspace_id, "historical rider b")
    item = _create(service, workspace_id, "batch primary")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    riders = [
        {
            "work_id": rider_a["work_id"],
            "revision_id": rider_a["revision_id"],
            "evidence": "probe: pytest -k rider_a -> 3 passed",
        },
        {
            "work_id": rider_b["work_id"],
            "revision_id": rider_b["revision_id"],
            "evidence": "probe: artifact b read -> present",
        },
    ]
    status, body = _begin(service, workspace_id, item, identity={"riders": riders})
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    assert len(attempt["riders"]) == 2
    assert all(r["evidence_sha256"] and r["title"] for r in attempt["riders"])
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    assert seal["manifest"]["manifest_version"] == 2
    task_body = seal["manifest"]["task_body"]
    assert "Riders (batch completion, owner ruling 2026-08-22)" in task_body
    assert "    probe: pytest -k rider_a -> 3 passed" in task_body
    assert "historical rider b" in task_body
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"]
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_id,
        json.dumps({"verdict": "PASS", "report": PASS_REPORT}),
    )
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    status, body = _close_ritual(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    assert set(body["result"]["completed_work_ids"]) == {
        rider_a["work_id"],
        rider_b["work_id"],
    }
    for rider in (rider_a, rider_b):
        view = service.client.get(
            f"/v1/work-items/{rider['key']}", headers=_owner_headers(workspace_id)
        ).json()
        assert view["state"] == "DONE", view
        workflow = service.client.get(
            f"/v1/work-items/{rider['key']}/workflow",
            headers=_owner_headers(workspace_id),
        ).json()
        provenance = [
            event
            for event in workflow["close_attempt_events"]
            if event["event_type"] == "rider_completed"
        ]
        assert provenance and "sealed rider" in provenance[0]["reason"], workflow[
            "close_attempt_events"
        ]


def test_rider_binding_refuses_wrong_revision_at_begin(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "mis-sealed rider")
    item = _create(service, workspace_id, "refusal primary")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    bad = [
        {
            "work_id": rider["work_id"],
            "revision_id": item["revision_id"],
            "evidence": "probe: n/a",
        }
    ]
    status, body = _begin(service, workspace_id, item, identity={"riders": bad})
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "rider_binding_invalid"


def test_rider_closed_elsewhere_refuses_the_batch_done(service) -> None:
    # Drift between seal and /done: the rider reaches DONE through its own
    # ritual; the batch /done must refuse rather than double-complete.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "independently closed rider")
    item = _create(service, workspace_id, "stale-batch primary")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "probe: superseded",
        }
    ]
    status, body = _begin(service, workspace_id, item, identity={"riders": riders})
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"]
    )
    assert status == 200, body
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_id,
        json.dumps({"verdict": "PASS", "report": PASS_REPORT}),
    )
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    # Close the rider through its own full ritual.
    rider_plan = _plan(service, workspace_id, rider)
    status, body = _finalize(service, workspace_id, rider, rider_plan["candidate_id"])
    assert status == 200, body
    rider_final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, rider)
    assert status == 200 and body["result"]["status"] == "applied", body
    rider_attempt = body["result"]["attempt"]
    rider_seal = _verify_and_seal(
        service, workspace_id, rider, rider_final, rider_attempt
    )
    status, body = _reserve(
        service,
        workspace_id,
        rider_attempt["attempt_id"],
        rider_seal["manifest"]["task_sha256"],
    )
    assert status == 200, body
    rider_launch = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        rider_attempt["attempt_id"],
        rider_launch,
        json.dumps({"verdict": "PASS", "report": PASS_REPORT}),
    )
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    status, body = _close_ritual(
        service, workspace_id, rider, rider_final, rider_attempt
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    # The batch /done now refuses on the sealed rider.
    status, body = _close_ritual(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "rider_binding_invalid"


def test_rider_change_under_same_authorization_refuses_resume(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "resume rider")
    item = _create(service, workspace_id, "resume primary")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    auth = f"summary:{uuid4()}"
    started_at = datetime.now(timezone.utc).isoformat()
    diff_sha = secrets.token_hex(32)
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "probe: original",
        }
    ]
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=auth,
        identity={
            "owner_session_started_at": started_at,
            "diff_sha256": diff_sha,
            "riders": riders,
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    # Replaying identical authorization returns stored begin outcome.
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=auth,
        attempt_id=attempt["attempt_id"],
        identity={
            "owner_session_started_at": started_at,
            "diff_sha256": diff_sha,
            "riders": riders,
        },
    )
    assert status == 200 and body["result"]["event"]["event_type"] == "attempt_begun", (
        body
    )
    # Changed rider evidence under the same authorization refuses with reuse conflict.
    changed = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "probe: tampered",
        }
    ]
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=auth,
        attempt_id=attempt["attempt_id"],
        identity={
            "owner_session_started_at": started_at,
            "diff_sha256": diff_sha,
            "riders": changed,
        },
    )
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "authorization_reuse_conflict"


def test_complete_work_with_cancellations_applied_and_events(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "primary item")
    target = _create(service, workspace_id, "target to cancel")

    operation_id = uuid4()
    done_ref = f"done:{uuid4()}"
    cancellations = [
        {
            "work_id": target["work_id"],
            "revision_id": target["revision_id"],
            "reason": "superseded by primary OMP-1",
        }
    ]
    status, body = _close_ritual(
        service,
        workspace_id,
        item,
        final,
        attempt,
        done_ref=done_ref,
        cancellations=cancellations,
        operation_id=operation_id,
    )
    assert body["result"]["canceled_work_ids"] == [target["work_id"]]

    # Target is CANCELED
    target_view = service.client.get(
        f"/v1/work-items/{target['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert target_view["item"]["state"] == "CANCELED"
    cancel_events = [
        e
        for e in target_view["close_attempt_events"]
        if e["event_type"] == "batch_canceled"
    ]
    assert len(cancel_events) == 1
    assert "superseded by primary OMP-1" in cancel_events[0]["reason"]

    # Operation replay returns identical result
    status, replay = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        done_ref=done_ref,
        cancellations=cancellations,
        key=item["key"],
        operation_id=operation_id,
    )
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    assert replay["result"]["canceled_work_ids"] == [target["work_id"]]


def test_complete_work_cancellation_refusals_and_rollback(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "rollback primary")
    target = _create(service, workspace_id, "target for rollback")
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        "push",
        remote_ref="refs/heads/main",
        remote_commit=final["commit_sha"],
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])
    # Self-cancellation is invalid
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        cancellations=[
            {
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "reason": "self",
            }
        ],
        key=item["key"],
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"

    # Duplicate cancellation target is invalid
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        cancellations=[
            {
                "work_id": target["work_id"],
                "revision_id": target["revision_id"],
                "reason": "r1",
            },
            {
                "work_id": target["work_id"],
                "revision_id": target["revision_id"],
                "reason": "r2",
            },
        ],
        key=item["key"],
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"

    # Drifted revision refuses transaction and leaves all unchanged
    fake_rev = str(uuid4())
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        cancellations=[
            {"work_id": target["work_id"], "revision_id": fake_rev, "reason": "stale"}
        ],
        key=item["key"],
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "cancel_binding_invalid"
    assert (
        "no longer open on the submitted revision" in body["result"]["event"]["reason"]
    )

    # Already-terminal target refuses transaction with cancel_binding_invalid
    terminal_target = _create(service, workspace_id, "terminal target")
    status, _ = _command(
        service,
        workspace_id,
        {
            "type": "set_work_state",
            "payload": {"work_id": terminal_target["work_id"], "state": "CANCELED"},
        },
    )
    assert status == 200
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        cancellations=[
            {
                "work_id": terminal_target["work_id"],
                "revision_id": terminal_target["revision_id"],
                "reason": "already canceled",
            }
        ],
        key=item["key"],
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "cancel_binding_invalid"
    # Primary and target stay open
    primary_view = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert primary_view["item"]["state"] not in ("DONE", "CANCELED")
    target_view = service.client.get(
        f"/v1/work-items/{target['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert target_view["item"]["state"] == "BACKLOG"


def test_complete_work_overlap_guards_refuse_and_mutate_nothing(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "overlap primary")
    target = _create(service, workspace_id, "overlap target")

    def _snapshot(key: str) -> dict:
        view = service.client.get(
            f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)
        ).json()
        return {
            "state": view["item"]["state"],
            "revision": view["item"]["revision"]["revision_id"],
            "terminal_events": sorted(
                e["event_id"]
                for e in view["close_attempt_events"]
                if e["event_type"] in ("work_completed", "batch_canceled")
            ),
        }

    before_primary = _snapshot(item["key"])
    before_target = _snapshot(target["key"])

    # satisfied child ∩ cancellation target: exact typed refusal, zero mutation
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        satisfied=[target["work_id"]],
        cancellations=[
            {
                "work_id": target["work_id"],
                "revision_id": target["revision_id"],
                "reason": "overlap",
            }
        ],
        key=item["key"],
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert (
        "work item cannot be both a satisfied child and a cancellation target"
        in body["error"]["diagnostics"]
    )
    assert _snapshot(item["key"]) == before_primary
    assert _snapshot(target["key"]) == before_target

    # sealed rider ∩ cancellation target: exact typed refusal, zero mutation
    rider = _create(service, workspace_id, "rider target")
    rider_primary = _create(service, workspace_id, "rider primary")
    plan = _plan(service, workspace_id, rider_primary)
    status, body = _finalize(service, workspace_id, rider_primary, plan["candidate_id"])
    assert status == 200, body
    rider_final = body["result"]["candidate"]
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "probe: rider",
        }
    ]
    status, body = _begin(
        service, workspace_id, rider_primary, identity={"riders": riders}
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    rider_attempt = body["result"]["attempt"]
    before_rider_primary = _snapshot(rider_primary["key"])
    before_rider = _snapshot(rider["key"])
    status, body = _complete(
        service,
        workspace_id,
        rider_primary,
        rider_final,
        rider_attempt["attempt_id"],
        cancellations=[
            {
                "work_id": rider["work_id"],
                "revision_id": rider["revision_id"],
                "reason": "overlap",
            }
        ],
        key=rider_primary["key"],
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert (
        "work item cannot be both a sealed rider and a cancellation target"
        in body["error"]["diagnostics"]
    )
    assert _snapshot(rider_primary["key"]) == before_rider_primary
    assert _snapshot(rider["key"]) == before_rider

    # Both cancel probes remain open
    assert _snapshot(target["key"])["state"] == "BACKLOG"
    assert _snapshot(rider["key"])["state"] == "BACKLOG"


def test_closeout_refused_before_checkpoint_attestation_and_succeeds_after(
    service,
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "gated item")

    # Before delivery is attested, record_closeout_review is refused with delivery_pending
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "delivery_pending"

    # Attest pending deliveries
    _drain_deliveries(service, workspace_id)

    # After delivery is attested, record_closeout_review succeeds and atomically transitions attempt to closeout_requested
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["attempt"]["state"] == "closeout_requested"


def test_summary_authorization_resume_audited_and_closeout_requested(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "resume audited")

    # A new authorization token with matching identity resumes the audited attempt
    new_auth = f"summary:{uuid4()}"
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=new_auth,
        identity={"diff_sha256": attempt["diff_sha256"]},
    )
    assert status == 200 and body["result"]["status"] == "applied"
    resumed = body["result"]["attempt"]
    assert resumed["attempt_id"] == attempt["attempt_id"]
    assert resumed["state"] == "audited"
    assert body["result"]["event"]["event_type"] == "attempt_resumed"

    # Mismatched identity against audited attempt refuses finished_attempt_identity_mismatch and does NOT supersede
    mismatched_auth = f"summary:{uuid4()}"
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=mismatched_auth,
        identity={"diff_sha256": secrets.token_hex(32)},
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert (
        body["result"]["event"]["reason_code"] == "finished_attempt_identity_mismatch"
    )

    view = service.client.get(
        "/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)
    ).json()
    live = [
        a
        for a in view["close_attempts"]
        if a["state"]
        in (
            "active",
            "audit_ready",
            "auditor_in_flight",
            "audited",
            "closeout_requested",
        )
    ]
    assert (
        len(live) == 1
        and live[0]["attempt_id"] == attempt["attempt_id"]
        and live[0]["state"] == "audited"
    )

    # Advance to closeout_requested
    _drain_deliveries(service, workspace_id)
    status, body = _record_review(
        service, workspace_id, item, final, attempt, authorization_ref=new_auth
    )
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["state"] == "closeout_requested"

    # A fresh authorization token against closeout_requested attempt resumes without demoting state
    fresh_auth = f"summary:{uuid4()}"
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=fresh_auth,
        identity={"diff_sha256": attempt["diff_sha256"]},
    )
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] == attempt["attempt_id"]
    assert body["result"]["attempt"]["state"] == "closeout_requested"
    assert body["result"]["event"]["event_type"] == "attempt_resumed"

    # Mismatched identity against closeout_requested also refuses finished_attempt_identity_mismatch
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=f"summary:{uuid4()}",
        identity={"diff_sha256": secrets.token_hex(32)},
    )
    assert status == 200 and body["result"]["status"] == "refused"
    assert (
        body["result"]["event"]["reason_code"] == "finished_attempt_identity_mismatch"
    )


def test_resume_with_omitted_riders_retains_sealed_riders(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "rider for omit test")
    item = _create(service, workspace_id, "primary for omit test")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    initial_auth = f"summary:{uuid4()}"
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "probe: sealed",
        }
    ]
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=initial_auth,
        identity={"riders": riders},
    )
    assert status == 200 and body["result"]["status"] == "applied"
    attempt = body["result"]["attempt"]
    assert len(attempt["riders"]) == 1

    # Resume with empty riders list retains the sealed riders
    resume_auth = f"summary:{uuid4()}"
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=resume_auth,
        identity={"diff_sha256": attempt["diff_sha256"], "riders": []},
    )
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] == attempt["attempt_id"]
    assert len(body["result"]["attempt"]["riders"]) == 1
    assert body["result"]["event"]["event_type"] == "attempt_resumed"


def test_terminal_work_authorization_refused(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "terminal work item")
    status, body = _close_ritual(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["state"] == "DONE"

    # A fresh authorization token against completed work refuses work_terminal
    fresh_auth = f"summary:{uuid4()}"
    status, body = _begin(service, workspace_id, item, authorization_ref=fresh_auth)
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "work_terminal"

    # A replayed authorization token returns stored outcome without mutating
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=attempt["authorization_ref"],
        identity={"diff_sha256": attempt["diff_sha256"]},
    )
    assert status == 200 and body["result"]["status"] == "applied"


def test_duplicate_title_rejection(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    # Create initial projectless item
    first = _create(service, workspace_id, "Fix the auth flow")
    assert first["key"] == "OMP-1"

    # Case & whitespace variation is rejected
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "dup", "title": "  fix   THE  auth flow  "}]),
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert (
        f'duplicate open title "fix   THE  auth flow" matches {first["key"]}'
        in body["error"]["diagnostics"]
    )

    # Intra-batch duplicate is rejected
    status, body = _command(
        service,
        workspace_id,
        _batch(
            [
                {"client_ref": "b1", "title": "Build feature X"},
                {"client_ref": "b2", "title": "build feature x"},
            ]
        ),
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert (
        'duplicate open title "build feature x" matches OMP-2'
        in body["error"]["diagnostics"]
    )

    # Different projects allow same title
    proj1 = str(uuid4())
    proj2 = str(uuid4())
    _seed_project(service, workspace_id, proj1, "Project 1")
    _seed_project(service, workspace_id, proj2, "Project 2")

    p1_item = _create(service, workspace_id, "Shared Title", project_id=proj1)
    p2_item = _create(service, workspace_id, "Shared Title", project_id=proj2)
    assert p1_item["key"] != p2_item["key"]

    # Duplicate in same project is rejected
    status, body = _command(
        service,
        workspace_id,
        _batch(
            [{"client_ref": "p1_dup", "title": "shared title", "project_id": proj1}]
        ),
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert (
        f'duplicate open title "shared title" matches {p1_item["key"]}'
        in body["error"]["diagnostics"]
    )

    # Closed or canceled item's title can be reused
    cancellable = _create(service, workspace_id, "To be canceled")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_work_state",
            "payload": {"work_id": cancellable["work_id"], "state": "CANCELED"},
        },
    )
    assert status == 200, body

    reused = _create(service, workspace_id, "to be canceled")
    assert reused["key"] != cancellable["key"]
    # Archived open non-terminal item still blocks duplicate
    archived_open = _create(service, workspace_id, "Archived but still open")
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OWNER)),
            )
            cur.execute(
                "UPDATE omp_work.work_items SET archived = true WHERE work_id = %s",
                (archived_open["work_id"],),
            )
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "dup_arch", "title": "archived but still open"}]),
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert (
        f'duplicate open title "archived but still open" matches {archived_open["key"]}'
        in body["error"]["diagnostics"]
    )


def _tcb_manifest():
    fp = service_runtime_fingerprint()
    manifest = {
        "auditor_agent_sha256": "a" * 64,
        "host_sha256": "b" * 64,
        "adapter_sha256": "c" * 64,
        "freeze_sha256": "d" * 64,
        "runner_sha256": "e" * 64,
        "executor_sha256": "f" * 64,
        "contract_sha256": contract_sha256(),
        "service_fingerprint": fp,
        "service_code_fingerprint": fp,
        "service_migration_sha256": fp,
    }
    return sha256(manifest), manifest


def test_execution_grant_lifecycle_pass(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(
        service,
        workspace_id,
        "Build execution feature",
        description="The request description",
    )
    work_id = item["work_id"]
    rev_id = item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": "/execute OMP-1",
        "workspace_id": str(workspace_id),
        "repository": "oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "The request description",
                        "original_request_sha256": text_sha256(
                            "The request description"
                        ),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "active"
    assert body["result"]["items"][0]["phase"] == "criteria_pending"

    # Seal criteria
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: criteria one", "AC-2: criteria two"],
                "description_sha256": text_sha256("The request description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "planning"
    new_rev_id = body["result"]["revision"]["revision_id"]

    # Stamp plan
    candidate_id = str(uuid4())
    plan_content = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://execute-omp-1-plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "executing"
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]
    # Finalize & Push receipt
    final_commit = "1" * 40
    final_cand_id = str(uuid4())
    final_cand_sha = "2" * 64
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        candidate_id,
        commit=final_commit,
        final_id=final_cand_id,
        candidate_hash=final_cand_sha,
    )

    push_receipt_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": push_receipt_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_cand_id),
                    "kind": "push",
                    "payload": {
                        "repository": "oh-my-pi",
                        "remote_url": "git@github.com:owner/oh-my-pi.git",
                        "remote_ref": "refs/heads/main",
                        "prior_tip": head_commit,
                        "candidate_commit": final_commit,
                        "result_tip": final_commit,
                    },
                    "payload_sha256": sha256(
                        {
                            "repository": "oh-my-pi",
                            "remote_url": "git@github.com:owner/oh-my-pi.git",
                            "remote_ref": "refs/heads/main",
                            "prior_tip": head_commit,
                            "candidate_commit": final_commit,
                            "result_tip": final_commit,
                        }
                    ),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_sha,
                    "candidate_commit": final_commit,
                    "remote_ref": "refs/heads/main",
                    "remote_commit": final_commit,
                },
            },
        },
    )
    assert status == 200, body

    # Close attempt
    attempt_id = str(uuid4())
    status, begin_body = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": attempt_id,
                "authorization_ref": f"execution:{grant_id}:0:1",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_start_commit": head_commit,
                "repository": "oh-my-pi",
                "diff_sha256": "3" * 64,
                "starting_dirty_paths": [],
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one", "AC-2: criteria two"]),
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": judge_sha,
                "riders": [],
            },
        },
    )
    assert status == 200, begin_body
    begin_event = begin_body["result"]["event"]

    verif_receipt_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": verif_receipt_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_cand_id),
                    "kind": "verification",
                    "payload": {"body": "tests passed"},
                    "payload_sha256": sha256({"body": "tests passed"}),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_sha,
                    "candidate_commit": final_commit,
                },
            },
        },
    )
    assert status == 200, body

    # Seal manifest
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_audit_manifest",
            "payload": {
                "attempt_id": attempt_id,
                "verification_receipt_id": verif_receipt_id,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["manifest"]["manifest_version"] == 3
    task_sha = body["result"]["manifest"]["task_sha256"]

    # Launch and PASS
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "reserve_auditor_launch",
            "payload": {
                "attempt_id": attempt_id,
                "task_sha256": task_sha,
                "tool_call_id": "call-1",
            },
        },
    )
    assert status == 200, body
    launch_id = body["result"]["launch"]["launch_id"]

    status, settle_body = _command(
        service,
        workspace_id,
        {
            "type": "settle_auditor_launch",
            "payload": {
                "attempt_id": attempt_id,
                "launch_id": launch_id,
                "transport_payload": PASS_REPORT,
            },
        },
    )
    assert status == 200, settle_body
    assert settle_body["result"]["verdict"] == "PASS"
    settle_event = settle_body["result"]["event"]

    # Attest deliveries before completion
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": begin_event["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": begin_event["rendered_sha256"],
                "status": "delivered",
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": settle_event["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": settle_event["rendered_sha256"],
                "status": "delivered",
            },
        },
    )
    # Complete execution item
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "attempt_id": attempt_id,
                "push_receipt_id": push_receipt_id,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["state"] == "DONE"
    assert body["result"]["grant"]["state"] == "completed"


def test_seal_preserves_existing_criteria_verbatim(service) -> None:
    """Items carrying acceptance criteria seal them verbatim: the caller's
    derived proposal is discarded (sessions cannot reproduce stored bytes),
    no replacement revision is created, and the result reports the
    authoritative revision."""
    workspace_id = uuid4()
    _grant(service, workspace_id)

    existing = ["AC-1: stored criterion", "AC-2: exact bytes \u2014 kept"]
    item = _create(
        service,
        workspace_id,
        "Sealed criteria item",
        description="Do the thing",
        acceptance_criteria=existing,
    )
    work_id, rev_id = item["work_id"], item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": "/execute OMP-1",
        "workspace_id": str(workspace_id),
        "repository": "oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "Do the thing",
                        "original_request_sha256": text_sha256("Do the thing"),
                        "initial_git_baseline": "0" * 40,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body

    # A mismatched derived proposal must seal the stored criteria verbatim.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: a guessed paraphrase"],
                "description_sha256": text_sha256("Do the thing"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    result = body["result"]
    assert result["item"]["phase"] == "planning"
    assert result["revision"]["acceptance_criteria"] == existing
    assert result["revision"]["revision_id"] == str(rev_id), "no replacement revision"
    assert result["item"]["criteria_revision_id"] == str(rev_id)
    assert result["item"]["criteria_sha256"] == sha256(existing)


@pytest.mark.parametrize("report_payload", [NEEDS_FIX_REPORT, BLOCKED_REPORT])
def test_execution_grant_no_progress_cap(service, report_payload) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "No progress test item", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-1",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )

    status, seal_body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: test"],
                "description_sha256": text_sha256("desc"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, seal_body
    new_rev_id = seal_body["result"]["revision"]["revision_id"]

    cand_id = str(uuid4())
    _, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": cand_id,
                "plan_file": "local://p.md",
                "plan_body": "## Approach\n1. a\n\n## Verification\n1. v",
                "plan_sha256": sha256("## Approach\n1. a\n\n## Verification\n1. v"),
                "approach": ["1. a"],
                "verification": ["1. v"],
                "paths": ["a.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert body["result"]["item"]["phase"] == "executing"
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]
    final_cand_sha = "f" * 64
    final_commit = "c" * 40
    final_id = str(uuid4())
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        cand_id,
        commit=final_commit,
        final_id=final_id,
        candidate_hash=final_cand_sha,
    )
    for attempt_num in range(1, 4):
        att_id = str(uuid4())
        _command(
            service,
            workspace_id,
            {
                "type": "begin_close_attempt",
                "payload": {
                    "work_id": str(work_id),
                    "attempt_id": att_id,
                    "authorization_ref": f"execution:{grant_id}:0:{attempt_num}",
                    "owner_session_id": "session-1",
                    "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                    "owner_session_start_commit": head_commit,
                    "repository": "oh-my-pi",
                    "diff_sha256": "d" * 64,
                    "starting_dirty_paths": [],
                    "authorization_kind": "execution",
                    "execution_grant_id": grant_id,
                    "candidate_tree_sha": final_cand_sha,
                    "original_request_sha256": text_sha256("desc"),
                    "criteria_sha256": sha256(["AC-1: test"]),
                    "plan_stamp_sha256": plan_stamp_sha,
                    "judge_sha256": judge_sha,
                    "riders": [],
                },
            },
        )
        v_id = str(uuid4())
        _command(
            service,
            workspace_id,
            {
                "type": "append_evidence",
                "payload": {
                    "receipt": {
                        "receipt_id": v_id,
                        "work_id": str(work_id),
                        "revision_id": str(new_rev_id),
                        "candidate_id": str(final_id),
                        "kind": "verification",
                        "payload": {"body": "v"},
                        "payload_sha256": sha256({"body": "v"}),
                        "issuer": "test",
                        "issued_at": datetime.now(timezone.utc).isoformat(),
                        "candidate_sha256": final_cand_sha,
                        "candidate_commit": final_commit,
                    },
                },
            },
        )
        _, m_body = _command(
            service,
            workspace_id,
            {
                "type": "seal_audit_manifest",
                "payload": {"attempt_id": att_id, "verification_receipt_id": v_id},
            },
        )
        _, l_body = _command(
            service,
            workspace_id,
            {
                "type": "reserve_auditor_launch",
                "payload": {
                    "attempt_id": att_id,
                    "task_sha256": m_body["result"]["manifest"]["task_sha256"],
                    "tool_call_id": f"c-{attempt_num}",
                },
            },
        )
        _, s_body = _command(
            service,
            workspace_id,
            {
                "type": "settle_auditor_launch",
                "payload": {
                    "attempt_id": att_id,
                    "launch_id": l_body["result"]["launch"]["launch_id"],
                    "transport_payload": report_payload,
                },
            },
        )
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["grant"]["state"] == "stopped"
    assert resp.json()["grant"]["terminal_reason"] == "max_no_progress_exceeded"


def test_execution_grant_lifecycle_blocked_remediation(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(
        service,
        workspace_id,
        "Build execution feature",
        description="The request description",
    )
    work_id = item["work_id"]
    rev_id = item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": "/execute OMP-1",
        "workspace_id": str(workspace_id),
        "repository": "oh-my-pi",
        "nonce": str(uuid4()),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": provenance,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "The request description",
                        "original_request_sha256": text_sha256(
                            "The request description"
                        ),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body

    # Seal criteria
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: criteria one"],
                "description_sha256": text_sha256("The request description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]

    # Stamp plan 1
    cand_1_id = str(uuid4())
    plan_content_1 = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": cand_1_id,
                "plan_file": "local://execute-omp-1-plan.md",
                "plan_body": plan_content_1,
                "plan_sha256": sha256(plan_content_1),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    plan_stamp_sha_1 = body["result"]["item"]["plan_stamp_sha256"]

    # Finalize candidate 1
    final_commit_1 = "1" * 40
    final_cand_1_id = str(uuid4())
    final_cand_1_sha = "2" * 64
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        cand_1_id,
        commit=final_commit_1,
        final_id=final_cand_1_id,
        candidate_hash=final_cand_1_sha,
    )

    # Begin close attempt 1
    att_1_id = str(uuid4())
    status, begin_body_1 = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": att_1_id,
                "authorization_ref": f"execution:{grant_id}:0:1",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_start_commit": head_commit,
                "repository": "oh-my-pi",
                "diff_sha256": "3" * 64,
                "starting_dirty_paths": [],
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_1_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one"]),
                "plan_stamp_sha256": plan_stamp_sha_1,
                "judge_sha256": judge_sha,
                "riders": [],
            },
        },
    )
    assert status == 200, begin_body_1
    begin_event_1 = begin_body_1["result"]["event"]

    verif_receipt_1_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": verif_receipt_1_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_cand_1_id),
                    "kind": "verification",
                    "payload": {"body": "tests 1"},
                    "payload_sha256": sha256({"body": "tests 1"}),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_1_sha,
                    "candidate_commit": final_commit_1,
                },
            },
        },
    )
    assert status == 200, body

    # Seal manifest 1
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_audit_manifest",
            "payload": {
                "attempt_id": att_1_id,
                "verification_receipt_id": verif_receipt_1_id,
            },
        },
    )
    assert status == 200, body
    task_sha_1 = body["result"]["manifest"]["task_sha256"]

    # Reserve launch 1
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "reserve_auditor_launch",
            "payload": {
                "attempt_id": att_1_id,
                "task_sha256": task_sha_1,
                "tool_call_id": "call-1",
            },
        },
    )
    assert status == 200, body
    launch_1_id = body["result"]["launch"]["launch_id"]

    # Settle launch 1 with BLOCKED_REPORT
    status, settle_body_1 = _command(
        service,
        workspace_id,
        {
            "type": "settle_auditor_launch",
            "payload": {
                "attempt_id": att_1_id,
                "launch_id": launch_1_id,
                "transport_payload": BLOCKED_REPORT,
            },
        },
    )
    assert status == 200, settle_body_1
    assert settle_body_1["result"]["verdict"] == "BLOCKED"
    settle_event_1 = settle_body_1["result"]["event"]

    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": begin_event_1["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": begin_event_1["rendered_sha256"],
                "status": "delivered",
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": settle_event_1["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": settle_event_1["rendered_sha256"],
                "status": "delivered",
            },
        },
    )

    # Verify active execution grant and remediating phase
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200, resp.json()
    exec_data = resp.json()
    assert exec_data["grant"]["state"] == "active"
    assert exec_data["items"][0]["phase"] == "remediating"
    assert exec_data["items"][0]["consecutive_no_progress"] == 1

    # Remediation: Stamp updated plan 2
    cand_2_id = str(uuid4())
    plan_content_2 = "## Approach\n1. Step one fixed\n\n## Verification\n1. Check one fixed"
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": cand_2_id,
                "plan_file": "local://execute-omp-1-plan.md",
                "plan_body": plan_content_2,
                "plan_sha256": sha256(plan_content_2),
                "approach": ["1. Step one fixed"],
                "verification": ["1. Check one fixed"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "4" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "executing"
    plan_stamp_sha_2 = body["result"]["item"]["plan_stamp_sha256"]

    # Finalize candidate 2 & Push receipt
    final_commit_2 = "5" * 40
    final_cand_2_id = str(uuid4())
    final_cand_2_sha = "6" * 64
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        cand_2_id,
        commit=final_commit_2,
        final_id=final_cand_2_id,
        candidate_hash=final_cand_2_sha,
    )

    push_receipt_2_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": push_receipt_2_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_cand_2_id),
                    "kind": "push",
                    "payload": {
                        "repository": "oh-my-pi",
                        "remote_url": "git@github.com:owner/oh-my-pi.git",
                        "remote_ref": "refs/heads/main",
                        "prior_tip": final_commit_1,
                        "candidate_commit": final_commit_2,
                        "result_tip": final_commit_2,
                    },
                    "payload_sha256": sha256(
                        {
                            "repository": "oh-my-pi",
                            "remote_url": "git@github.com:owner/oh-my-pi.git",
                            "remote_ref": "refs/heads/main",
                            "prior_tip": final_commit_1,
                            "candidate_commit": final_commit_2,
                            "result_tip": final_commit_2,
                        }
                    ),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_2_sha,
                    "candidate_commit": final_commit_2,
                    "remote_ref": "refs/heads/main",
                    "remote_commit": final_commit_2,
                },
            },
        },
    )
    assert status == 200, body

    # Close attempt 2
    att_2_id = str(uuid4())
    status, begin_body_2 = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": att_2_id,
                "authorization_ref": f"execution:{grant_id}:0:2",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_start_commit": final_commit_1,
                "repository": "oh-my-pi",
                "diff_sha256": "7" * 64,
                "starting_dirty_paths": [],
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_2_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one"]),
                "plan_stamp_sha256": plan_stamp_sha_2,
                "judge_sha256": judge_sha,
                "riders": [],
            },
        },
    )
    assert status == 200, begin_body_2
    begin_event_2 = begin_body_2["result"]["event"]

    verif_receipt_2_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": verif_receipt_2_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_cand_2_id),
                    "kind": "verification",
                    "payload": {"body": "tests 2 passed"},
                    "payload_sha256": sha256({"body": "tests 2 passed"}),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_2_sha,
                    "candidate_commit": final_commit_2,
                },
            },
        },
    )
    assert status == 200, body

    # Seal manifest 2
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "seal_audit_manifest",
            "payload": {
                "attempt_id": att_2_id,
                "verification_receipt_id": verif_receipt_2_id,
            },
        },
    )
    assert status == 200, body
    task_sha_2 = body["result"]["manifest"]["task_sha256"]

    # Launch 2 and PASS
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "reserve_auditor_launch",
            "payload": {
                "attempt_id": att_2_id,
                "task_sha256": task_sha_2,
                "tool_call_id": "call-2",
            },
        },
    )
    assert status == 200, body
    launch_2_id = body["result"]["launch"]["launch_id"]

    status, settle_body_2 = _command(
        service,
        workspace_id,
        {
            "type": "settle_auditor_launch",
            "payload": {
                "attempt_id": att_2_id,
                "launch_id": launch_2_id,
                "transport_payload": PASS_REPORT,
            },
        },
    )
    assert status == 200, settle_body_2
    assert settle_body_2["result"]["verdict"] == "PASS"
    settle_event_2 = settle_body_2["result"]["event"]

    # Verify phase is reviewing
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200, resp.json()
    assert resp.json()["items"][0]["phase"] == "reviewing"
    assert resp.json()["items"][0]["consecutive_no_progress"] == 0

    # Attest deliveries before completion
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": begin_event_2["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": begin_event_2["rendered_sha256"],
                "status": "delivered",
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": settle_event_2["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": settle_event_2["rendered_sha256"],
                "status": "delivered",
            },
        },
    )

    # Complete execution item
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 4,
                "work_id": str(work_id),
                "attempt_id": att_2_id,
                "push_receipt_id": push_receipt_2_id,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["state"] == "DONE"
    assert body["result"]["grant"]["state"] == "completed"


def test_execution_grant_continuation_cap(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Continuation cap test item", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-1",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )

    # Schedule continuations 1 through 8
    for i in range(1, 9):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "set_execution_state",
                "payload": {
                    "grant_id": grant_id,
                    "target_state": "active",
                    "expected_grant_version": i,
                    "judge_sha256": judge_sha,
                },
            },
        )
        assert status == 200, body
        assert body["result"]["grant"]["state"] == "active"
        assert body["result"]["grant"]["continuations_scheduled"] == i
        assert body["result"]["grant"]["grant_version"] == i + 1

    # 9th continuation exceeds cap of 8 -> atomically returns stopped
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "active",
                "expected_grant_version": 9,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "stopped"
    assert body["result"]["grant"]["terminal_reason"] == "max_continuations_exceeded"


def test_execution_grant_pause_resume_contract_approval(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Contract approval test item", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-1",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )

    # Pause for contract approval
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "paused",
                "expected_grant_version": 1,
                "reason": "contract_approval_required: contract hash mismatch",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "paused"
    assert body["result"]["grant"]["paused_at"] is not None

    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    )
    assert resp.json()["items"][0]["phase"] == "awaiting_contract_approval"

    # Resume returns to active and item returns to planning
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "active",
                "expected_grant_version": 2,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "active"
    assert body["result"]["grant"]["paused_at"] is None

    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    )
    assert resp.json()["items"][0]["phase"] == "planning"

    # Pause and then cancel
    _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "paused",
                "expected_grant_version": 3,
                "judge_sha256": judge_sha,
            },
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "canceled",
                "expected_grant_version": 4,
                "reason": "owner_canceled",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "canceled"
    assert body["result"]["grant"]["paused_at"] is None
    assert body["result"]["grant"]["canceled_at"] is not None


def test_execution_grant_completion_push_binding_enforcement(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Push binding test item", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-1",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )

    status, seal_body = _command(
        service,
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(work_id),
                "expected_revision_id": str(rev_id),
                "criteria": ["AC-1: test"],
                "description_sha256": text_sha256("desc"),
                "judge_sha256": judge_sha,
            },
        },
    )
    new_rev_id = seal_body["result"]["revision"]["revision_id"]
    cand_id = str(uuid4())
    _, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(new_rev_id),
                "candidate_id": cand_id,
                "plan_file": "local://p.md",
                "plan_body": "## Approach\n1. a\n\n## Verification\n1. v",
                "plan_sha256": sha256("p"),
                "approach": ["1. a"],
                "verification": ["1. v"],
                "paths": ["a.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]
    final_cand_sha = "f" * 64
    final_commit = "c" * 40
    final_id = str(uuid4())
    _finalize(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        cand_id,
        commit=final_commit,
        final_id=final_id,
        candidate_hash=final_cand_sha,
    )
    att_id = str(uuid4())
    status, begin_body = _command(
        service,
        workspace_id,
        {
            "type": "begin_close_attempt",
            "payload": {
                "work_id": str(work_id),
                "attempt_id": att_id,
                "authorization_ref": f"execution:{grant_id}:0:1",
                "owner_session_id": "session-1",
                "owner_session_started_at": datetime.now(timezone.utc).isoformat(),
                "owner_session_start_commit": head_commit,
                "repository": "oh-my-pi",
                "diff_sha256": "d" * 64,
                "starting_dirty_paths": [],
                "authorization_kind": "execution",
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_sha,
                "original_request_sha256": text_sha256("desc"),
                "criteria_sha256": sha256(["AC-1: test"]),
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": judge_sha,
                "riders": [],
            },
        },
    )
    assert status == 200, begin_body
    begin_event = begin_body["result"]["event"]

    v_id = str(uuid4())
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": v_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_id),
                    "kind": "verification",
                    "payload": {"body": "v"},
                    "payload_sha256": sha256({"body": "v"}),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_sha,
                    "candidate_commit": final_commit,
                },
            },
        },
    )
    assert status == 200, body

    status, m_body = _command(
        service,
        workspace_id,
        {
            "type": "seal_audit_manifest",
            "payload": {"attempt_id": att_id, "verification_receipt_id": v_id},
        },
    )
    assert status == 200, m_body

    status, l_body = _command(
        service,
        workspace_id,
        {
            "type": "reserve_auditor_launch",
            "payload": {
                "attempt_id": att_id,
                "task_sha256": m_body["result"]["manifest"]["task_sha256"],
                "tool_call_id": "c-1",
            },
        },
    )
    assert status == 200, l_body

    status, settle_body = _command(
        service,
        workspace_id,
        {
            "type": "settle_auditor_launch",
            "payload": {
                "attempt_id": att_id,
                "launch_id": l_body["result"]["launch"]["launch_id"],
                "transport_payload": PASS_REPORT,
            },
        },
    )
    assert status == 200, settle_body
    assert settle_body["result"]["verdict"] == "PASS"
    settle_event = settle_body["result"]["event"]

    # Attest begin delivery first, leaving settle delivery pending
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": begin_event["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": begin_event["rendered_sha256"],
                "status": "delivered",
            },
        },
    )

    # Push receipt with mismatched remote commit fails
    bad_push_id = str(uuid4())
    _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": bad_push_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_id),
                    "kind": "push",
                    "payload": {
                        "remote_ref": "refs/heads/main",
                        "remote_commit": "bad" + "0" * 37,
                    },
                    "payload_sha256": sha256({"remote_commit": "bad" + "0" * 37}),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_sha,
                    "candidate_commit": final_commit,
                    "remote_ref": "refs/heads/main",
                    "remote_commit": "bad" + "0" * 37,
                },
            },
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "attempt_id": att_id,
                "push_receipt_id": bad_push_id,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "completion_blocked"

    # Negative probes for push bindings
    for field_name, bad_value, expected_msg in [
        ("repository", "other-repo", "push receipt repository mismatch"),
        ("remote_ref", "invalid-tag-ref", "push receipt remote_ref mismatch"),
        ("remote_ref", "refs/tags/v1.0", "push receipt remote_ref mismatch"),
        ("prior_tip", "f" * 40, "push receipt prior_tip mismatch"),
        ("candidate_commit", "f" * 40, "push receipt candidate_commit mismatch"),
        ("result_tip", "f" * 40, "push receipt result_tip mismatch"),
    ]:
        bad_payload = {
            "repository": "oh-my-pi",
            "remote_url": "git@github.com:owner/oh-my-pi.git",
            "remote_ref": "refs/heads/main",
            "prior_tip": head_commit,
            "candidate_commit": final_commit,
            "result_tip": final_commit,
        }
        bad_payload[field_name] = bad_value
        bad_id = str(uuid4())
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "append_evidence",
                "payload": {
                    "receipt": {
                        "receipt_id": bad_id,
                        "work_id": str(work_id),
                        "revision_id": str(new_rev_id),
                        "candidate_id": str(final_id),
                        "kind": "push",
                        "payload": bad_payload,
                        "payload_sha256": sha256(bad_payload),
                        "issuer": "test",
                        "issued_at": datetime.now(timezone.utc).isoformat(),
                        "candidate_sha256": final_cand_sha,
                        "candidate_commit": final_commit,
                        "remote_ref": bad_payload["remote_ref"],
                        "remote_commit": final_commit,
                    },
                },
            },
        )
        assert status == 200, body
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "complete_execution_item",
                "payload": {
                    "grant_id": grant_id,
                    "expected_grant_version": 3,
                    "work_id": str(work_id),
                    "attempt_id": att_id,
                    "push_receipt_id": bad_id,
                    "judge_sha256": judge_sha,
                },
            },
        )
        assert status == 409, body
        assert body["error"]["code"] == "completion_blocked"
        assert expected_msg in body["error"]["diagnostics"][0]

    # Negative probe: attempt candidate_tree_sha is immutable
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(uuid4())),
            )
            with pytest.raises(psycopg.Error, match="close attempt identity is immutable"):
                cur.execute(
                    "UPDATE omp_work.close_attempts SET candidate_tree_sha=%s WHERE workspace_id=%s AND attempt_id=%s",
                    ("0" * 64, workspace_id, att_id),
                )
    # Push receipt with matching full bindings
    good_push_id = str(uuid4())
    good_push_payload = {
        "repository": "oh-my-pi",
        "remote_url": "git@github.com:owner/oh-my-pi.git",
        "remote_ref": "refs/heads/main",
        "prior_tip": head_commit,
        "candidate_commit": final_commit,
        "result_tip": final_commit,
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "append_evidence",
            "payload": {
                "receipt": {
                    "receipt_id": good_push_id,
                    "work_id": str(work_id),
                    "revision_id": str(new_rev_id),
                    "candidate_id": str(final_id),
                    "kind": "push",
                    "payload": good_push_payload,
                    "payload_sha256": sha256(good_push_payload),
                    "issuer": "test",
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                    "candidate_sha256": final_cand_sha,
                    "candidate_commit": final_commit,
                    "remote_ref": "refs/heads/main",
                    "remote_commit": final_commit,
                },
            },
        },
    )
    assert status == 200, body

    # Prove pending delivery blocks completion
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "attempt_id": att_id,
                "push_receipt_id": good_push_id,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "completion_blocked"
    assert "delivery_pending" in body["error"]["diagnostics"][0]

    # Attest remaining delivery -> completion succeeds
    _command(
        service,
        workspace_id,
        {
            "type": "attest_checkpoint_delivery",
            "payload": {
                "event_id": settle_event["event_id"],
                "owner_session_id": "session-1",
                "rendered_sha256": settle_event["rendered_sha256"],
                "status": "delivered",
            },
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "attempt_id": att_id,
                "push_receipt_id": good_push_id,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["state"] == "DONE"
    assert body["result"]["grant"]["state"] == "completed"

def test_execution_grant_project_drift_rejection(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Item without project", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    project_id = uuid4()
    _seed_project(service, workspace_id, project_id, "Test Project")
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    # 1. Reject begin_execution when claim has project_id but work item has none (null-to-project mismatch)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": str(uuid4()),
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-1",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                        "project_id": str(project_id),
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 409
    assert any("project mismatch" in d for d in body["error"]["diagnostics"])

    # 2. Reject begin_execution when claim has None but work item has a project
    proj_item = _create(service, workspace_id, "Item with project", description="desc", project_id=str(project_id))
    proj_work_id, proj_rev_id = proj_item["work_id"], proj_item["revision_id"]
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": str(uuid4()),
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-1",
                    "normalized_command": "/execute OMP-2",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(proj_work_id),
                        "revision_id": str(proj_rev_id),
                        "position": 0,
                        "original_request": "desc",
                        "original_request_sha256": text_sha256("desc"),
                        "initial_git_baseline": head_commit,
                        "project_id": None,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 409
    assert any("project mismatch" in d for d in body["error"]["diagnostics"])
