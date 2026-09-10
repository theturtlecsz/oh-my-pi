from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
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
from omp_work.v1.store import PostgresWorkStore
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
    if kind == "push":
        remote_commit = extra.get("remote_commit", "c" * 40)
        candidate_commit = extra.get("candidate_commit", remote_commit)
        remote_ref = extra.get("remote_ref", "refs/heads/main")
        prior_tip = extra.get("prior_tip", "0" * 40)
        repository = extra.get("repository", "/repo")
        remote_url = extra.get(
            "remote_url", "https://github.com/theturtlecsz/oh-my-pi.git"
        )
        if body is None:
            body = {
                "repository": repository,
                "remote_url": remote_url,
                "remote_ref": remote_ref,
                "prior_tip": prior_tip,
                "candidate_commit": candidate_commit,
                "result_tip": remote_commit,
            }
        elif isinstance(body, dict):
            body.setdefault("repository", repository)
            body.setdefault("remote_url", remote_url)
            body.setdefault("remote_ref", remote_ref)
            body.setdefault("prior_tip", prior_tip)
            body.setdefault("candidate_commit", candidate_commit)
            body.setdefault("result_tip", remote_commit)
        extra.setdefault("candidate_commit", candidate_commit)
        extra.setdefault("remote_ref", remote_ref)
        extra.setdefault("remote_commit", remote_commit)

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

def _push_receipt(
    work_id,
    revision_id,
    candidate_id,
    commit_sha: str,
    *,
    candidate_sha256: str | None = None,
    remote_ref="refs/heads/main",
    remote_commit=None,
    prior_tip="0" * 40,
    repository="theturtlecsz/oh-my-pi",
    remote_url="https://github.com/theturtlecsz/oh-my-pi.git",
) -> dict:
    remote_commit = remote_commit or commit_sha
    payload = {
        "repository": repository,
        "remote_url": remote_url,
        "remote_ref": remote_ref,
        "prior_tip": prior_tip,
        "candidate_commit": commit_sha,
        "result_tip": remote_commit,
    }
    return _receipt(
        work_id,
        revision_id,
        candidate_id,
        "push",
        body=payload,
        candidate_sha256=candidate_sha256,
        candidate_commit=commit_sha,
        remote_ref=remote_ref,
        remote_commit=remote_commit,
    )


def _execution_grant_audited_attempt(
    service,
    workspace_id,
    title: str = "exec item",
    *,
    predecessor_ids: list[str] | None = None,
    riders: list[dict] | None = None,
    after_begin: Callable[[], None] | None = None,
    owner_started_at: str | None = None,
    authorization_kind: str = "execution",
) -> tuple[str, str, str, str, str, str, str, dict]:
    item = _create(service, workspace_id, title, description="The request description")
    work_id = item["work_id"]
    rev_id = item["revision_id"]
    for predecessor_id in predecessor_ids or []:
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "put_relation",
                "payload": {
                    "relation": {
                        "workspace_id": str(workspace_id),
                        "source_work_id": predecessor_id,
                        "target_work_id": str(work_id),
                        "kind": "blocks",
                        "active": True,
                    }
                },
            },
        )
        assert status == 200, body

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-1",
        "normalized_command": f"/execute {item['key']}",
        "workspace_id": str(workspace_id),
        "repository": "theturtlecsz/oh-my-pi",
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
                        "active_blocker_ids": predecessor_ids or [],
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200, body
    if after_begin is not None:
        after_begin()

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
                "plan_file": "local://execute-plan.md",
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
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]

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
                "owner_session_started_at": owner_started_at or datetime.now(timezone.utc).isoformat(),
                "riders": riders or [],
                "owner_session_start_commit": head_commit,
                "repository": "theturtlecsz/oh-my-pi",
                "diff_sha256": "5" * 64,
                "authorization_kind": authorization_kind,
                "execution_grant_id": grant_id,
                "candidate_tree_sha": final_cand_sha,
                "original_request_sha256": text_sha256("The request description"),
                "criteria_sha256": sha256(["AC-1: criteria one"]),
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, begin_body

    seal = _verify_and_seal(
        service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        {"candidate_id": final_cand_id, "candidate_sha256": final_cand_sha, "commit_sha": final_commit},
        {"attempt_id": attempt_id, "work_id": work_id, "revision_id": new_rev_id, "candidate_id": final_cand_id, "candidate_sha256": final_cand_sha, "candidate_commit": final_commit},
    )
    exec_task_sha = seal["manifest"]["task_sha256"]

    status, body = _reserve(service, workspace_id, attempt_id, exec_task_sha)
    assert status == 200, body
    exec_launch_id = body["result"]["launch"]["launch_id"]

    status, body = _settle(
        service,
        workspace_id,
        attempt_id,
        exec_launch_id,
        json.dumps({"verdict": "PASS", "report": PASS_REPORT}),
    )
    assert status == 200 and body["result"]["verdict"] == "PASS", body

    push_r = _push_receipt(
        work_id,
        new_rev_id,
        final_cand_id,
        final_commit,
        candidate_sha256=final_cand_sha,
        prior_tip=head_commit,
        repository="theturtlecsz/oh-my-pi",
        remote_url="https://github.com/theturtlecsz/oh-my-pi.git",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": push_r}},
    )
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])

    return (
        grant_id,
        str(work_id),
        str(new_rev_id),
        str(final_cand_id),
        str(attempt_id),
        str(push_r["receipt_id"]),
        judge_sha,
        item,
    )


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
    service, workspace_id, title: str = "close target", *, riders: list[dict] | None = None
) -> tuple[dict, dict, dict]:
    """Full happy path through PASS settle: returns (item, final, attempt-after-settle)."""
    item = _create(service, workspace_id, title)
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item, identity={"riders": riders} if riders else None)
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
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
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
    complete_payload = {
        "input": completion,
        "attempt_id": attempt["attempt_id"],
        "done_authorization_ref": done_ref,
        "evidence": evidence,
    }
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        operation_id=operation_id,
        completion_payload=complete_payload,
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
        operation_id=operation_id,
        completion_payload=complete_payload,
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
    rider = _create(service, workspace_id, "drift carryover rider")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(
        service,
        workspace_id,
        item,
        identity={
            "riders": [
                {
                    "work_id": rider["work_id"],
                    "revision_id": rider["revision_id"],
                    "evidence": "probe: candidate drift carryover",
                }
            ]
        },
    )
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

    replacement_plan = _plan(service, workspace_id, item)
    status, body = _finalize(
        service, workspace_id, item, replacement_plan["candidate_id"]
    )
    assert status == 200, body
    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=f"summary:{uuid4()}",
        identity={"riders": []},
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    carried = body["result"]["attempt"]["riders"]
    assert len(carried) == 1
    assert carried[0]["work_id"] == rider["work_id"]
    assert carried[0]["revision_id"] == rider["revision_id"]
    assert carried[0]["evidence"] == "probe: candidate drift carryover"


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
    rider = _create(service, workspace_id, "replan supersede carryover rider")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    status, body = _begin(
        service,
        workspace_id,
        item,
        identity={
            "riders": [
                {
                    "work_id": rider["work_id"],
                    "revision_id": rider["revision_id"],
                    "evidence": "probe: superseded by new plan",
                }
            ]
        },
    )
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
    status, body = _begin(service, workspace_id, item, identity={"riders": []})
    assert status == 200 and body["result"]["status"] == "applied"
    carried = body["result"]["attempt"]["riders"]
    assert len(carried) == 1
    assert carried[0]["work_id"] == rider["work_id"]
    assert carried[0]["revision_id"] == rider["revision_id"]
    assert carried[0]["evidence"] == "probe: superseded by new plan"
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


