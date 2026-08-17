from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
from omp_work.v1.server import create_app

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")

OWNER = uuid4()


def _config(root: Path) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup", "gpg-passphrase", "operator-actor-id"):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(config_dir=root / "config", state_dir=root / "state", data_dir=root / "data", port=port)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("workflow-service")
    config = _config(root)
    with native_postgres(root, config.port):
        bootstrap(config)
        capabilities = root / "capabilities"
        capabilities.mkdir(mode=0o700)
        owner = capabilities / "owner.json"
        owner.write_text(json.dumps({"token": "owner-token", "actor_id": str(OWNER), "actor_kind": "owner", "workspaces": [], "scopes": ["work.read", "work.mutate", "work.approve", "work.close"]}))
        owner.chmod(0o600)
        yield SimpleNamespace(client=TestClient(create_app(config, capabilities_dir=capabilities)), capabilities=capabilities)


def _grant(service, workspace_id) -> None:
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    if str(workspace_id) not in data["workspaces"]:
        data["workspaces"].append(str(workspace_id))
        owner.write_text(json.dumps(data))
        owner.chmod(0o600)


def _owner_headers(workspace_id) -> dict[str, str]:
    return {"Authorization": "Bearer owner-token", "X-OMP-Workspace-ID": str(workspace_id)}


def _command(service, workspace_id, command: dict, *, token: str = "owner-token", operation_id=None) -> tuple[int, dict]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    response = service.client.post("/v1/commands", headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"}, json=envelope)
    return response.status_code, response.json()


def _batch(items: list[dict], relations: list[dict] | None = None) -> dict:
    return {"type": "create_work_batch", "payload": {"items": items, "relations": relations or []}}


def _receipt(work_id, revision_id, candidate_id, kind: str, *, body: dict | None = None, **extra) -> dict:
    body = body if body is not None else {"note": kind}
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
    status, body = _command(service, workspace_id, _batch([{"client_ref": "root", "title": title, **extra}]))
    assert status == 200, body
    return body["result"]["items"][0]


def _plan(service, workspace_id, item: dict, candidate_hash: str | None = None) -> dict:
    receipt = _receipt(item["work_id"], item["revision_id"], str(uuid4()), "plan", candidate_sha256=candidate_hash or secrets.token_hex(32))
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": receipt}})
    assert status == 200, body
    return body["result"]["receipt"]


def _finalize(service, workspace_id, item: dict, planned_id: str, *, commit: str | None = None, final_id=None, candidate_hash: str | None = None) -> tuple[int, dict]:
    payload = {
        "work_id": item["work_id"],
        "revision_id": item["revision_id"],
        "planned_candidate_id": planned_id,
        "candidate_id": str(final_id or uuid4()),
        "candidate_sha256": candidate_hash or secrets.token_hex(32),
        "commit_sha": commit or secrets.token_hex(20),
    }
    return _command(service, workspace_id, {"type": "finalize_candidate", "payload": payload})


def test_rich_batch_atomicity_rollback_and_replay(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    batch = _batch(
        [
            {"client_ref": "parent", "title": "Parent", "scope": "world", "acceptance_criteria": ["children exist"]},
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
    assert [item["client_ref"] for item in items] == ["parent", "child-a", "child-b"]
    assert all(item["row_version"] == 1 for item in items)
    status, replay = _command(service, workspace_id, batch, operation_id=operation_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed" and replay["result"] == body["result"]
    child_a = items[1]
    workflow = service.client.get(f"/v1/work-items/{child_a['key']}/workflow", headers=_owner_headers(workspace_id))
    assert workflow.status_code == 200
    relations = workflow.json()["relations"]
    assert {(relation["kind"], relation["target_work_id"] == items[0]["work_id"]) for relation in relations} == {("parent", True), ("blocks", False)}

    status, body = _command(service, workspace_id, _batch([{"client_ref": "x", "title": "bad", "project_id": str(uuid4())}]))
    assert status == 400 and body["error"]["code"] == "invalid_request"
    status, body = _command(service, workspace_id, _batch(
        [{"client_ref": "a", "title": "A"}, {"client_ref": "b", "title": "B"}],
        [{"source_ref": "a", "target_ref": "b", "kind": "blocks"}, {"source_ref": "b", "target_ref": "a", "kind": "blocks"}],
    ))
    assert status == 400 and body["error"]["code"] == "relation_cycle"
    status, body = _command(service, workspace_id, _batch([{"client_ref": "next", "title": "Next"}]))
    assert status == 200 and body["result"]["items"][0]["key"] == "OMP-4"


def test_revision_invalidation_and_exact_candidate_completion(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "exact candidate")
    plan = _plan(service, workspace_id, item)
    planned_id = plan["candidate_id"]

    status, body = _finalize(service, workspace_id, item, str(uuid4()))
    assert status == 409 and body["error"]["code"] == "stale_evidence"

    status, body = _finalize(service, workspace_id, item, planned_id)
    assert status == 200, body
    final = body["result"]["candidate"]
    assert final["kind"] == "final" and len(final["commit_sha"]) == 40
    assert final["candidate_id"] != planned_id

    wrong_hash = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "verification", candidate_sha256=secrets.token_hex(32), candidate_commit=final["commit_sha"])
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": wrong_hash}})
    assert status == 409 and body["error"]["code"] == "stale_evidence"

    binding = {"candidate_sha256": final["candidate_sha256"], "candidate_commit": final["commit_sha"]}
    verification = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "verification", **binding)
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": verification}})
    assert status == 200
    handoff = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "handoff", body={"resume": "step 3"})
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": handoff}})
    assert status == 200
    audit = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "audit", independent=True, verdict="PASS", **binding)
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": audit}})
    assert status == 200
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"]}})
    assert status == 200 and body["result"]["intent"]["state"] == "pending"

    workflow = service.client.get(f"/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    completion = {
        "work_id": item["work_id"],
        "current_revision_id": item["revision_id"],
        "candidate": workflow["item"]["candidate"],
        "receipts": [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]],
        "closeout_requested": True,
    }
    status, body = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}})
    assert status == 409 and body["error"]["code"] == "completion_blocked"

    closeout = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "closeout", body={"summary": "done"})
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout}})
    assert status == 200
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    completion["receipts"] = [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]]
    status, body = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}})
    assert status == 409 and body["error"]["code"] == "completion_blocked"

    push = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=secrets.token_hex(20))
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}})
    assert status == 200
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    completion["receipts"] = [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]]
    status, body = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}})
    assert status == 409 and body["error"]["code"] == "completion_blocked"


