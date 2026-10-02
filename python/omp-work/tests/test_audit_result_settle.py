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
from omp_work.v1.audit_result import render_audit_result
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    AuditCriterionCheck,
    AuditFinding,
    AuditFindingLocation,
    AuditResult,
    EvidenceManifest,
    EvidenceReference,
)
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

OWNER = uuid4()


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
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
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


def _build_completion_evidence_from_view(
    view: dict, push_receipt_id: str | None = None
) -> dict:
    cand = view["item"].get("candidate") or {
        "candidate_id": str(uuid4()),
        "candidate_sha256": "0" * 64,
        "commit_sha": "0" * 40,
    }
    manifest = view.get("audit_manifest") or {
        "manifest_id": str(uuid4()),
        "manifest_version": 1,
        "verification_receipt_id": str(uuid4()),
        "task_sha256": "0" * 64,
        "attempt_id": str(uuid4()),
    }
    receipts = view.get("receipts") or []
    attempts = view.get("close_attempts") or []
    attempt = next(
        (a for a in attempts if str(a["attempt_id"]) == str(manifest["attempt_id"])),
        attempts[0] if attempts else {},
    )

    verif_receipt = next(
        (
            r
            for r in receipts
            if str(r["receipt_id"]) == str(manifest["verification_receipt_id"])
        ),
        None,
    )
    if verif_receipt is None:
        verif_receipt = next(
            (r for r in receipts if r["kind"] == "verification"), None
        )
    if verif_receipt is None:
        verif_receipt = {
            "receipt_id": str(uuid4()),
            "kind": "verification",
            "payload_sha256": "0" * 64,
            "artifact_sha256": None,
        }

    audit_receipt = next(
        (r for r in receipts if r["kind"] == "audit" and r["verdict"] == "PASS"),
        None,
    )
    if audit_receipt is None:
        audit_receipt = next((r for r in receipts if r["kind"] == "audit"), None)
    if audit_receipt is None:
        audit_receipt = {
            "receipt_id": str(uuid4()),
            "kind": "audit",
            "payload": {},
            "payload_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
        }

    audit_payload = audit_receipt.get("payload") or {}
    if isinstance(audit_payload, str):
        try:
            audit_payload = json.loads(audit_payload)
        except Exception:
            audit_payload = {}
    launch_id = audit_payload.get("launch_id", str(uuid4()))
    launches = view.get("auditor_launches") or []
    launch = next(
        (l for l in launches if str(l["launch_id"]) == str(launch_id)),
        launches[0]
        if launches
        else {
            "launch_id": launch_id,
            "tool_call_id": "call_default",
            "task_sha256": manifest["task_sha256"],
        },
    )

    if push_receipt_id:
        push_receipt = next(
            (r for r in receipts if str(r["receipt_id"]) == str(push_receipt_id)),
            None,
        )
    else:
        push_receipt = next((r for r in receipts if r["kind"] == "push"), None)

    if push_receipt is None:
        push_receipt = {
            "receipt_id": str(uuid4()),
            "kind": "push",
            "payload": {
                "repository": attempt.get("repository", "/repo"),
                "remote_url": "https://github.com/theturtlecsz/oh-my-pi.git",
                "remote_ref": "refs/heads/main",
                "candidate_commit": cand["commit_sha"],
                "result_tip": cand["commit_sha"],
            },
            "payload_sha256": "0" * 64,
            "artifact_sha256": None,
            "remote_ref": "refs/heads/main",
            "remote_commit": cand["commit_sha"],
        }

    push_payload = push_receipt.get("payload") or {}
    if isinstance(push_payload, str):
        try:
            push_payload = json.loads(push_payload)
        except Exception:
            push_payload = {}

    rev_id = (
        view["item"]["revision"]["revision_id"]
        if "revision" in view["item"] and isinstance(view["item"]["revision"], dict)
        else view["item"].get("current_revision_id", str(uuid4()))
    )

    return {
        "runner": {
            "issuer": "work-service/auditor-settle",
            "launch_id": str(launch["launch_id"]),
            "tool_call_id": launch.get("tool_call_id", "call_default"),
            "task_sha256": launch.get("task_sha256", manifest["task_sha256"]),
            "judge_sha256": attempt.get("judge_sha256"),
        },
        "subject": {
            "work_id": str(view["item"]["work_id"]),
            "revision_id": str(rev_id),
            "candidate_id": str(cand["candidate_id"]),
            "candidate_sha256": cand["candidate_sha256"],
            "candidate_commit": cand["commit_sha"],
        },
        "check": {
            "definition": "sealed_audit_manifest",
            "version": manifest["manifest_version"],
            "manifest_id": str(manifest["manifest_id"]),
            "task_sha256": manifest["task_sha256"],
        },
        "result": "PASS",
        "artifacts": [
            {
                "receipt_id": str(verif_receipt["receipt_id"]),
                "kind": "verification",
                "payload_sha256": verif_receipt["payload_sha256"],
                "artifact_sha256": verif_receipt.get("artifact_sha256"),
            },
            {
                "receipt_id": str(audit_receipt["receipt_id"]),
                "kind": "audit",
                "payload_sha256": audit_receipt["payload_sha256"],
                "artifact_sha256": audit_receipt.get("artifact_sha256")
                or ("0" * 64),
            },
            {
                "receipt_id": str(push_receipt["receipt_id"]),
                "kind": "push",
                "payload_sha256": push_receipt["payload_sha256"],
                "artifact_sha256": push_receipt.get("artifact_sha256"),
            },
        ],
        "delivery": {
            "repository": push_payload.get(
                "repository", attempt.get("repository", "/repo")
            ),
            "remote_url": push_payload.get(
                "remote_url", "https://github.com/theturtlecsz/oh-my-pi.git"
            ),
            "remote_ref": push_receipt.get("remote_ref", "refs/heads/main"),
            "candidate_commit": cand["commit_sha"],
            "remote_commit": push_receipt.get("remote_commit", cand["commit_sha"]),
        },
    }


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
    completion_payload: dict | None = None,
) -> tuple[int, dict]:
    if completion_payload is not None:
        payload = completion_payload
    else:
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
        evidence = _build_completion_evidence_from_view(workflow)
        payload = {
            "input": completion,
            "attempt_id": attempt_id,
            "done_authorization_ref": done_ref or f"done:{uuid4()}",
            "evidence": evidence,
            **({"satisfied_work_ids": satisfied} if satisfied else {}),
            **({"cancellations": cancellations} if cancellations else {}),
        }
    return _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": payload},
        operation_id=operation_id,
    )