def _prepare_closeout(service, workspace_id, item: dict, final: dict, attempt: dict) -> None:
    """Record supported closeout/push evidence without completing the work item."""
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    _drain_deliveries(service, workspace_id, key=item["key"])
    push = _push_receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        final["commit_sha"],
        candidate_sha256=final["candidate_sha256"],
        repository=attempt.get("repository", "/repo"),
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200, body


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
    _prepare_closeout(service, workspace_id, item, final, attempt)
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
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    _drain_deliveries(service, workspace_id, key=item["key"])
    push = _push_receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        final["commit_sha"],
        candidate_sha256=final["candidate_sha256"],
        repository=attempt.get("repository", "/repo"),
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200, body
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
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
    complete_payload = {
        "input": completion,
        "attempt_id": attempt["attempt_id"],
        "done_authorization_ref": done_ref,
        "evidence": evidence,
        "cancellations": cancellations,
    }
    status, body = _complete(
        service,
        workspace_id,
        item,
        final,
        attempt["attempt_id"],
        operation_id=operation_id,
        completion_payload=complete_payload,
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
        operation_id=operation_id,
        completion_payload=complete_payload,
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


@pytest.mark.parametrize(
    ("terminal_state", "report"),
    [
        ("remediation_required", NEEDS_FIX_REPORT),
        ("blocked", BLOCKED_REPORT),
        ("budget_exhausted", None),
    ],
)
def test_terminal_rider_carryover(
    service, terminal_state: str, report: str | None
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, f"rider for {terminal_state}")
    item = _create(service, workspace_id, f"primary for {terminal_state}")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": f"probe: {terminal_state}",
        }
    ]
    status, body = _begin(service, workspace_id, item, identity={"riders": riders})
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    task_sha = seal["manifest"]["task_sha256"]

    settle_events = []
    if report is not None:
        status, body = _reserve(
            service, workspace_id, attempt["attempt_id"], task_sha
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        status, body = _settle(
            service,
            workspace_id,
            attempt["attempt_id"],
            body["result"]["launch"]["launch_id"],
            {"report": report},
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        settle_events.append(body["result"]["event"])
    else:
        for _ in range(3):
            status, body = _reserve(
                service, workspace_id, attempt["attempt_id"], task_sha
            )
            assert status == 200 and body["result"]["status"] == "applied", body
            status, body = _settle(
                service,
                workspace_id,
                attempt["attempt_id"],
                body["result"]["launch"]["launch_id"],
                failed=True,
            )
            assert status == 200, body
            settle_events.append(body["result"]["event"])

    assert body["result"]["attempt"]["state"] == terminal_state
    for event in settle_events:
        if event["requires_delivery"]:
            status, attest_body = _attest(service, workspace_id, event)
            assert status == 200 and attest_body["result"]["status"] == "applied"

    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=f"summary:{uuid4()}",
        identity={"riders": []},
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    replacement = body["result"]["attempt"]
    assert replacement["attempt_id"] != attempt["attempt_id"]
    assert len(replacement["riders"]) == 1
    assert replacement["riders"][0]["work_id"] == rider["work_id"]
    assert replacement["riders"][0]["revision_id"] == rider["revision_id"]
    assert replacement["riders"][0]["evidence"] == f"probe: {terminal_state}"


def test_terminal_rider_carryover_revalidates_revision(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "rider revision changes")
    item = _create(service, workspace_id, "primary rider revision changes")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(
        service,
        workspace_id,
        item,
        identity={
            "riders": [
                {
                    "work_id": rider["work_id"],
                    "revision_id": rider["revision_id"],
                    "evidence": "probe: original revision",
                }
            ]
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(
        service,
        workspace_id,
        attempt["attempt_id"],
        seal["manifest"]["task_sha256"],
    )
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(
        service,
        workspace_id,
        attempt["attempt_id"],
        launch_id,
        {"report": NEEDS_FIX_REPORT},
    )
    assert status == 200 and body["result"]["attempt"]["state"] == "remediation_required"
    event = body["result"]["event"]
    if event["requires_delivery"]:
        status, attest_body = _attest(service, workspace_id, event)
        assert status == 200 and attest_body["result"]["status"] == "applied"

    new_revision_id = uuid4()
    revision = {
        "revision_id": str(new_revision_id),
        "work_id": rider["work_id"],
        "revision_number": 2,
        "title": "rider revision changes",
        "description": "revised rider",
        "scope": "",
        "acceptance_criteria": [],
        "content_sha256": sha256(
            {"title": "rider revision changes", "description": "revised rider"}
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
                "work_id": rider["work_id"],
                "expected_revision_id": rider["revision_id"],
                "revision": revision,
            },
        },
    )
    assert status == 200 and body["result"]["changed"] is True, body

    status, body = _begin(
        service,
        workspace_id,
        item,
        authorization_ref=f"summary:{uuid4()}",
        identity={"riders": []},
    )
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "rider_binding_invalid"
    assert "not on the sealed revision" in body["result"]["event"]["reason"]


def test_terminal_rider_carryover_reversed_concurrent_begins(service) -> None:
    """OMP-187 remediation: two concurrent replacement begins with reversed
    primary/rider relationships must acquire one canonical work-row lock order
    (carried riders pre-locked with the primary) — never an ABBA deadlock."""
    import threading
    import time

    from omp_work.v1.store import PostgresWorkStore

    workspace_id = uuid4()
    _grant(service, workspace_id)
    item_a = _create(service, workspace_id, "reversed carryover primary A")
    item_b = _create(service, workspace_id, "reversed carryover primary B")

    def seed_terminal(primary: dict, rider: dict, tag: str) -> None:
        plan = _plan(service, workspace_id, primary)
        status, body = _finalize(service, workspace_id, primary, plan["candidate_id"])
        assert status == 200, body
        final = body["result"]["candidate"]
        status, body = _begin(
            service,
            workspace_id,
            primary,
            identity={
                "riders": [
                    {
                        "work_id": rider["work_id"],
                        "revision_id": rider["revision_id"],
                        "evidence": f"probe: {tag}",
                    }
                ]
            },
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        attempt = body["result"]["attempt"]
        seal = _verify_and_seal(service, workspace_id, primary, final, attempt)
        status, body = _reserve(
            service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"]
        )
        assert status == 200 and body["result"]["status"] == "applied", body
        status, body = _settle(
            service,
            workspace_id,
            attempt["attempt_id"],
            body["result"]["launch"]["launch_id"],
            {"report": NEEDS_FIX_REPORT},
        )
        assert status == 200, body
        assert body["result"]["attempt"]["state"] == "remediation_required"
        event = body["result"]["event"]
        if event["requires_delivery"]:
            status, attest_body = _attest(service, workspace_id, event)
            assert status == 200 and attest_body["result"]["status"] == "applied"

    seed_terminal(item_a, item_b, "a-carries-b")
    seed_terminal(item_b, item_a, "b-carries-a")

    gate_armed = threading.Event()
    t1_at_hook = threading.Event()
    release_t1 = threading.Event()
    original_chain = PostgresWorkStore._lock_work_chain

    def gated_chain(self, cur, ws, work_id):
        result = original_chain(self, cur, ws, work_id)
        if gate_armed.is_set() and str(work_id) == str(item_a["work_id"]):
            gate_armed.clear()
            t1_at_hook.set()
            assert release_t1.wait(timeout=30), "gate never released"
        return result

    results: dict[str, tuple[int, dict]] = {}

    def run_begin(name: str, primary: dict) -> None:
        results[name] = _begin(
            service,
            workspace_id,
            primary,
            authorization_ref=f"summary:{uuid4()}",
            identity={"riders": []},
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PostgresWorkStore, "_lock_work_chain", gated_chain)
        gate_armed.set()
        t1 = threading.Thread(target=run_begin, args=("a", item_a))
        t1.start()
        assert t1_at_hook.wait(timeout=30), "thread 1 never reached the chain-lock gate"

        t2 = threading.Thread(target=run_begin, args=("b", item_b))
        t2.start()

        # Deterministic gate: thread 2's canonical bulk lock ({A, B} sorted)
        # must queue behind thread 1's pre-locked set — poll pg_stat_activity
        # until its backend waits on a row lock, never a fixed sleep.
        deadline = time.monotonic() + 15
        blocked = False
        with psycopg.connect(
            **service.config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            while time.monotonic() < deadline:
                with conn.cursor() as stat_cur:
                    stat_cur.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock'",
                        (service.config.database,),
                    )
                    if stat_cur.fetchone()[0] >= 1:
                        blocked = True
                        break
                time.sleep(0.05)
        assert blocked, "thread 2 never queued behind the canonical lock set"

        release_t1.set()
        t1.join(timeout=30)
        t2.join(timeout=30)
        assert not t1.is_alive() and not t2.is_alive(), "concurrent begins did not finish"

    for name, rider in (("a", item_b), ("b", item_a)):
        status, body = results[name]
        assert status == 200, (name, body)
        assert body["result"]["status"] == "applied", (name, body)
        replacement = body["result"]["attempt"]
        assert len(replacement["riders"]) == 1, (name, replacement["riders"])
        assert replacement["riders"][0]["work_id"] == rider["work_id"]


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


def test_execution_lookup_accepts_grant_id_work_id_and_key(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(
        service,
        workspace_id,
        "Execution lookup selectors",
        description="Lookup this execution by grant, work id, or key",
    )
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_id,
                "provenance": {
                    "owner_input_id": str(uuid4()),
                    "owner_session_id": "session-lookup",
                    "normalized_command": f"/execute {item['key']}",
                    "workspace_id": str(workspace_id),
                    "repository": "oh-my-pi",
                    "nonce": str(uuid4()),
                    "issued_at": datetime.now(timezone.utc).isoformat(),
                },
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": item["work_id"],
                        "revision_id": item["revision_id"],
                        "position": 0,
                        "original_request": "Lookup this execution by grant, work id, or key",
                        "original_request_sha256": text_sha256(
                            "Lookup this execution by grant, work id, or key"
                        ),
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

    views = []
    for selector in (grant_id, item["work_id"], item["key"]):
        response = service.client.get(
            f"/v1/workspaces/{workspace_id}/execution/{selector}",
            headers=_owner_headers(workspace_id),
        )
        assert response.status_code == 200, response.text
        views.append(response.json())

    assert [view["grant"]["grant_id"] for view in views] == [grant_id] * 3
    assert [view["active_item"]["work_id"] for view in views] == [
        item["work_id"]
    ] * 3


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
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    evidence = _build_completion_evidence_from_view(workflow, push_receipt_id)
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
                "evidence": evidence,
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
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    evidence = _build_completion_evidence_from_view(workflow, push_receipt_2_id)
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
                "evidence": evidence,
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


@pytest.mark.parametrize(
    ("terminal_state", "terminal_reason"),
    [("stopped", "owner_stopped"), ("canceled", "owner_canceled")],
)
def test_execution_grant_pause_resume_and_terminal_judge_drift(
    service, terminal_state: str, terminal_reason: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Contract approval test item", description="desc")
    work_id, rev_id = item["work_id"], item["revision_id"]
    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    drifted_judge_sha = ("0" if judge_sha[0] != "0" else "1") + judge_sha[1:]
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

    # Drifted pause is rejected
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
                "judge_sha256": drifted_judge_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "execution_judge_drift"

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

    # Drifted resume is rejected
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "target_state": "active",
                "expected_grant_version": 2,
                "judge_sha256": drifted_judge_sha,
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "execution_judge_drift"

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

    # Pause and then terminate with drifted judge hash
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
                "target_state": terminal_state,
                "expected_grant_version": 4,
                "reason": terminal_reason,
                "judge_sha256": drifted_judge_sha,
            },
        },
    )
    assert status == 200, body
    grant = body["result"]["grant"]
    assert grant["state"] == terminal_state
    assert grant["terminal_reason"] == terminal_reason
    assert grant["paused_at"] is None
    assert grant[f"{terminal_state}_at"] is not None

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
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    bad_evidence = _build_completion_evidence_from_view(workflow, bad_push_id)
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
                "evidence": bad_evidence,
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
        workflow = service.client.get(
            f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
        ).json()
        bad_evidence = _build_completion_evidence_from_view(workflow, bad_id)
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
                    "evidence": bad_evidence,
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
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    good_evidence = _build_completion_evidence_from_view(workflow, good_push_id)
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
                "evidence": good_evidence,
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
                "evidence": good_evidence,
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


def test_execution_grant_zero_path_lifecycle_and_remediation_widening(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Zero Path Item", description="Zero path item description")
    work_id, rev_id = item["work_id"], item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    status, body = _command(
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
                        "original_request": "Zero path item description",
                        "original_request_sha256": text_sha256("Zero path item description"),
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
                "criteria": ["AC-1: criteria zero path"],
                "description_sha256": text_sha256("Zero path item description"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]

    # Stamp zero-path plan
    cand_1_id = str(uuid4())
    plan_content = "## Approach\n1. External restoration\n\n## Verification\n1. Verification check"
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
                "plan_file": "local://zero-path-plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. External restoration"],
                "verification": ["1. Verification check"],
                "paths": [],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    item_res = body["result"]["item"]
    assert item_res["phase"] == "executing"
    plan_stamp = item_res["plan_stamp"]
    assert plan_stamp["paths"] == []
    assert plan_stamp["initial_paths"] == []
    plan_stamp_sha_1 = item_res["plan_stamp_sha256"]

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
                "original_request_sha256": text_sha256("Zero path item description"),
                "criteria_sha256": sha256(["AC-1: criteria zero path"]),
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

    # Restamp with non-empty path -> must fail with invalid_request naming the widened path
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
                "plan_file": "local://zero-path-plan-2.md",
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
    assert status == 400, body
    assert any("remediation plan widens sealed paths beyond initial plan stamp" in d and "src/feature.ts" in d for d in body["error"]["diagnostics"])

    # Restamp with [] -> must succeed
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
                "plan_file": "local://zero-path-plan-2.md",
                "plan_body": plan_content_2,
                "plan_sha256": sha256(plan_content_2),
                "approach": ["1. Step one fixed"],
                "verification": ["1. Check one fixed"],
                "paths": [],
                "candidate_sha256": "4" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "executing"
    assert body["result"]["item"]["plan_stamp"]["paths"] == []
    assert body["result"]["item"]["plan_stamp"]["initial_paths"] == []


def test_execution_grant_service_refresh_stale_source_and_drift_matrix(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(
        service,
        workspace_id,
        "Build python runtime feature",
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

    # Stamp plan with Python runtime path
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
                "paths": ["python/omp-work/src/omp_work/v1/store.py"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    plan_stamp_sha = body["result"]["item"]["plan_stamp_sha256"]
    criteria_sha = body["result"]["item"]["criteria_sha256"]
    # Prepare TS-only item in separate workspace before monkeypatch
    workspace_ts = uuid4()
    _grant(service, workspace_ts)
    item_ts = _create(service, workspace_ts, "TS only item", description="TS request")
    grant_ts_id = str(uuid4())
    provenance_ts = dict(provenance, owner_input_id=str(uuid4()), nonce=str(uuid4()), workspace_id=str(workspace_ts))
    status, body = _command(
        service,
        workspace_ts,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_ts_id,
                "provenance": provenance_ts,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(item_ts["work_id"]),
                        "revision_id": str(item_ts["revision_id"]),
                        "position": 0,
                        "original_request": "TS request",
                        "original_request_sha256": text_sha256("TS request"),
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
    status, body = _command(
        service,
        workspace_ts,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_ts_id,
                "expected_grant_version": 1,
                "work_id": str(item_ts["work_id"]),
                "expected_revision_id": str(item_ts["revision_id"]),
                "criteria": ["AC-1: criteria"],
                "description_sha256": text_sha256("TS request"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200
    ts_rev = body["result"]["revision"]["revision_id"]
    status, _ = _command(
        service,
        workspace_ts,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_ts_id,
                "expected_grant_version": 2,
                "work_id": str(item_ts["work_id"]),
                "revision_id": str(ts_rev),
                "candidate_id": str(uuid4()),
                "plan_file": "local://plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["packages/coding-agent/src/index.ts", "python/omp-work/src/omp_work/data.txt"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200
    # Prepare migration-path item in separate workspace before monkeypatch
    workspace_mig = uuid4()
    _grant(service, workspace_mig)
    item_mig = _create(service, workspace_mig, "Migration item", description="Mig request")
    grant_mig_id = str(uuid4())
    provenance_mig = dict(provenance, owner_input_id=str(uuid4()), nonce=str(uuid4()), workspace_id=str(workspace_mig))
    status, _ = _command(
        service,
        workspace_mig,
        {
            "type": "begin_execution",
            "payload": {
                "grant_id": grant_mig_id,
                "provenance": provenance_mig,
                "remote_ref": "refs/heads/main",
                "mode": "single",
                "items": [
                    {
                        "work_id": str(item_mig["work_id"]),
                        "revision_id": str(item_mig["revision_id"]),
                        "position": 0,
                        "original_request": "Mig request",
                        "original_request_sha256": text_sha256("Mig request"),
                        "initial_git_baseline": head_commit,
                    }
                ],
                "expected_focus_version": 0,
                "judge_sha256": judge_sha,
                "judge_manifest": judge_manifest,
            },
        },
    )
    assert status == 200
    status, body = _command(
        service,
        workspace_mig,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_mig_id,
                "expected_grant_version": 1,
                "work_id": str(item_mig["work_id"]),
                "expected_revision_id": str(item_mig["revision_id"]),
                "criteria": ["AC-1: criteria"],
                "description_sha256": text_sha256("Mig request"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200
    mig_rev = body["result"]["revision"]["revision_id"]
    status, _ = _command(
        service,
        workspace_mig,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_mig_id,
                "expected_grant_version": 2,
                "work_id": str(item_mig["work_id"]),
                "revision_id": str(mig_rev),
                "candidate_id": str(uuid4()),
                "plan_file": "local://plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["python/omp-work/src/omp_work/operations/migrations/0024_x.sql", "python/omp-work/src/omp_work/v1/store.py"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200



    # Direct DB trigger regression: arbitrary judge updates fail at trigger
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            # 1. Non-service field modification fails
            bad_manifest = dict(judge_manifest)
            bad_manifest["auditor_agent_sha256"] = "9" * 64
            with pytest.raises(psycopg.Error, match="service-only manifest deltas"):
                cur.execute(
                    "UPDATE omp_work.execution_grants SET judge_manifest=%s, judge_sha256=%s, grant_version=grant_version+1 WHERE grant_id=%s",
                    (json.dumps(bad_manifest), sha256(bad_manifest), grant_id),
                )
            conn.rollback()

            # 2. Extra key addition fails
            extra_manifest = dict(judge_manifest)
            extra_manifest["extra_key"] = "malicious"
            with pytest.raises(psycopg.Error, match="service-only manifest deltas"):
                cur.execute(
                    "UPDATE omp_work.execution_grants SET judge_manifest=%s, judge_sha256=%s, grant_version=grant_version+1 WHERE grant_id=%s",
                    (json.dumps(extra_manifest), sha256(extra_manifest), grant_id),
                )
            conn.rollback()
    # Now simulate on-disk source change
    import omp_work.v1.server as server_module
    new_fp = "a" * 64
    monkeypatch.setattr(server_module, "code_fingerprint", lambda: "beef" * 16)
    monkeypatch.setattr("omp_work.operations.fingerprints.service_runtime_fingerprint", lambda: new_fp)
    monkeypatch.setattr("omp_work.v1.server.service_runtime_fingerprint", lambda: new_fp)
    monkeypatch.setattr("omp_work.v1.store.service_runtime_fingerprint", lambda: new_fp)

    new_manifest = dict(judge_manifest)
    new_manifest["service_fingerprint"] = new_fp
    new_manifest["service_code_fingerprint"] = new_fp
    new_manifest["service_migration_sha256"] = new_fp
    new_judge_sha = sha256(new_manifest)

    # 1. Ordinary write fails 503 service_stale
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "stale", "title": "must not land"}]),
    )
    assert status == 503 and body["error"]["code"] == "unavailable"

    # 2. Service refresh with wrong prospective judge fails execution_judge_drift
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "target_state": "active",
                "reason": "service_refresh",
                "judge_sha256": "0" * 64,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_judge_drift"

    # 2b. Refusal when plan has no eligible .py runtime path (e.g. data.txt / TS path)
    status, body = _command(
        service,
        workspace_ts,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_ts_id,
                "expected_grant_version": 3,
                "target_state": "active",
                "reason": "service_refresh",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert any("at least one stamped .py path" in d for d in body["error"]["diagnostics"])

    # 2c. Refusal when plan contains migration path
    status, body = _command(
        service,
        workspace_mig,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_mig_id,
                "expected_grant_version": 3,
                "target_state": "active",
                "reason": "service_refresh",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 400 and body["error"]["code"] == "invalid_request"
    assert any("migration paths" in d for d in body["error"]["diagnostics"])

    # Query DB state before refresh
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT judge_manifest, grant_version, state, continuations_scheduled FROM omp_work.execution_grants WHERE grant_id=%s", (grant_id,))
            before_row = cur.fetchone()
            before_manifest = before_row[0] if isinstance(before_row[0], dict) else json.loads(before_row[0])
            before_ver = before_row[1]
            before_state = before_row[2]
            before_continuations = before_row[3]

            cur.execute("SELECT phase FROM omp_work.execution_grant_items WHERE grant_id=%s AND work_id=%s", (grant_id, str(work_id)))
            before_phase = cur.fetchone()[0]

    # 3. Valid service_refresh succeeds under stale source
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "target_state": "active",
                "reason": "service_refresh",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["grant_version"] == 4
    assert body["result"]["grant"]["state"] == "active"
    assert body["result"]["grant"]["judge_sha256"] == new_judge_sha

    # Query DB state after refresh and assert invariants
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT judge_manifest, grant_version, state, continuations_scheduled FROM omp_work.execution_grants WHERE grant_id=%s", (grant_id,))
            after_row = cur.fetchone()
            after_manifest = after_row[0] if isinstance(after_row[0], dict) else json.loads(after_row[0])
            after_ver = after_row[1]
            after_state = after_row[2]
            after_continuations = after_row[3]

            cur.execute("SELECT phase FROM omp_work.execution_grant_items WHERE grant_id=%s AND work_id=%s", (grant_id, str(work_id)))
            after_phase = cur.fetchone()[0]

    service_keys = {"service_fingerprint", "service_code_fingerprint", "service_migration_sha256"}
    assert {k: v for k, v in after_manifest.items() if k not in service_keys} == {k: v for k, v in before_manifest.items() if k not in service_keys}
    assert after_manifest["service_fingerprint"] == new_fp
    assert after_manifest["service_code_fingerprint"] == new_fp
    assert after_manifest["service_migration_sha256"] == new_fp
    assert after_ver == before_ver + 1
    assert after_state == "active"
    assert after_continuations == before_continuations
    assert after_phase == before_phase == "executing"
    # 4. Repeating valid service_refresh is idempotent
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 4,
                "target_state": "active",
                "reason": "service_refresh",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["grant_version"] == 4

    # 5. Simulate post-restart: create new service instance capturing new source snapshot
    restarted_service = SimpleNamespace(
        client=TestClient(create_app(service.config, capabilities_dir=service.capabilities)),
        capabilities=service.capabilities,
        config=service.config,
    )

    # 6. Old judge fails non-terminal command with execution_judge_drift
    status, body = _command(
        restarted_service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 4,
                "target_state": "paused",
                "reason": "testing_old_judge",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_judge_drift"

    # 7. New judge can pause and resume
    status, body = _command(
        restarted_service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 4,
                "target_state": "paused",
                "reason": "testing_new_judge",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "paused"

    status, body = _command(
        restarted_service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 5,
                "target_state": "active",
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "active"

    # 8. Complete review lifecycle under restarted service using new judge
    final_commit = "1" * 40
    final_cand_id = str(uuid4())
    final_cand_sha = "2" * 64
    _finalize(
        restarted_service,
        workspace_id,
        {"work_id": work_id, "revision_id": new_rev_id},
        candidate_id,
        commit=final_commit,
        final_id=final_cand_id,
        candidate_hash=final_cand_sha,
    )

    push_receipt_id = str(uuid4())
    status, body = _command(
        restarted_service,
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

    attempt_id = str(uuid4())
    status, begin_body = _command(
        restarted_service,
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
                "criteria_sha256": criteria_sha,
                "plan_stamp_sha256": plan_stamp_sha,
                "judge_sha256": new_judge_sha,
                "riders": [],
            },
        },
    )
    assert begin_body["result"]["status"] == "applied", begin_body["result"]["event"]["rendered_text"]
    attempt_id = str(begin_body["result"]["attempt"]["attempt_id"])
    begin_event = begin_body["result"]["event"]

    verif_receipt_id = str(uuid4())
    status, body = _command(
        restarted_service,
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

    status, body = _command(
        restarted_service,
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
    task_sha = body["result"]["manifest"]["task_sha256"]

    launch_id = str(uuid4())
    status, body = _command(
        restarted_service,
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
        restarted_service,
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
        restarted_service,
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
        restarted_service,
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

    status, body = _record_review(
        restarted_service,
        workspace_id,
        item,
        {"candidate_id": final_cand_id, "candidate_sha256": final_cand_sha, "commit_sha": final_commit},
        {"attempt_id": attempt_id, "revision_id": new_rev_id, "candidate_id": final_cand_id, "candidate_sha256": final_cand_sha, "candidate_commit": final_commit},
        authorization_ref=f"execution:{grant_id}:0:1",
    )
    assert status == 200, body
    _drain_deliveries(restarted_service, workspace_id, key=item["key"])

    workflow = restarted_service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    evidence = _build_completion_evidence_from_view(workflow, push_receipt_id)
    status, body = _command(
        restarted_service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "work_id": str(work_id),
                "attempt_id": attempt_id,
                "evidence": evidence,
                "expected_grant_version": 6,
                "judge_sha256": new_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "completed"
    assert body["result"]["item"]["phase"] == "completed"

def test_execution_grant_pre_review_replan_and_stale_service_pause_stop(
    service, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(
        service,
        workspace_id,
        "Replan test item",
        description="Test description for replan and stale source",
    )
    work_id = item["work_id"]
    rev_id = item["revision_id"]

    grant_id = str(uuid4())
    judge_sha, judge_manifest = _tcb_manifest()
    head_commit = "0" * 40

    provenance = {
        "owner_input_id": str(uuid4()),
        "owner_session_id": "session-replan",
        "normalized_command": "/execute OMP-10",
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
                        "original_request": "Test description for replan and stale source",
                        "original_request_sha256": text_sha256(
                            "Test description for replan and stale source"
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
                "description_sha256": text_sha256("Test description for replan and stale source"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]

    # 1. Initial stamp in planning
    cand_1_id = str(uuid4())
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
                "candidate_id": cand_1_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["python/omp-work/src/omp_work/v1/server.py"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "executing"
    assert body["result"]["item"]["plan_stamp"]["paths"] == ["python/omp-work/src/omp_work/v1/server.py"]
    assert body["result"]["item"]["plan_stamp"]["initial_paths"] == ["python/omp-work/src/omp_work/v1/server.py"]

    # 2. Re-stamp in executing phase (scope correction before review)
    cand_2_id = str(uuid4())
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
                "plan_file": "local://plan.md",
                "plan_body": plan_content,
                "plan_sha256": sha256(plan_content),
                "approach": ["1. Step one"],
                "verification": ["1. Check one"],
                "paths": ["python/omp-work/src/omp_work/v1/server.py", "python/omp-work/src/omp_work/v1/store.py"],
                "candidate_sha256": "2" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["item"]["phase"] == "executing"
    assert body["result"]["item"]["plan_stamp"]["paths"] == ["python/omp-work/src/omp_work/v1/server.py", "python/omp-work/src/omp_work/v1/store.py"]
    assert body["result"]["item"]["plan_stamp"]["initial_paths"] == ["python/omp-work/src/omp_work/v1/server.py", "python/omp-work/src/omp_work/v1/store.py"]

    # 3. Simulate on-disk source modification causing stale service
    import omp_work.v1.server as server_module
    monkeypatch.setattr(server_module, "code_fingerprint", lambda: "c001" * 16)

    # Ordinary write fails 503 service_stale
    status, body = _command(
        service,
        workspace_id,
        _batch([{"client_ref": "stale", "title": "must not land"}]),
    )
    assert status == 503 and body["error"]["code"] == "unavailable"

    # Pausing grant succeeds under stale service
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 4,
                "target_state": "paused",
                "reason": "pause_under_stale_source",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "paused"

    # Stopping grant succeeds under stale service
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 5,
                "target_state": "stopped",
                "reason": "model_stopped_stale",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["grant"]["state"] == "stopped"


def test_completion_evidence_service_boundary(service) -> None:
    # OMP-247: complete_work and complete_execution_item require valid CompletionEvidence
    # verified against service-owned rows.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(
        service, workspace_id, "evidence service boundary"
    )
    push_r = _push_receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        final["commit_sha"],
        candidate_sha256=final["candidate_sha256"],
        repository=attempt["repository"],
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": push_r}},
    )
    assert status == 200, body
    push_receipt_id = push_r["receipt_id"]

    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])

    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    valid_evidence = _build_completion_evidence_from_view(workflow, push_receipt_id)

    # Negative test 1: arbitrary caller PASS prose plus fabricated audit reference is refused
    fake_audit_id = str(uuid4())
    fake_evidence = dict(valid_evidence)
    fake_evidence["artifacts"] = [
        valid_evidence["artifacts"][0],
        {
            "receipt_id": fake_audit_id,
            "kind": "audit",
            "payload_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
        },
        valid_evidence["artifacts"][2],
    ]
    completion_input = {
        "work_id": item["work_id"],
        "current_revision_id": item["revision_id"],
        "candidate": workflow["item"]["candidate"],
        "receipts": [
            r for r in workflow["receipts"] if r["candidate_id"] == final["candidate_id"]
        ],
        "closeout_requested": True,
    }
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_work",
            "payload": {
                "input": completion_input,
                "attempt_id": attempt["attempt_id"],
                "done_authorization_ref": f"done:{uuid4()}",
                "evidence": fake_evidence,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "refused", body
    assert "completion_evidence_invalid" in body["result"]["event"]["reason"]

    # Negative test 2: foreign receipt naming another work item is refused
    other_item = _create(service, workspace_id, "other work item")
    other_plan = _plan(service, workspace_id, other_item)
    foreign_evidence = dict(valid_evidence)
    foreign_evidence["artifacts"] = [
        {
            "receipt_id": other_plan["receipt_id"],
            "kind": "verification",
            "payload_sha256": other_plan["payload_sha256"],
            "artifact_sha256": other_plan.get("artifact_sha256"),
        },
        valid_evidence["artifacts"][1],
        valid_evidence["artifacts"][2],
    ]
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_work",
            "payload": {
                "input": completion_input,
                "attempt_id": attempt["attempt_id"],
                "done_authorization_ref": f"done:{uuid4()}",
                "evidence": foreign_evidence,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "refused", body

    _drain_deliveries(service, workspace_id, key=item["key"])

    # Positive complete_work with valid evidence succeeds
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_work",
            "payload": {
                "input": completion_input,
                "attempt_id": attempt["attempt_id"],
                "done_authorization_ref": f"done:{uuid4()}",
                "evidence": valid_evidence,
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["state"] == "DONE"

    # Now test complete_execution_item path
    (
        grant_id,
        exec_work_id,
        exec_rev_id,
        exec_cand_id,
        exec_attempt_id,
        exec_push_id,
        exec_judge_sha,
        exec_item,
    ) = _execution_grant_audited_attempt(
        service, workspace_id, "exec item for boundary"
    )
    workflow_exec = service.client.get(
        f"/v1/work-items/{exec_item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    valid_exec_evidence = _build_completion_evidence_from_view(
        workflow_exec, exec_push_id
    )

    # Negative probe 1: fabricated audit receipt in evidence is refused with completion_blocked
    fake_exec_evidence = dict(valid_exec_evidence)
    fake_exec_evidence["artifacts"] = [
        valid_exec_evidence["artifacts"][0],
        {
            "receipt_id": str(uuid4()),
            "kind": "audit",
            "payload_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
        },
        valid_exec_evidence["artifacts"][2],
    ]
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": exec_work_id,
                "attempt_id": exec_attempt_id,
                "evidence": fake_exec_evidence,
                "judge_sha256": exec_judge_sha,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "completion_blocked", body

    # Negative probe 2: foreign receipt naming another work item is refused with completion_blocked
    foreign_exec_evidence = dict(valid_exec_evidence)
    foreign_exec_evidence["artifacts"] = [
        {
            "receipt_id": other_plan["receipt_id"],
            "kind": "verification",
            "payload_sha256": other_plan["payload_sha256"],
            "artifact_sha256": other_plan.get("artifact_sha256"),
        },
        valid_exec_evidence["artifacts"][1],
        valid_exec_evidence["artifacts"][2],
    ]
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": exec_work_id,
                "attempt_id": exec_attempt_id,
                "evidence": foreign_exec_evidence,
                "judge_sha256": exec_judge_sha,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "completion_blocked", body

    # Positive complete_execution_item with valid evidence succeeds
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": exec_work_id,
                "attempt_id": exec_attempt_id,
                "evidence": valid_exec_evidence,
                "judge_sha256": exec_judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["state"] == "DONE"
    assert body["result"]["grant"]["state"] == "completed"


def test_receipt_idempotency_exact_retry_returns_original_row(service) -> None:
    # OMP-247 step 3: identical receipt payload returns the existing persisted row
    # without primary key failure; changed payload conflicts.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "idempotent receipt target")
    plan = _plan(service, workspace_id, item)

    # 1. Verification receipt retry
    verif_id = str(uuid4())
    verif_payload = {"notes": "initial verification evidence"}
    verif_body = {
        "receipt_id": verif_id,
        "work_id": str(item["work_id"]),
        "revision_id": str(item["revision_id"]),
        "candidate_id": str(plan["candidate_id"]),
        "kind": "verification",
        "payload": verif_payload,
        "payload_sha256": sha256(verif_payload),
        "artifact_sha256": None,
        "issuer": "test",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": plan["candidate_sha256"],
        "candidate_commit": plan["candidate_commit"],
        "verdict": None,
        "independent": False,
        "remote_ref": None,
        "remote_commit": None,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": verif_body}},
    )
    assert status == 200, body
    first_verif = body["result"]["receipt"]

    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": verif_body}},
    )
    assert status == 200, body
    assert body["result"]["receipt"]["receipt_id"] == first_verif["receipt_id"]

    # 2. Plan receipt retry
    plan_id = str(uuid4())
    plan_cand_id = str(uuid4())
    plan_payload = {"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"}
    plan_body = {
        "receipt_id": plan_id,
        "work_id": str(item["work_id"]),
        "revision_id": str(item["revision_id"]),
        "candidate_id": plan_cand_id,
        "kind": "plan",
        "payload": plan_payload,
        "payload_sha256": sha256(plan_payload),
        "artifact_sha256": None,
        "issuer": "test",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": "7" * 64,
        "candidate_commit": None,
        "verdict": None,
        "independent": False,
        "remote_ref": None,
        "remote_commit": None,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": plan_body}},
    )
    assert status == 200, body
    first_plan = body["result"]["receipt"]

    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": plan_body}},
    )
    assert status == 200, body
    assert body["result"]["receipt"]["receipt_id"] == first_plan["receipt_id"]

    # 3. Same receipt_id with altered claim field fails with idempotency_conflict
    altered_receipt = dict(verif_body)
    altered_receipt["payload"] = {"notes": "altered payload"}
    altered_receipt["payload_sha256"] = sha256({"notes": "altered payload"})
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": altered_receipt}},
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"


def test_completion_evidence_idempotency_and_claim_race(service) -> None:
    # OMP-247 step 2: same operation_id replays; same operation_id + changed claim conflicts;
    # concurrent completions yield exactly one owner.
    import concurrent.futures

    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(
        service, workspace_id, "idempotency replay item"
    )
    push_r = _push_receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        final["commit_sha"],
        candidate_sha256=final["candidate_sha256"],
        repository=attempt["repository"],
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": push_r}},
    )
    assert status == 200, body
    push_receipt_id = push_r["receipt_id"]

    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])

    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    valid_evidence = _build_completion_evidence_from_view(workflow, push_receipt_id)

    completion_input = {
        "work_id": item["work_id"],
        "current_revision_id": item["revision_id"],
        "candidate": workflow["item"]["candidate"],
        "receipts": [
            r for r in workflow["receipts"] if r["candidate_id"] == final["candidate_id"]
        ],
        "closeout_requested": True,
    }
    op_id = uuid4()
    payload = {
        "input": completion_input,
        "attempt_id": attempt["attempt_id"],
        "done_authorization_ref": f"done:{uuid4()}",
        "evidence": valid_evidence,
    }

    # 1. First completion applies
    status, body = _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": payload},
        operation_id=op_id,
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 2. Identical request with same operation_id replays
    status, body = _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": payload},
        operation_id=op_id,
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    # 3. Changed payload with same operation_id returns idempotency_conflict
    altered_payload = dict(payload)
    altered_payload["done_authorization_ref"] = f"done:{uuid4()}"
    status, body = _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": altered_payload},
        operation_id=op_id,
    )
    assert status == 409, body
    assert body["error"]["code"] == "idempotency_conflict"

    # 4. Concurrent race test: two concurrent transactions trying to complete
    item2, final2, attempt2 = _audited_attempt(
        service, workspace_id, "race completion item"
    )
    push_r2 = _push_receipt(
        item2["work_id"],
        item2["revision_id"],
        final2["candidate_id"],
        final2["commit_sha"],
        candidate_sha256=final2["candidate_sha256"],
        repository=attempt2["repository"],
    )
    _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push_r2}})
    _drain_deliveries(service, workspace_id, key=item2["key"])
    _record_review(service, workspace_id, item2, final2, attempt2)
    _drain_deliveries(service, workspace_id, key=item2["key"])

    workflow2 = service.client.get(
        f"/v1/work-items/{item2['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    valid_evidence2 = _build_completion_evidence_from_view(workflow2, push_r2["receipt_id"])
    completion_input2 = {
        "work_id": item2["work_id"],
        "current_revision_id": item2["revision_id"],
        "candidate": workflow2["item"]["candidate"],
        "receipts": [
            r for r in workflow2["receipts"] if r["candidate_id"] == final2["candidate_id"]
        ],
        "closeout_requested": True,
    }

    race_payload_1 = {
        "input": completion_input2,
        "attempt_id": attempt2["attempt_id"],
        "done_authorization_ref": f"done:{uuid4()}",
        "evidence": valid_evidence2,
    }
    race_payload_2 = {
        "input": completion_input2,
        "attempt_id": attempt2["attempt_id"],
        "done_authorization_ref": f"done:{uuid4()}",
        "evidence": valid_evidence2,
    }

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(
            _command, service, workspace_id, {"type": "complete_work", "payload": race_payload_1}
        )
        f2 = executor.submit(
            _command, service, workspace_id, {"type": "complete_work", "payload": race_payload_2}
        )
        r1 = f1.result()
        r2 = f2.result()

    applied_count = sum(1 for (st, bd) in (r1, r2) if st == 200 and bd.get("result", {}).get("status") == "applied")
    assert applied_count == 1, f"Expected exactly 1 applied completion, got {r1} and {r2}"



def test_complete_execution_item_allows_retained_terminal_predecessor(service) -> None:
    """A terminal predecessor's preserved edge must not prevent execution completion."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _create(service, workspace_id, "terminal predecessor")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_work_state",
            "payload": {"work_id": predecessor["work_id"], "state": "CANCELED"},
        },
    )
    assert status == 200, body
    (
        grant_id,
        work_id,
        _revision_id,
        _candidate_id,
        attempt_id,
        push_id,
        judge_sha,
        item,
    ) = _execution_grant_audited_attempt(
        service, workspace_id, predecessor_ids=[predecessor["work_id"]]
    )
    workflow = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": work_id,
                "attempt_id": attempt_id,
                "evidence": _build_completion_evidence_from_view(workflow, push_id),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["state"] == "DONE"
    after = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert after["relations"] == workflow["relations"]
    assert after["item"]["state"] == "DONE"


def test_complete_work_refuses_retained_nonterminal_predecessor(service) -> None:
    """Otherwise-valid ordinary completion must not bypass an unfinished predecessor."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id)
    predecessor = _create(service, workspace_id, "unfinished predecessor")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": predecessor["work_id"],
                    "target_work_id": item["work_id"],
                    "kind": "blocks",
                    "active": True,
                }
            },
        },
    )
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _record_review(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _push_receipt(
        item["work_id"],
        item["revision_id"],
        final["candidate_id"],
        final["commit_sha"],
        candidate_sha256=final["candidate_sha256"],
        repository="/repo",
    )
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}}
    )
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])
    before = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    status, body = _complete(
        service, workspace_id, item, final, attempt["attempt_id"], key=item["key"]
    )
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "completion_blocked"
    after = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    ).json()
    assert after["item"] == before["item"]
    assert after["relations"] == before["relations"]
    assert after["close_attempts"] == before["close_attempts"]
    assert after["checkpoint_deliveries"] == before["checkpoint_deliveries"]


def _p3_relation(
    service,
    workspace_id,
    source: dict,
    target: dict,
    kind: str = "blocks",
    *,
    remove: bool = False,
) -> None:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "remove_relation" if remove else "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": source["work_id"],
                    "target_work_id": target["work_id"],
                    "kind": kind,
                    "active": True,
                }
            },
        },
    )
    assert status == 200, body