def test_closeout_replay_and_focus_clear(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replay target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    binding = {"candidate_sha256": final["candidate_sha256"], "candidate_commit": final["commit_sha"]}
    for kind, extra in (("verification", {}), ("audit", {"independent": True, "verdict": "PASS"}), ("closeout", {}), ("push", {"remote_ref": "refs/heads/main", "remote_commit": final["commit_sha"]})):
        receipt = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], kind, **binding, **extra)
        status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": receipt}})
        assert status == 200, body
    status, _ = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"]}})
    assert status == 200
    status, body = _command(service, workspace_id, {"type": "set_focus", "payload": {"slot": {"workspace_id": str(workspace_id), "owner_id": str(OWNER), "work_id": item["work_id"], "version": 0}, "expected_version": 0}})
    assert status == 200 and body["result"]["version"] == 1

    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    completion = {"work_id": item["work_id"], "current_revision_id": item["revision_id"], "candidate": workflow["item"]["candidate"], "receipts": [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]], "closeout_requested": True}
    complete_op = uuid4()
    status, body = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}}, operation_id=complete_op)
    assert status == 200 and body["result"]["state"] == "DONE", body
    status, replay = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}}, operation_id=complete_op)
    assert status == 200 and replay["receipt"]["state"] == "replayed" and replay["result"] == body["result"]
    clear_op = uuid4()
    clear = {"type": "clear_focus", "payload": {"workspace_id": str(workspace_id), "owner_id": str(OWNER), "expected_version": 1}}
    status, body = _command(service, workspace_id, clear, operation_id=clear_op)
    assert status == 200 and body["result"]["work_id"] is None
    status, replay = _command(service, workspace_id, clear, operation_id=clear_op)
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    focus = service.client.get(f"/v1/workspaces/{workspace_id}/focus/{OWNER}", headers=_owner_headers(workspace_id)).json()
    assert focus["work_id"] is None and focus["version"] == 2
    stored = service.client.get(f"/v1/operations/{complete_op}", headers=_owner_headers(workspace_id))
    assert stored.status_code == 200 and stored.json()["receipt"]["state"] == "applied" and stored.json()["command_type"] == "complete_work"