def test_typed_audit_settle_pass(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "typed pass item")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    manifest = seal["manifest"]
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], manifest["task_sha256"]
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    launch = body["result"]["launch"]

    result = AuditResult(
        verdict="PASS",
        criteria=(
            AuditCriterionCheck(
                criterion_id="AC-1", status="met", evidence="all criteria verified"
            ),
        ),
        evidence=EvidenceManifest(
            references=(
                EvidenceReference(
                    kind="command",
                    ref="bun test",
                    result="exit 0",
                    sha256="a" * 64,
                ),
            )
        ),
    )
    submitted = {"audit_result": result.model_dump(mode="json")}
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch["launch_id"],
        submitted,
    )
    assert status == 200, body
    res = body["result"]
    assert res["status"] == "applied"
    assert res["attempt"]["state"] == "audited"

    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    receipt = next(r for r in workflow["receipts"] if r["kind"] == "audit")
    assert receipt["payload"]["audit_result"] == submitted["audit_result"]
    assert receipt["payload"]["report"] == render_audit_result(result)


def test_typed_audit_settle_needs_fix(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "typed needs fix item")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    manifest = seal["manifest"]
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], manifest["task_sha256"]
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    launch = body["result"]["launch"]

    finding = AuditFinding(
        severity="HIGH",
        criterion_id="AC-1",
        location=AuditFindingLocation(path="src/file.ts", line_start=10),
        evidence="syntax error",
        impact="build failure",
        minimal_fix="fix syntax",
    )
    result = AuditResult(
        verdict="NEEDS_FIX",
        findings=(finding,),
        criteria=(
            AuditCriterionCheck(
                criterion_id="AC-1", status="not_met", evidence="build failure"
            ),
        ),
        evidence=EvidenceManifest(references=()),
    )
    submitted = {"audit_result": result.model_dump(mode="json")}
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch["launch_id"],
        submitted,
    )
    assert status == 200, body
    res = body["result"]
    assert res["status"] == "applied"
    assert res["attempt"]["state"] == "remediation_required"

    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    receipt = next(r for r in workflow["receipts"] if r["kind"] == "audit")
    assert receipt["payload"]["audit_result"] == submitted["audit_result"]
    assert receipt["payload"]["report"] == render_audit_result(result)


def test_typed_audit_settle_pass_with_not_met_criterion_refused(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "typed invalid item")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    manifest = seal["manifest"]
    status, body = _reserve(
        service, workspace_id, attempt["attempt_id"], manifest["task_sha256"]
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    launch = body["result"]["launch"]

    submitted = {
        "audit_result": {
            "schema_version": 1,
            "verdict": "PASS",
            "criteria": [
                {
                    "criterion_id": "AC-1",
                    "status": "not_met",
                    "evidence": "failed check",
                }
            ],
            "evidence": {
                "references": [
                    {
                        "kind": "command",
                        "ref": "bun test",
                        "result": "exit 1",
                    }
                ]
            },
        }
    }
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch["launch_id"],
        submitted,
    )
    assert status == 200, body
    res = body["result"]
    assert res["status"] == "refused"
    assert res["event"]["reason_code"] == "audit_result_invalid"

    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    audit_receipts = [r for r in workflow.get("receipts", []) if r["kind"] == "audit"]
    assert len(audit_receipts) == 0

    status, comp_res = _complete(
        service, workspace_id, item, final, attempt["attempt_id"], key=item["key"]
    )
    assert comp_res.get("result", {}).get("status") != "applied"