def _p3_state(service, workspace_id, item: dict, state: str) -> None:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_work_state",
            "payload": {"work_id": item["work_id"], "state": state},
        },
    )
    assert status == 200, body


def _p3_workflow(service, workspace_id, item: dict) -> dict:
    response = service.client.get(
        f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _p3_terminal(service, workspace_id, state: str) -> dict:
    if state == "DONE":
        item, final, attempt = _audited_attempt(
            service, workspace_id, "completed predecessor"
        )
        status, body = _close_ritual(service, workspace_id, item, final, attempt)
        assert status == 200 and body["result"]["status"] == "applied", body
    else:
        item = _create(service, workspace_id, "predecessor")
        _p3_state(service, workspace_id, item, state)
    return item


def _p3_ready(
    service,
    workspace_id,
    *,
    execution: bool = False,
    predecessors: list[str] | None = None,
    riders: list[dict] | None = None,
    after_begin: Callable[[], None] | None = None,
    owner_started_at: str | None = None,
) -> dict:
    if execution:
        grant, work, revision, _candidate, attempt_id, _push, judge, item = (
            _execution_grant_audited_attempt(
                service,
                workspace_id,
                predecessor_ids=predecessors,
                riders=riders,
                after_begin=after_begin,
                owner_started_at=owner_started_at,
            )
        )
        item = dict(item, revision_id=revision)
        view = _p3_workflow(service, workspace_id, item)
        final = view["item"]["candidate"]
        attempt = next(
            a for a in view["close_attempts"] if a["attempt_id"] == attempt_id
        )
    else:
        item, final, attempt = _audited_attempt(service, workspace_id, riders=riders)
        grant = judge = None
    _prepare_closeout(service, workspace_id, item, final, attempt)
    return {
        "item": item,
        "final": final,
        "attempt": attempt,
        "grant": grant,
        "judge": judge,
    }


def _p3_payload(service, workspace_id, ready: dict, **extra) -> dict:
    view = _p3_workflow(service, workspace_id, ready["item"])
    final = view["item"]["candidate"]
    return {
        "input": {
            "work_id": ready["item"]["work_id"],
            "current_revision_id": ready["item"]["revision"]["revision_id"]
            if "revision" in ready["item"]
            else ready["item"]["revision_id"],
            "candidate": final,
            "receipts": [
                r
                for r in view["receipts"]
                if r["candidate_id"] == final["candidate_id"]
            ],
            "closeout_requested": True,
        },
        "attempt_id": ready["attempt"]["attempt_id"],
        "done_authorization_ref": f"done:{uuid4()}",
        "evidence": _build_completion_evidence_from_view(view),
        **extra,
    }


def _p3_complete(
    service, workspace_id, payload: dict, *, operation_id=None
) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {"type": "complete_work", "payload": payload},
        operation_id=operation_id,
    )