def test_negative_audit_permits_replan_on_same_revision(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "audit target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    binding = {"candidate_sha256": final["candidate_sha256"], "candidate_commit": final["commit_sha"]}
    audit = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "audit", independent=True, verdict="NEEDS_FIX", **binding)
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": audit}})
    assert status == 200

    replan = _plan(service, workspace_id, item)
    assert replan["candidate_id"] != plan["candidate_id"]
    status, body = _finalize(service, workspace_id, item, replan["candidate_id"])
    assert status == 200
    second_final = body["result"]["candidate"]
    binding = {"candidate_sha256": second_final["candidate_sha256"], "candidate_commit": second_final["commit_sha"]}
    audit = _receipt(item["work_id"], item["revision_id"], second_final["candidate_id"], "audit", independent=True, verdict="PASS", **binding)
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": audit}})
    assert status == 200
    receipt = _receipt(item["work_id"], item["revision_id"], str(uuid4()), "plan", candidate_sha256=secrets.token_hex(32))
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": receipt}})
    assert status == 409 and body["error"]["code"] == "stale_evidence"

    revision = {
        "revision_id": str(uuid4()),
        "work_id": item["work_id"],
        "revision_number": 2,
        "title": "audit target revised",
        "description": "",
        "scope": "",
        "acceptance_criteria": [],
        "content_sha256": secrets.token_hex(32),
        "created_by": "owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(service, workspace_id, {"type": "revise_work", "payload": {"work_id": item["work_id"], "expected_revision_id": item["revision_id"], "revision": revision}})
    assert status == 200 and body["result"]["changed"] is True
    stale = _receipt(item["work_id"], item["revision_id"], second_final["candidate_id"], "verification", **binding)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": stale}})
    assert status == 409 and body["error"]["code"] == "stale_evidence"
    status, body = _finalize(service, workspace_id, item, replan["candidate_id"])
    assert status == 409 and body["error"]["code"] == "stale_evidence"


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
    reader.write_text(json.dumps({"token": "reader-token", "actor_id": str(uuid4()), "actor_kind": "task-agent", "workspaces": [str(workspace_id)], "scopes": ["work.candidate.read"], "candidate_ids": [final["candidate_id"]]}))
    reader.chmod(0o600)
    broken = service.capabilities / "broken.json"
    broken.write_text(json.dumps({"token": "broken-token", "actor_id": str(uuid4()), "actor_kind": "task-agent", "workspaces": [str(workspace_id)], "scopes": ["work.candidate.read"]}))
    broken.chmod(0o600)

    headers = {"Authorization": "Bearer reader-token", "X-OMP-Workspace-ID": str(workspace_id)}
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=headers)
    assert workflow.status_code == 200
    assert workflow.json()["item"]["candidate"]["candidate_id"] == final["candidate_id"]
    assert workflow.json()["item"]["candidate"]["kind"] == "final"
    assert service.client.get("/v1/work-items/OMP-2/workflow", headers=headers).status_code == 403
    assert service.client.get("/v1/work-items/OMP-1", headers=headers).status_code == 403
    assert service.client.get(f"/v1/workspaces/{workspace_id}/tree", headers=headers).status_code == 403
    assert service.client.get(f"/v1/workspaces/{workspace_id}/focus/{OWNER}", headers=headers).status_code == 403
    status, _ = _command(service, workspace_id, _batch([{"client_ref": "x", "title": "nope"}]), token="reader-token")
    assert status == 403
    assert service.client.get("/v1/work-items/OMP-1/workflow", headers=headers | {"Authorization": "Bearer broken-token"}).status_code == 401
    assert service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(uuid4())).status_code == 403


def test_handoff_and_hash_mismatch_never_satisfy_blockers(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "handoff target")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    handoff = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "handoff", body={"resume": "half-way"})
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": handoff}})
    assert status == 200
    status, _ = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"]}})
    assert status == 200
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    assert any(receipt["kind"] == "handoff" for receipt in workflow["receipts"])
    completion = {"work_id": item["work_id"], "current_revision_id": item["revision_id"], "candidate": workflow["item"]["candidate"], "receipts": [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]], "closeout_requested": True}
    status, body = _command(service, workspace_id, {"type": "complete_work", "payload": {"input": completion}})
    assert status == 409 and body["error"]["code"] == "completion_blocked"

    mismatched = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "verification", candidate_sha256=final["candidate_sha256"], candidate_commit=final["commit_sha"])
    mismatched["payload_sha256"] = secrets.token_hex(32)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": mismatched}})
    assert status == 400 and body["error"]["code"] == "invalid_request"