@pytest.mark.parametrize("state", ["DONE", "CANCELED", "CANCELLED"])
@pytest.mark.parametrize("execution", [False, True])
def test_p3_terminal_edges_allow_both_complete_work_bindings(
    service, state: str, execution: bool
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, state)
    ready = _p3_ready(
        service,
        workspace_id,
        execution=execution,
        predecessors=[predecessor["work_id"]] if execution else None,
    )
    if not execution:
        _p3_relation(service, workspace_id, predecessor, ready["item"])
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _p3_complete(
        service, workspace_id, _p3_payload(service, workspace_id, ready)
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"]["state"] == "DONE" and after["relations"] == before["relations"]
    )
    assert _p3_workflow(service, workspace_id, predecessor)["item"]["state"] == state


@pytest.mark.parametrize("execution", [False, True])
def test_p3_validated_cancellation_projects_only_its_own_terminal_write(
    service, execution: bool
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(
        service,
        workspace_id,
        execution=execution,
        predecessors=[predecessor["work_id"]] if execution else None,
    )
    if not execution:
        _p3_relation(service, workspace_id, predecessor, ready["item"])
    _p3_state(service, workspace_id, predecessor, "BACKLOG")
    before = _p3_workflow(service, workspace_id, ready["item"])
    payload = _p3_payload(
        service,
        workspace_id,
        ready,
        cancellations=[
            {
                "work_id": predecessor["work_id"],
                "revision_id": predecessor["revision_id"],
                "reason": "superseded within this validated batch",
            }
        ],
    )
    op = uuid4()
    status, body = _p3_complete(service, workspace_id, payload, operation_id=op)
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["canceled_work_ids"] == [predecessor["work_id"]]
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"]["state"] == "DONE" and after["relations"] == before["relations"]
    )
    assert (
        _p3_workflow(service, workspace_id, predecessor)["item"]["state"] == "CANCELED"
    )
    status, replay = _p3_complete(service, workspace_id, payload, operation_id=op)
    assert (
        status == 200
        and replay["receipt"]["state"] == "replayed"
        and replay["result"] == body["result"]
    )
    assert _p3_workflow(service, workspace_id, ready["item"]) == after


@pytest.mark.parametrize("execution", [False, True])
@pytest.mark.parametrize(
    "fault,reason",
    [("cancel", "cancel_binding_invalid"), ("later-child", "child_receipt_invalid")],
)
def test_p3_invalid_batch_proof_wins_before_predecessor_refusal(
    service, fault: str, reason: str, execution: bool
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(
        service,
        workspace_id,
        execution=execution,
        predecessors=[predecessor["work_id"]] if execution else None,
    )
    if not execution:
        _p3_relation(service, workspace_id, predecessor, ready["item"])
    _p3_state(service, workspace_id, predecessor, "BACKLOG")
    proof = {
        "work_id": predecessor["work_id"],
        "revision_id": predecessor["revision_id"],
        "reason": "validated cancellation required",
    }
    extra = {}
    if fault == "cancel":
        proof["revision_id"] = str(uuid4())
    else:
        child = _create(service, workspace_id, "unproven later child")
        _p3_relation(service, workspace_id, child, ready["item"], "parent")
        extra["satisfied_work_ids"] = [child["work_id"]]
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _p3_complete(
        service,
        workspace_id,
        _p3_payload(service, workspace_id, ready, cancellations=[proof], **extra),
    )
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == reason
    ), body
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["relations"] == before["relations"]
    )
    assert (
        _p3_workflow(service, workspace_id, predecessor)["item"]["state"] == "BACKLOG"
    )


@pytest.mark.parametrize("execution", [False, True])
@pytest.mark.parametrize("state", ["BACKLOG", "done", "cancelled", "UNFAMILIAR"])
def test_p3_repairable_state_refusal_preserves_success_authorization(
    service, execution: bool, state: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(
        service,
        workspace_id,
        execution=execution,
        predecessors=[predecessor["work_id"]] if execution else None,
    )
    if not execution:
        _p3_relation(service, workspace_id, predecessor, ready["item"])
    _p3_state(service, workspace_id, predecessor, state)
    before = _p3_workflow(service, workspace_id, ready["item"])
    payload = _p3_payload(service, workspace_id, ready)
    status, body = _p3_complete(service, workspace_id, payload)
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "completion_blocked"
    ), body
    assert (
        not body["result"]["event"]["requires_fresh_authorization"]
        and not body["result"]["event"]["requires_delivery"]
    )
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["checkpoint_deliveries"] == before["checkpoint_deliveries"]
    )
    _p3_state(service, workspace_id, predecessor, "CANCELED")
    status, completed = _p3_complete(service, workspace_id, payload)
    assert status == 200 and completed["result"]["status"] == "applied", completed


@pytest.mark.parametrize("state", ["DONE", "CANCELLED"])
def test_p3_execution_completion_keeps_other_terminal_spellings_nonblocking(
    service, state: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, state)
    ready = _p3_ready(
        service, workspace_id, execution=True, predecessors=[predecessor["work_id"]]
    )
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "complete_execution_item",
            "payload": {
                "grant_id": ready["grant"],
                "expected_grant_version": 3,
                "work_id": ready["item"]["work_id"],
                "attempt_id": ready["attempt"]["attempt_id"],
                "evidence": _build_completion_evidence_from_view(before),
                "judge_sha256": ready["judge"],
            },
        },
    )
    assert status == 200 and body["result"]["state"] == "DONE", body
    assert (
        _p3_workflow(service, workspace_id, ready["item"])["relations"]
        == before["relations"]
    )


def _p3_child_receipt(service, workspace_id, ready: dict, child: dict) -> None:
    _p3_relation(service, workspace_id, child, ready["item"], "parent")
    attempt = ready["attempt"]
    receipt = _receipt(
        child["work_id"],
        child["revision_id"],
        ready["final"]["candidate_id"],
        "same_session_found_fixed",
        body={
            "attempt_id": attempt["attempt_id"],
            "owner_session_id": attempt["owner_session_id"],
            "base_commit": attempt["owner_session_start_commit"],
            "fix_commit": ready["final"]["commit_sha"],
            "candidate_sha256": ready["final"]["candidate_sha256"],
            "finding": "same-session child fixed",
            "verification": "disposable service fixture verifies child contract",
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 200, body


@pytest.mark.parametrize("execution", [False, True])
def test_p3_validated_child_projects_done_without_removing_incoming_edge(
    service, execution: bool
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    started = datetime.now(timezone.utc).isoformat()
    if execution:
        child = _p3_terminal(service, workspace_id, "CANCELED")
        ready = _p3_ready(
            service,
            workspace_id,
            execution=True,
            predecessors=[child["work_id"]],
            owner_started_at=started,
        )
        _p3_state(service, workspace_id, child, "BACKLOG")
    else:
        ready = _p3_ready(service, workspace_id)
        child = _create(service, workspace_id, "satisfied blocking child")
        _p3_state(service, workspace_id, child, "unfamiliar-child")
        _p3_relation(service, workspace_id, child, ready["item"])
    _p3_child_receipt(service, workspace_id, ready, child)
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _p3_complete(
        service,
        workspace_id,
        _p3_payload(
            service, workspace_id, ready, satisfied_work_ids=[child["work_id"]]
        ),
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert child["work_id"] in body["result"]["completed_work_ids"]
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["relations"] == before["relations"]
        and _p3_workflow(service, workspace_id, child)["item"]["state"] == "DONE"
    )


@pytest.mark.parametrize("execution", [False, True])
def test_p3_validated_blocking_rider_projects_done_only_for_complete_work(
    service, execution: bool
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = (
        _p3_terminal(service, workspace_id, "CANCELED")
        if execution
        else _create(service, workspace_id, "blocking rider")
    )
    riders = [
        {
            "work_id": rider["work_id"],
            "revision_id": rider["revision_id"],
            "evidence": "the exact rider proof sealed in this real-service fixture",
        }
    ]
    ready = _p3_ready(
        service,
        workspace_id,
        execution=execution,
        predecessors=[rider["work_id"]] if execution else None,
        riders=riders,
        after_begin=(lambda: _p3_state(service, workspace_id, rider, "BACKLOG"))
        if execution
        else None,
    )
    if not execution:
        _p3_relation(service, workspace_id, rider, ready["item"])
    before = _p3_workflow(service, workspace_id, ready["item"])
    if execution:
        status, strict = _command(
            service,
            workspace_id,
            {
                "type": "complete_execution_item",
                "payload": {
                    "grant_id": ready["grant"],
                    "expected_grant_version": 3,
                    "work_id": ready["item"]["work_id"],
                    "attempt_id": ready["attempt"]["attempt_id"],
                    "evidence": _build_completion_evidence_from_view(before),
                    "judge_sha256": ready["judge"],
                },
            },
        )
        assert status == 409 and strict["error"]["code"] == "completion_blocked", strict
        assert _p3_workflow(service, workspace_id, ready["item"]) == before
    status, body = _p3_complete(
        service, workspace_id, _p3_payload(service, workspace_id, ready)
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    assert rider["work_id"] in body["result"]["completed_work_ids"]
    assert _p3_workflow(service, workspace_id, rider)["item"]["state"] == "DONE"
    assert (
        _p3_workflow(service, workspace_id, ready["item"])["relations"]
        == before["relations"]
    )


def test_p3_invalid_rider_proof_precedes_valid_blocker_cancellation(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "drifting rider")
    ready = _p3_ready(
        service,
        workspace_id,
        riders=[
            {
                "work_id": rider["work_id"],
                "revision_id": rider["revision_id"],
                "evidence": "sealed before supported cancellation",
            }
        ],
    )
    blocker = _create(service, workspace_id, "cancelable predecessor")
    _p3_relation(service, workspace_id, blocker, ready["item"])
    _p3_state(service, workspace_id, rider, "CANCELED")
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _p3_complete(
        service,
        workspace_id,
        _p3_payload(
            service,
            workspace_id,
            ready,
            cancellations=[
                {
                    "work_id": blocker["work_id"],
                    "revision_id": blocker["revision_id"],
                    "reason": "valid cancellation does not hide invalid rider",
                }
            ],
        ),
    )
    assert (
        status == 200
        and body["result"]["event"]["reason_code"] == "rider_binding_invalid"
    ), body
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["relations"] == before["relations"]
    )
    assert _p3_workflow(service, workspace_id, blocker)["item"]["state"] == "BACKLOG"


@pytest.mark.parametrize("route", ["complete_work", "complete_execution_item"])
@pytest.mark.parametrize("change", ["add", "remove"])
def test_p3_execution_completion_refuses_changed_terminal_source_set(
    service, route: str, change: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(
        service, workspace_id, execution=True, predecessors=[predecessor["work_id"]]
    )
    if change == "remove":
        _p3_relation(service, workspace_id, predecessor, ready["item"], remove=True)
    else:
        extra = _create(service, workspace_id, "new terminal predecessor")
        _p3_state(service, workspace_id, extra, "CANCELED")
        _p3_relation(service, workspace_id, extra, ready["item"])
    before = _p3_workflow(service, workspace_id, ready["item"])
    execution_before = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{ready['grant']}",
        headers=_owner_headers(workspace_id),
    ).json()
    if route == "complete_work":
        status, body = _p3_complete(
            service, workspace_id, _p3_payload(service, workspace_id, ready)
        )
        assert (
            status == 200
            and body["result"]["event"]["reason_code"] == "completion_blocked"
        ), body
        assert not body["result"]["event"]["requires_delivery"]
    else:
        status, body = _command(
            service,
            workspace_id,
            {
                "type": route,
                "payload": {
                    "grant_id": ready["grant"],
                    "expected_grant_version": 3,
                    "work_id": ready["item"]["work_id"],
                    "attempt_id": ready["attempt"]["attempt_id"],
                    "evidence": _build_completion_evidence_from_view(before),
                    "judge_sha256": ready["judge"],
                },
            },
        )
        assert status == 409 and body["error"]["code"] == "completion_blocked", body
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["checkpoint_deliveries"] == before["checkpoint_deliveries"]
    )
    execution_after = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{ready['grant']}",
        headers=_owner_headers(workspace_id),
    ).json()
    assert execution_after == execution_before


def _p3_begin_payload(
    service, workspace_id, item: dict, predecessors: list[str]
) -> dict:
    judge, manifest = _tcb_manifest()
    return {
        "grant_id": str(uuid4()),
        "provenance": {
            "owner_input_id": str(uuid4()),
            "owner_session_id": "session-1",
            "normalized_command": f"/execute {item['key']}",
            "workspace_id": str(workspace_id),
            "repository": "theturtlecsz/oh-my-pi",
            "nonce": str(uuid4()),
            "issued_at": datetime.now(timezone.utc).isoformat(),
        },
        "remote_ref": "refs/heads/main",
        "mode": "single",
        "items": [
            {
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "position": 0,
                "original_request": "admitted pending work",
                "original_request_sha256": text_sha256("admitted pending work"),
                "initial_git_baseline": "0" * 40,
                "active_blocker_ids": predecessors,
            }
        ],
        "expected_focus_version": 0,
        "judge_sha256": judge,
        "judge_manifest": manifest,
    }


@pytest.mark.parametrize("state", ["DONE", "CANCELED", "CANCELLED"])
def test_p3_admission_and_activation_retain_terminal_edges(service, state: str) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, state)
    item = _create(
        service, workspace_id, "activation target", description="admitted pending work"
    )
    _p3_relation(service, workspace_id, predecessor, item)
    before = _p3_workflow(service, workspace_id, item)
    payload = _p3_begin_payload(service, workspace_id, item, [predecessor["work_id"]])
    anchor = _create(
        service, workspace_id, "first queued item", description="admitted pending work"
    )
    payload["mode"] = "queue"
    payload["items"][0]["position"] = 1
    payload["items"].insert(
        0,
        {
            "work_id": anchor["work_id"],
            "revision_id": anchor["revision_id"],
            "position": 0,
            "original_request": "admitted pending work",
            "original_request_sha256": text_sha256("admitted pending work"),
            "initial_git_baseline": "0" * 40,
            "active_blocker_ids": [],
        },
    )
    status, body = _command(
        service, workspace_id, {"type": "begin_execution", "payload": payload}
    )
    assert status == 200, body
    status, active = _command(
        service,
        workspace_id,
        {
            "type": "activate_execution_item",
            "payload": {
                "grant_id": payload["grant_id"],
                "expected_grant_version": 1,
                "position": 1,
                "work_id": item["work_id"],
                "expected_revision_id": item["revision_id"],
                "git_baseline": "0" * 40,
                "judge_sha256": payload["judge_sha256"],
                "expected_focus_version": 1,
                "expected_blocker_ids": [predecessor["work_id"]],
            },
        },
    )
    assert status == 200 and active["result"]["grant"]["grant_version"] == 2, active
    assert _p3_workflow(service, workspace_id, item)["relations"] == before["relations"]


@pytest.mark.parametrize(
    "fault", ["snapshot", "reopen", "state-spelling", "edge-drift"]
)
def test_p3_admission_activation_refusals_preserve_grant_and_focus(
    service, fault: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    item = _create(
        service, workspace_id, "guarded activation", description="admitted pending work"
    )
    _p3_relation(service, workspace_id, predecessor, item)
    payload = _p3_begin_payload(
        service,
        workspace_id,
        item,
        [] if fault == "snapshot" else [predecessor["work_id"]],
    )
    if fault == "state-spelling":
        _p3_state(service, workspace_id, predecessor, "canceled")
    if fault in ("snapshot", "state-spelling"):
        status, body = _command(
            service, workspace_id, {"type": "begin_execution", "payload": payload}
        )
        assert status == 400 and body["error"]["code"] == "invalid_request", body
        assert any("blocking" in d for d in body["error"]["diagnostics"]), body
        with psycopg.connect(
            **service.config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM omp_work.execution_grants WHERE workspace_id=%s",
                    (workspace_id,),
                ).fetchone()[0]
                == 0
            )
        return
    status, body = _command(
        service, workspace_id, {"type": "begin_execution", "payload": payload}
    )
    assert status == 200, body
    if fault == "reopen":
        _p3_state(service, workspace_id, predecessor, "BACKLOG")
    else:
        _p3_relation(service, workspace_id, predecessor, item, remove=True)
    before = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{payload['grant_id']}",
        headers=_owner_headers(workspace_id),
    ).json()
    status, denied = _command(
        service,
        workspace_id,
        {
            "type": "activate_execution_item",
            "payload": {
                "grant_id": payload["grant_id"],
                "expected_grant_version": 1,
                "position": 0,
                "work_id": item["work_id"],
                "expected_revision_id": item["revision_id"],
                "git_baseline": "0" * 40,
                "judge_sha256": payload["judge_sha256"],
                "expected_focus_version": 1,
                "expected_blocker_ids": [predecessor["work_id"]],
            },
        },
    )
    assert status == 400 and denied["error"]["code"] == "invalid_request", denied
    assert any("blocking" in d for d in denied["error"]["diagnostics"]), denied
    assert (
        service.client.get(
            f"/v1/workspaces/{workspace_id}/execution/{payload['grant_id']}",
            headers=_owner_headers(workspace_id),
        ).json()
        == before
    )


@pytest.mark.parametrize("kind", ["summary", "legacy"])
def test_p3_complete_work_refuses_otherwise_valid_inconsistent_execution_binding(
    service, kind: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    grant, work, revision, _candidate, attempt_id, _push, judge, item = (
        _execution_grant_audited_attempt(service, workspace_id, authorization_kind=kind)
    )
    item = dict(item, revision_id=revision)
    view = _p3_workflow(service, workspace_id, item)
    final = view["item"]["candidate"]
    attempt = next(a for a in view["close_attempts"] if a["attempt_id"] == attempt_id)
    _prepare_closeout(service, workspace_id, item, final, attempt)
    ready = {"item": item, "final": final, "attempt": attempt}
    before = _p3_workflow(service, workspace_id, item)
    status, body = _p3_complete(
        service, workspace_id, _p3_payload(service, workspace_id, ready)
    )
    assert (
        status == 200 and body["result"]["event"]["reason_code"] == "completion_blocked"
    ), body
    assert "inconsistent execution binding" in body["result"]["event"]["reason"]
    after = _p3_workflow(service, workspace_id, item)
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["checkpoint_deliveries"] == before["checkpoint_deliveries"]
    )


def test_p3_missing_predecessor_is_rejected_by_real_relation_foreign_key(
    service,
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id)
    before = _p3_workflow(service, workspace_id, item)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": str(uuid4()),
                    "target_work_id": item["work_id"],
                    "kind": "blocks",
                    "active": True,
                }
            },
        },
    )
    assert status >= 400 and "error" in body, body
    assert _p3_workflow(service, workspace_id, item) == before


def _p3_bound_transactions(monkeypatch, workspace_id) -> None:
    original = PostgresWorkStore._transaction

    @contextmanager
    def bounded(store, workspace, actor, *, serializable=False):
        with original(store, workspace, actor, serializable=serializable) as cursor:
            if str(workspace) == str(workspace_id):
                cursor.execute("SET LOCAL statement_timeout = '3000ms'")
            yield cursor

    monkeypatch.setattr(PostgresWorkStore, "_transaction", bounded)


def _p3_wait_for_database_lock(service) -> None:
    deadline = time.monotonic() + 10
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute("SET statement_timeout = '3000ms'")
        while time.monotonic() < deadline:
            if conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s AND wait_event_type='Lock'",
                (service.config.database,),
            ).fetchone()[0]:
                return
            time.sleep(0.02)
    pytest.fail("Concurrent command never reached an observed PostgreSQL lock wait")


def _p3_operation_attempts(service, workspace_id, operation_id) -> list[tuple]:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        return conn.execute(
            "SELECT state, attempt_count, result_sha256 FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s",
            (workspace_id, operation_id),
        ).fetchall()


def test_p3_uncommitted_reopen_waits_then_refuses_without_consuming_success(
    service, monkeypatch, record_property
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _p3_bound_transactions(monkeypatch, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(service, workspace_id)
    _p3_relation(service, workspace_id, predecessor, ready["item"])
    payload = _p3_payload(service, workspace_id, ready)
    before = _p3_workflow(service, workspace_id, ready["item"])
    written = threading.Event()
    release = threading.Event()
    results = {}
    original = PostgresWorkStore._set_state

    def hold_reopen(store, cursor, envelope):
        result = original(store, cursor, envelope)
        if (
            str(envelope.workspace_id) == str(workspace_id)
            and str(envelope.command.payload.work_id) == predecessor["work_id"]
        ):
            written.set()
            assert release.wait(10), "reopen release timed out"
        return result

    monkeypatch.setattr(PostgresWorkStore, "_set_state", hold_reopen)

    def reopen():
        results["reopen"] = _command(
            service,
            workspace_id,
            {
                "type": "set_work_state",
                "payload": {"work_id": predecessor["work_id"], "state": "BACKLOG"},
            },
        )

    operation_id = uuid4()

    def complete():
        results["complete"] = _p3_complete(
            service, workspace_id, payload, operation_id=operation_id
        )

    writer = threading.Thread(target=reopen)
    reader = threading.Thread(target=complete)
    try:
        writer.start()
        assert written.wait(10), "supported reopen never wrote its uncommitted state"
        reader.start()
        _p3_wait_for_database_lock(service)
    finally:
        release.set()
        writer.join(15)
        if reader.ident is not None:
            reader.join(15)
    assert not writer.is_alive() and not reader.is_alive(), (
        "bounded concurrent commands did not settle"
    )
    assert results["reopen"][0] == 200
    status, body = results["complete"]
    assert (
        status == 200
        and body["result"]["status"] == "refused"
        and body["result"]["event"]["reason_code"] == "completion_blocked"
    ), body
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["checkpoint_deliveries"] == before["checkpoint_deliveries"]
    )
    attempts = _p3_operation_attempts(service, workspace_id, operation_id)
    assert len(attempts) == 1 and 1 <= attempts[0][1] <= 3
    record_property(
        "p3_real_pg_observation",
        json.dumps(
            {
                "case": "uncommitted reopen",
                "operation": str(operation_id),
                "result": body,
                "committedAttemptCount": attempts[0][1],
                "predecessorState": _p3_workflow(service, workspace_id, predecessor)[
                    "item"
                ]["state"],
            }
        ),
    )


def test_p3_three_real_serialization_conflicts_surface_unavailable_without_completion(
    service, monkeypatch, record_property
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _p3_bound_transactions(monkeypatch, workspace_id)
    predecessor = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(service, workspace_id)
    _p3_relation(service, workspace_id, predecessor, ready["item"])
    payload = _p3_payload(service, workspace_id, ready)
    before = _p3_workflow(service, workspace_id, ready["item"])
    original = PostgresWorkStore._observe_predecessors
    conflicts = []
    writes = []

    def change_after_snapshot(store, cursor, workspace, work, terminal_post_state=None):
        if str(workspace) != str(workspace_id) or str(work) != ready["item"]["work_id"]:
            return original(store, cursor, workspace, work, terminal_post_state)
        state = "CANCELLED" if len(writes) % 2 == 0 else "CANCELED"
        result = {}

        def change():
            result["command"] = _command(
                service,
                workspace_id,
                {
                    "type": "set_work_state",
                    "payload": {"work_id": predecessor["work_id"], "state": state},
                },
            )

        thread = threading.Thread(target=change)
        thread.start()
        thread.join(10)
        assert not thread.is_alive() and result["command"][0] == 200, result
        writes.append(result["command"][1])
        try:
            return original(store, cursor, workspace, work, terminal_post_state)
        except psycopg.Error as error:
            conflicts.append(error.sqlstate)
            raise

    monkeypatch.setattr(
        PostgresWorkStore, "_observe_predecessors", change_after_snapshot
    )
    operation_id = uuid4()
    status, body = _p3_complete(
        service, workspace_id, payload, operation_id=operation_id
    )
    assert (
        status == 503
        and body["error"]["code"] == "unavailable"
        and body["error"]["diagnostics"] == ["retry_exhausted"]
    ), body
    assert conflicts == ["40001", "40001", "40001"] and len(writes) == 3
    assert _p3_operation_attempts(service, workspace_id, operation_id) == []
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["checkpoint_deliveries"] == before["checkpoint_deliveries"]
    )
    record_property(
        "p3_real_pg_observation",
        json.dumps(
            {
                "case": "three real PG serialization errors",
                "operation": str(operation_id),
                "response": body,
                "observedSqlstates": conflicts,
                "committedMutationOperations": [
                    r["receipt"]["operation_id"] for r in writes
                ],
                "persistedCompletionOperationRows": 0,
            }
        ),
    )


def test_p3_overlapping_edge_insert_can_serialize_after_successful_completion(
    service, monkeypatch, record_property
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _p3_bound_transactions(monkeypatch, workspace_id)
    ready = _p3_ready(service, workspace_id)
    predecessor = _create(service, workspace_id, "later incoming source")
    payload = _p3_payload(service, workspace_id, ready)
    observed = threading.Event()
    release = threading.Event()
    original = PostgresWorkStore._observe_predecessors
    results = {}

    def hold_after_observation(
        store, cursor, workspace, work, terminal_post_state=None
    ):
        result = original(store, cursor, workspace, work, terminal_post_state)
        if (
            str(workspace) == str(workspace_id)
            and str(work) == ready["item"]["work_id"]
        ):
            observed.set()
            assert release.wait(10), "completion release timed out"
        return result

    monkeypatch.setattr(
        PostgresWorkStore, "_observe_predecessors", hold_after_observation
    )
    completion_id = uuid4()
    edge_id = uuid4()

    def complete():
        results["complete"] = _p3_complete(
            service, workspace_id, payload, operation_id=completion_id
        )

    def edge():
        results["edge"] = _command(
            service,
            workspace_id,
            {
                "type": "put_relation",
                "payload": {
                    "relation": {
                        "workspace_id": str(workspace_id),
                        "source_work_id": predecessor["work_id"],
                        "target_work_id": ready["item"]["work_id"],
                        "kind": "blocks",
                        "active": True,
                    }
                },
            },
            operation_id=edge_id,
        )

    reader = threading.Thread(target=complete)
    writer = threading.Thread(target=edge)
    try:
        reader.start()
        assert observed.wait(10), "completion did not observe predecessor set"
        writer.start()
        _p3_wait_for_database_lock(service)
    finally:
        release.set()
        reader.join(15)
        if writer.ident is not None:
            writer.join(15)
    assert not reader.is_alive() and not writer.is_alive()
    assert (
        results["complete"][0] == 200
        and results["complete"][1]["result"]["status"] == "applied"
    ), results
    assert results["edge"][0] == 200, results
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert after["item"]["state"] == "DONE" and any(
        r["source_work_id"] == predecessor["work_id"]
        and r["kind"] == "blocks"
        and r["active"]
        for r in after["relations"]
    )
    record_property(
        "p3_real_pg_observation",
        json.dumps(
            {
                "case": "completion serializes before overlapping edge",
                "completion": results["complete"][1],
                "edge": results["edge"][1],
                "completionAttemptRows": _p3_operation_attempts(
                    service, workspace_id, completion_id
                ),
                "edgeAttemptRows": _p3_operation_attempts(
                    service, workspace_id, edge_id
                ),
            }
        ),
    )


def test_p3_valid_cancellation_does_not_excuse_execution_predecessor_seal_drift(
    service,
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    blocker = _p3_terminal(service, workspace_id, "CANCELED")
    ready = _p3_ready(
        service, workspace_id, execution=True, predecessors=[blocker["work_id"]]
    )
    _p3_state(service, workspace_id, blocker, "BACKLOG")
    extra = _create(service, workspace_id, "additional historical source")
    _p3_state(service, workspace_id, extra, "CANCELED")
    _p3_relation(service, workspace_id, extra, ready["item"])
    before = _p3_workflow(service, workspace_id, ready["item"])
    status, body = _p3_complete(
        service,
        workspace_id,
        _p3_payload(
            service,
            workspace_id,
            ready,
            cancellations=[
                {
                    "work_id": blocker["work_id"],
                    "revision_id": blocker["revision_id"],
                    "reason": "valid proof cannot alter admitted source set",
                }
            ],
        ),
    )
    assert (
        status == 200 and body["result"]["event"]["reason_code"] == "completion_blocked"
    ), body
    assert "blocking relations changed" in body["result"]["event"]["reason"]
    after = _p3_workflow(service, workspace_id, ready["item"])
    assert (
        after["item"] == before["item"]
        and after["close_attempts"] == before["close_attempts"]
        and after["relations"] == before["relations"]
    )
    assert _p3_workflow(service, workspace_id, blocker)["item"]["state"] == "BACKLOG"


def test_p3_complete_work_reads_immutable_execution_seal_without_waiting_on_grant_locks(
    service, monkeypatch, record_property
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    ready = _p3_ready(service, workspace_id, execution=True)
    payload = _p3_payload(service, workspace_id, ready)
    _p3_bound_transactions(monkeypatch, workspace_id)
    result = {}

    def complete():
        result["response"] = _p3_complete(service, workspace_id, payload)

    thread = threading.Thread(target=complete)
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as holder:
        holder.execute("SET LOCAL statement_timeout = '3000ms'")
        holder.execute(
            "SELECT grant_id FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s FOR UPDATE",
            (workspace_id, ready["grant"]),
        )
        holder.execute(
            "SELECT item_id FROM omp_work.execution_grant_items WHERE workspace_id=%s AND grant_id=%s AND work_id=%s FOR UPDATE",
            (workspace_id, ready["grant"], ready["item"]["work_id"]),
        )
        thread.start()
        thread.join(2)
        completed_while_locked = not thread.is_alive()
    thread.join(12)
    assert not thread.is_alive() and completed_while_locked, (
        "complete_work blocked on an immutable grant/seal row lock"
    )
    status, body = result["response"]
    assert status == 200 and body["result"]["status"] == "applied", body
    record_property(
        "p3_plain_seal_observation",
        json.dumps(
            {
                "grant": ready["grant"],
                "completedWhileGrantAndItemLocked": completed_while_locked,
                "response": body,
            }
        ),
    )
