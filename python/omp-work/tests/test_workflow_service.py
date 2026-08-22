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

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")

OWNER = uuid4()

PASS_REPORT = "VERDICT: PASS\nFINDINGS\n(none)\nACCEPTANCE COVERAGE\nAC-1 covered\nOUT OF SCOPE\nnone\nCHECKS RUN\nbun test → exit 0\nREMAINING QUESTIONS\nnone"
NEEDS_FIX_REPORT = PASS_REPORT.replace("VERDICT: PASS", "VERDICT: NEEDS_FIX").replace("(none)", "- [major] AC-1 src/x.ts:1 evidence: broken; impact: wrong; minimal fix: revert")
BLOCKED_REPORT = PASS_REPORT.replace("VERDICT: PASS", "VERDICT: BLOCKED")


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
        yield SimpleNamespace(client=TestClient(create_app(config, capabilities_dir=capabilities)), capabilities=capabilities, config=config)


def _grant(service, workspace_id) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
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
    status, body = _command(service, workspace_id, _batch([{"client_ref": "root", "title": title, **extra}]))
    assert status == 200, body
    return body["result"]["items"][0]


def _plan(service, workspace_id, item: dict, candidate_hash: str | None = None) -> dict:
    receipt = _receipt(item["work_id"], item["revision_id"], str(uuid4()), "plan", body={"body": "## Approach\n1. do it\n\n## Verification\n1. prove it"}, candidate_sha256=candidate_hash or secrets.token_hex(32))
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


def _begin(service, workspace_id, item: dict, *, authorization_ref: str | None = None, attempt_id=None, identity: dict | None = None) -> tuple[int, dict]:
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
    return _command(service, workspace_id, {"type": "begin_close_attempt", "payload": payload})


def _verify_and_seal(service, workspace_id, item: dict, final: dict, attempt: dict) -> dict:
    """Append verification, then seal — returns the applied seal result."""
    binding = {"candidate_sha256": final["candidate_sha256"], "candidate_commit": final["commit_sha"]}
    verification = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "verification", **binding)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": verification}})
    assert status == 200, body
    status, body = _command(service, workspace_id, {"type": "seal_audit_manifest", "payload": {"attempt_id": attempt["attempt_id"], "verification_receipt_id": verification["receipt_id"]}})
    assert status == 200, body
    return body["result"]


def _reserve(service, workspace_id, attempt_id: str, task_sha256: str) -> tuple[int, dict]:
    return _command(service, workspace_id, {"type": "reserve_auditor_launch", "payload": {"attempt_id": attempt_id, "task_sha256": task_sha256, "tool_call_id": f"tc-{uuid4()}"}})

def _cancel(service, workspace_id, attempt_id: str, launch_id: str) -> tuple[int, dict]:
    return _command(service, workspace_id, {"type": "cancel_auditor_launch", "payload": {"attempt_id": attempt_id, "launch_id": launch_id}})


def _settle(service, workspace_id, attempt_id: str, launch_id: str, payload_value=None, *, failed: bool = False) -> tuple[int, dict]:
    payload: dict = {"attempt_id": attempt_id, "launch_id": launch_id}
    if failed:
        payload["transport_failed"] = True
    else:
        payload["transport_payload"] = payload_value
    return _command(service, workspace_id, {"type": "settle_auditor_launch", "payload": payload})


def _attest(service, workspace_id, event: dict, status_value: str = "delivered", *, authorization_ref: str | None = None) -> tuple[int, dict]:
    payload = {
        "event_id": event["event_id"],
        "owner_session_id": "session-test",
        "rendered_sha256": event["rendered_sha256"],
        "status": status_value,
    }
    if authorization_ref is not None:
        payload["authorization_ref"] = authorization_ref
    return _command(service, workspace_id, {"type": "attest_checkpoint_delivery", "payload": payload})


def _drain_deliveries(service, workspace_id, key: str = "OMP-1") -> None:
    """Deliver every unresolved requires_delivery event so close gates pass."""
    view = service.client.get(f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)).json()
    latest: dict[str, tuple[int, str]] = {}
    for delivery in view["checkpoint_deliveries"]:
        prior = latest.get(delivery["event_id"])
        if prior is None or delivery["delivery_sequence"] > prior[0]:
            latest[delivery["event_id"]] = (delivery["delivery_sequence"], delivery["status"])
    for event in view["close_attempt_events"]:
        if not event["requires_delivery"]:
            continue
        state = latest.get(event["event_id"])
        if state is not None and state[1] in ("delivered", "waived"):
            continue
        status, body = _attest(service, workspace_id, event)
        assert status == 200 and body["result"]["status"] == "applied", body


def _audited_attempt(service, workspace_id, title: str = "close target") -> tuple[dict, dict, dict]:
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
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_id, json.dumps({"verdict": "PASS", "report": PASS_REPORT}))
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["verdict"] == "PASS", body
    return item, final, body["result"]["attempt"]


def _complete(service, workspace_id, item: dict, final: dict, attempt_id: str, *, done_ref: str | None = None, satisfied: list[str] | None = None, key: str = "OMP-1", operation_id=None) -> tuple[int, dict]:
    workflow = service.client.get(f"/v1/work-items/{key}/workflow", headers=_owner_headers(workspace_id)).json()
    completion = {
        "work_id": item["work_id"],
        "current_revision_id": item["revision_id"],
        "candidate": workflow["item"]["candidate"],
        "receipts": [receipt for receipt in workflow["receipts"] if receipt["candidate_id"] == final["candidate_id"]],
        "closeout_requested": True,
    }
    payload = {
        "input": completion,
        "attempt_id": attempt_id,
        "done_authorization_ref": done_ref or f"done:{uuid4()}",
        **({"satisfied_work_ids": satisfied} if satisfied else {}),
    }
    return _command(service, workspace_id, {"type": "complete_work", "payload": payload}, operation_id=operation_id)


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
    status, replay = _command(service, workspace_id, batch, operation_id=operation_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed" and replay["result"] == body["result"]
    status, body = _command(service, workspace_id, _batch(
        [{"client_ref": "a", "title": "A"}, {"client_ref": "b", "title": "B"}],
        [{"source_ref": "a", "target_ref": "b", "kind": "blocks"}, {"source_ref": "b", "target_ref": "a", "kind": "blocks"}],
    ))
    assert status == 400 and body["error"]["code"] == "relation_cycle"


def test_begin_refuses_without_final_candidate_or_plan(service) -> None:
    # Scenario: keep-open without plan/authorization — typed refusals, never DONE.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "no plan")
    status, body = _begin(service, workspace_id, item)
    assert status == 200, body
    result = body["result"]
    assert result["status"] == "refused" and result["event"]["reason_code"] == "candidate_not_final"
    assert result["event"]["requires_delivery"] is True
    # audit appends are ALWAYS refused — receipts are settle-minted only.
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    binding = {"candidate_sha256": final["candidate_sha256"], "candidate_commit": final["commit_sha"]}
    forged = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "audit", independent=True, verdict="PASS", **binding)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": forged}})
    assert status == 400 and body["error"]["code"] == "invalid_request"
    # request_closeout without an audited attempt refuses too.
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    attempt = body["result"]["attempt"]
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "attempt_not_audited"


def test_manifest_falls_back_to_description_acceptance_criteria(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, description="Context\n\n## Acceptance criteria\n- preserves exact range\n2. reports every check\n\n## Verification\n- not acceptance")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    assert status == 200, body
    seal = _verify_and_seal(service, workspace_id, item, final, body["result"]["attempt"])
    task = seal["manifest"]["task_body"]
    criteria = task.split("Acceptance criteria\n", 1)[1].split("\n\nStarting state", 1)[0]
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
    status, body = _cancel(service, workspace_id, attempt["attempt_id"], first["launch_id"])
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
    status, body = _settle(service, workspace_id, attempt["attempt_id"], second["launch_id"], failed=True)
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
    view = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    manifest = view["audit_manifest"]
    assert manifest is not None and manifest["task_sha256"] == text_sha256(manifest["task_body"])
    assert "Plan receipt SHA-256:" in manifest["task_body"] and f"Final commit: {final['commit_sha']}" in manifest["task_body"]
    # The settle-minted audit receipt is the ONLY audit receipt, independent PASS.
    audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"]
    assert len(audits) == 1 and audits[0]["independent"] is True and audits[0]["verdict"] == "PASS"
    assert audits[0]["issuer"] == "work-service/auditor-settle"
    assert audits[0]["payload"]["report"] == PASS_REPORT
    # Closeout review requires the audited attempt and mints a deliverable checkpoint.
    closeout = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "closeout")
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout}})
    assert status == 200 and body["result"].get("event", {}).get("event_type") == "closeout_review_recorded", body
    # request_closeout refuses while deliveries are pending, then applies.
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "delivery_pending"
    _drain_deliveries(service, workspace_id)
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["attempt"]["state"] == "closeout_requested", body
    # Completion needs the push receipt and a fresh /done authorization.
    push = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=final["commit_sha"])
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}})
    assert status == 200
    status, body = _complete(service, workspace_id, item, final, attempt["attempt_id"], done_ref=attempt["authorization_ref"])
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "done_authorization_not_fresh"
    operation_id = uuid4()
    done_ref = f"done:{uuid4()}"
    status, body = _complete(service, workspace_id, item, final, attempt["attempt_id"], done_ref=done_ref, operation_id=operation_id)
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["state"] == "DONE", body
    status, replay = _complete(service, workspace_id, item, final, attempt["attempt_id"], done_ref=done_ref, operation_id=operation_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed" and replay["result"] == body["result"]
    # A REUSED done authorization on new work refuses.
    item2, final2, attempt2 = _audited_attempt(service, workspace_id, "second")
    closeout2 = _receipt(item2["work_id"], item2["revision_id"], final2["candidate_id"], "closeout")
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout2}})
    assert status == 200
    _drain_deliveries(service, workspace_id, key="OMP-2")
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item2["work_id"], "attempt_id": attempt2["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "applied"
    push2 = _receipt(item2["work_id"], item2["revision_id"], final2["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=final2["commit_sha"])
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push2}})
    assert status == 200
    status, body = _complete(service, workspace_id, item2, final2, attempt2["attempt_id"], done_ref=done_ref, key="OMP-2")
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "done_authorization_reused"


def test_containment_push_receipt_completes_to_done(service) -> None:
    # OMP-99: a push receipt recording a newer same-branch tip (remote_commit)
    # plus the audited candidate (candidate_commit) clears push_unverified.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id)
    closeout = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "closeout")
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout}})
    assert status == 200
    _drain_deliveries(service, workspace_id)
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "applied", body
    tip = "a" * 40 if final["commit_sha"] != "a" * 40 else "b" * 40
    push = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=tip, candidate_commit=final["commit_sha"])
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}})
    assert status == 200
    status, body = _complete(service, workspace_id, item, final, attempt["attempt_id"], done_ref=f"done:{uuid4()}")
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["state"] == "DONE", body


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
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], secrets.token_hex(32))
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "manifest_task_mismatch"
    assert body["result"]["attempt"]["launch_count"] == 0

    # Launch 1: wrapper verdict contradicts the report line — mismatch burns, wrapper not trusted.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "applied"
    launch_1 = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_1, {"verdict": "NEEDS_FIX", "report": PASS_REPORT})
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "report_wrapper_verdict_mismatch"
    assert body["result"]["attempt"]["state"] == "audit_ready" and body["result"]["attempt"]["launch_count"] == 1

    # Launch 2: transport failed before any payload arrived — burns typed.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_2 = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_2, failed=True)
    assert status == 200 and body["result"]["event"]["reason_code"] == "transport_failed"
    assert body["result"]["attempt"]["state"] == "audit_ready" and body["result"]["attempt"]["launch_count"] == 2

    # Launch 3: verdict missing — burns the FINAL launch; the attempt exhausts.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    launch_3 = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_3, "no verdict here")
    assert status == 200 and body["result"]["event"]["reason_code"] == "verdict_missing"
    assert body["result"]["attempt"]["state"] == "budget_exhausted"
    assert body["result"]["event"]["requires_fresh_authorization"] is True

    # A fourth reserve refuses: the attempt is terminal.
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], task_sha)
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "attempt_not_ready"

    # Only a NEW literal /summary (fresh authorization) creates a replacement.
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["launch_count"] == 0


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
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"])
    launch_id = body["result"]["launch"]["launch_id"]

    # Mutate the candidate while the auditor "runs": NEEDS_FIX audit + replan + refinalize.
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as connection:
        connection.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issued_at,issuer,candidate_sha256,candidate_commit,verdict,independent) VALUES(%s,%s,%s,%s,%s,'audit','{}','%s',now(),'test',%s,%s,'NEEDS_FIX',true)" % ("%s", "%s", "%s", "%s", "%s", sha256({}), "%s", "%s"),
            (uuid4(), workspace_id, item["work_id"], item["revision_id"], final["candidate_id"], final["candidate_sha256"], final["commit_sha"]),
        )
    replan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, replan["candidate_id"])
    assert status == 200, body

    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_id, PASS_REPORT)
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "candidate_drift"
    assert body["result"]["attempt"]["state"] == "superseded"
    view = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    settle_audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit" and receipt["issuer"] == "work-service/auditor-settle"]
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
    status, body = _begin(service, workspace_id, item, authorization_ref=ref, identity=identity)
    assert status == 200 and body["result"]["status"] == "applied"
    first = body["result"]["attempt"]
    # Identical authorization AND identity: idempotent resume of the same attempt.
    status, body = _begin(service, workspace_id, item, authorization_ref=ref, identity=identity)
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] == first["attempt_id"]
    assert body["result"]["event"]["event_type"] == "attempt_resumed"
    # Same authorization but drifted identity (fresh attempt_id/diff): typed
    # refusal, no silent rebind.
    status, body = _begin(service, workspace_id, item, authorization_ref=ref)
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "authorization_identity_mismatch"
    status, body = _begin(service, workspace_id, item)
    assert status == 200 and body["result"]["status"] == "applied"
    assert body["result"]["attempt"]["attempt_id"] != first["attempt_id"]
    view = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    live = [attempt for attempt in view["close_attempts"] if attempt["state"] in ("active", "audit_ready", "auditor_in_flight", "audited", "closeout_requested")]
    assert len(live) == 1
    superseded = [attempt for attempt in view["close_attempts"] if attempt["state"] == "superseded"]
    assert len(superseded) == 1 and superseded[0]["terminal_reason"] == "superseded_by_new_summary"
    # A terminal attempt's authorization can never be reused.
    status, body = _begin(service, workspace_id, item, authorization_ref=ref)
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "authorization_exhausted"


def test_remediation_required_blocks_closeout_until_fresh_summary(service) -> None:
    # Scenario: closeout after remediation — NEEDS_FIX terminal state refuses
    # request_closeout with a fresh-authorization requirement.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "remediation")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, item)
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"])
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_id, {"report": NEEDS_FIX_REPORT})
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["verdict"] == "NEEDS_FIX"
    assert body["result"]["attempt"]["state"] == "remediation_required"
    assert body["result"]["event"]["requires_fresh_authorization"] is True
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "refused"
    assert body["result"]["event"]["reason_code"] == "attempt_not_audited"
    assert body["result"]["event"]["requires_fresh_authorization"] is True
    # The NEEDS_FIX receipt IS recorded (accepted report), but completion refuses.
    view = service.client.get("/v1/work-items/OMP-1/workflow", headers=_owner_headers(workspace_id)).json()
    audits = [receipt for receipt in view["receipts"] if receipt["kind"] == "audit"]
    assert len(audits) == 1 and audits[0]["verdict"] == "NEEDS_FIX"


def test_same_session_child_completion_valid_and_invalid(service) -> None:
    # Scenario: valid/invalid same-session receipt — one invalid child refuses
    # the WHOLE completion; valid children complete with the parent.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item, final, attempt = _audited_attempt(service, workspace_id, "parent work")
    # Children created after the owner session started, parent-linked.
    status, body = _command(service, workspace_id, _batch([{"client_ref": "c1", "title": "found+fixed child"}]))
    child = body["result"]["items"][0]
    status, body = _command(service, workspace_id, _batch([{"client_ref": "c2", "title": "unreceipted child"}]))
    stray = body["result"]["items"][0]
    for source in (child, stray):
        status, body = _command(service, workspace_id, {"type": "put_relation", "payload": {"relation": {"workspace_id": str(workspace_id), "source_work_id": source["work_id"], "target_work_id": item["work_id"], "kind": "parent", "active": True}}})
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
    receipt = _receipt(child["work_id"], child["revision_id"], final["candidate_id"], "same_session_found_fixed", body=link)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": receipt}})
    assert status == 200, body
    # A receipt binding the WRONG candidate refuses at append.
    bad_link = dict(link, candidate_sha256=secrets.token_hex(32))
    bad = _receipt(stray["work_id"], stray["revision_id"], final["candidate_id"], "same_session_found_fixed", body=bad_link)
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": bad}})
    assert status == 409 and body["error"]["code"] == "stale_evidence"
    # Close ritual on the parent.
    closeout = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "closeout")
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout}})
    assert status == 200
    _drain_deliveries(service, workspace_id)
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=final["commit_sha"])
    status, _ = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}})
    assert status == 200
    # Invalid child (no receipt) refuses the WHOLE completion — nothing moves.
    status, body = _complete(service, workspace_id, item, final, attempt["attempt_id"], satisfied=[child["work_id"], stray["work_id"]])
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "child_receipt_invalid"
    tree = service.client.get(f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)).json()
    states = {entry["alias"]["key"]: entry["state"] for entry in tree["items"]}
    assert states["OMP-1"] != "DONE" and states["OMP-2"] != "DONE"
    # Valid child alone completes with the parent, atomically.
    status, body = _complete(service, workspace_id, item, final, attempt["attempt_id"], satisfied=[child["work_id"]])
    assert status == 200 and body["result"]["status"] == "applied", body
    assert body["result"]["completed_work_ids"] == [child["work_id"]]
    tree = service.client.get(f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)).json()
    states = {entry["alias"]["key"]: entry["state"] for entry in tree["items"]}
    assert states["OMP-1"] == "DONE" and states["OMP-2"] == "DONE" and states["OMP-3"] != "DONE"


def test_waiver_requires_failed_delivery(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "waiver target")
    # A begin refusal produces a deliverable event.
    status, body = _begin(service, workspace_id, item)
    event = body["result"]["event"]
    assert event["requires_delivery"] is True
    # Waiving before any failed delivery refuses.
    status, body = _attest(service, workspace_id, event, "waived", authorization_ref="waiver:cf-test")
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "waiver_requires_failed"
    # Hash mismatch refuses.
    status, body = _attest(service, workspace_id, {**event, "rendered_sha256": secrets.token_hex(32)})
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "delivery_hash_mismatch"
    # failed → waived succeeds; a second resolution refuses.
    status, body = _attest(service, workspace_id, event, "failed")
    assert status == 200 and body["result"]["status"] == "applied"
    status, body = _attest(service, workspace_id, event, "waived", authorization_ref="waiver:cf-test")
    assert status == 200 and body["result"]["status"] == "applied" and body["result"]["delivery"]["status"] == "waived"
    status, body = _attest(service, workspace_id, event)
    assert status == 200 and body["result"]["status"] == "refused" and body["result"]["event"]["reason_code"] == "delivery_already_resolved"


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

    headers = {"Authorization": "Bearer reader-token", "X-OMP-Workspace-ID": str(workspace_id)}
    workflow = service.client.get("/v1/work-items/OMP-1/workflow", headers=headers)
    assert workflow.status_code == 200
    assert workflow.json()["item"]["candidate"]["candidate_id"] == final["candidate_id"]
    assert service.client.get("/v1/work-items/OMP-2/workflow", headers=headers).status_code == 403
    assert service.client.get("/v1/work-items/OMP-1", headers=headers).status_code == 403
    assert service.client.get(f"/v1/workspaces/{workspace_id}/tree", headers=headers).status_code == 403
    status, _ = _command(service, workspace_id, _batch([{"client_ref": "x", "title": "nope"}]), token="reader-token")
    assert status == 403
    # Close-ritual commands need work.close — a candidate reader has none.
    status, _ = _command(service, workspace_id, {"type": "begin_close_attempt", "payload": {"work_id": item["work_id"], "attempt_id": str(uuid4()), "authorization_ref": "summary:forged", "owner_session_id": "s", "owner_session_started_at": datetime.now(timezone.utc).isoformat(), "owner_session_start_commit": "e" * 40, "repository": "/r", "diff_sha256": secrets.token_hex(32), "starting_dirty_paths": []}}, token="reader-token")
    assert status == 403


def test_stale_service_refuses_writes_and_still_reads(service, monkeypatch: pytest.MonkeyPatch) -> None:
    # OMP-89: once the on-disk source no longer matches the loaded snapshot,
    # every command is refused with a typed restart instruction and no side
    # effects; reads keep working so the ledger stays inspectable.
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "pre-stale item")
    import omp_work.v1.server as server_module
    monkeypatch.setattr(server_module, "code_fingerprint", lambda: "deadbeef")
    status, body = _command(service, workspace_id, _batch([{"client_ref": "stale", "title": "must not land"}]))
    assert status == 503 and body["error"]["code"] == "unavailable"
    assert any("service_stale" in diag for diag in body["error"]["diagnostics"])
    assert any("restart" in diag for diag in body["error"]["diagnostics"])
    workflow = service.client.get(f"/v1/work-items/{item['key']}/workflow", headers=_owner_headers(workspace_id))
    assert workflow.status_code == 200
    monkeypatch.undo()
    status, _ = _command(service, workspace_id, _batch([{"client_ref": "fresh", "title": "lands after restart-equivalent"}]))
    assert status == 200


def _close_ritual(service, workspace_id, item: dict, final: dict, attempt: dict) -> tuple[int, dict]:
    """Post-PASS closeout: review receipt, drain, request, push, complete."""
    closeout = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "closeout")
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": closeout}})
    assert status == 200, body
    _drain_deliveries(service, workspace_id, key=item["key"])
    status, body = _command(service, workspace_id, {"type": "request_closeout", "payload": {"work_id": item["work_id"], "attempt_id": attempt["attempt_id"]}})
    assert status == 200 and body["result"]["status"] == "applied", body
    push = _receipt(item["work_id"], item["revision_id"], final["candidate_id"], "push", remote_ref="refs/heads/main", remote_commit=final["commit_sha"])
    status, body = _command(service, workspace_id, {"type": "append_evidence", "payload": {"receipt": push}})
    assert status == 200, body
    return _complete(service, workspace_id, item, final, attempt["attempt_id"], key=item["key"])


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
        {"work_id": rider_a["work_id"], "revision_id": rider_a["revision_id"], "evidence": "probe: pytest -k rider_a -> 3 passed"},
        {"work_id": rider_b["work_id"], "revision_id": rider_b["revision_id"], "evidence": "probe: artifact b read -> present"},
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
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"])
    assert status == 200 and body["result"]["status"] == "applied", body
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_id, json.dumps({"verdict": "PASS", "report": PASS_REPORT}))
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    status, body = _close_ritual(service, workspace_id, item, final, attempt)
    assert status == 200 and body["result"]["status"] == "applied", body
    assert set(body["result"]["completed_work_ids"]) == {rider_a["work_id"], rider_b["work_id"]}
    for rider in (rider_a, rider_b):
        view = service.client.get(f"/v1/work-items/{rider['key']}", headers=_owner_headers(workspace_id)).json()
        assert view["state"] == "DONE", view
        workflow = service.client.get(f"/v1/work-items/{rider['key']}/workflow", headers=_owner_headers(workspace_id)).json()
        provenance = [event for event in workflow["close_attempt_events"] if event["event_type"] == "rider_completed"]
        assert provenance and "sealed rider" in provenance[0]["reason"], workflow["close_attempt_events"]


def test_rider_binding_refuses_wrong_revision_at_begin(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    rider = _create(service, workspace_id, "mis-sealed rider")
    item = _create(service, workspace_id, "refusal primary")
    plan = _plan(service, workspace_id, item)
    status, body = _finalize(service, workspace_id, item, plan["candidate_id"])
    assert status == 200, body
    bad = [{"work_id": rider["work_id"], "revision_id": item["revision_id"], "evidence": "probe: n/a"}]
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
    riders = [{"work_id": rider["work_id"], "revision_id": rider["revision_id"], "evidence": "probe: superseded"}]
    status, body = _begin(service, workspace_id, item, identity={"riders": riders})
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    seal = _verify_and_seal(service, workspace_id, item, final, attempt)
    status, body = _reserve(service, workspace_id, attempt["attempt_id"], seal["manifest"]["task_sha256"])
    assert status == 200, body
    launch_id = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, attempt["attempt_id"], launch_id, json.dumps({"verdict": "PASS", "report": PASS_REPORT}))
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    # Close the rider through its own full ritual.
    rider_plan = _plan(service, workspace_id, rider)
    status, body = _finalize(service, workspace_id, rider, rider_plan["candidate_id"])
    assert status == 200, body
    rider_final = body["result"]["candidate"]
    status, body = _begin(service, workspace_id, rider)
    assert status == 200 and body["result"]["status"] == "applied", body
    rider_attempt = body["result"]["attempt"]
    rider_seal = _verify_and_seal(service, workspace_id, rider, rider_final, rider_attempt)
    status, body = _reserve(service, workspace_id, rider_attempt["attempt_id"], rider_seal["manifest"]["task_sha256"])
    assert status == 200, body
    rider_launch = body["result"]["launch"]["launch_id"]
    status, body = _settle(service, workspace_id, rider_attempt["attempt_id"], rider_launch, json.dumps({"verdict": "PASS", "report": PASS_REPORT}))
    assert status == 200 and body["result"]["verdict"] == "PASS", body
    status, body = _close_ritual(service, workspace_id, rider, rider_final, rider_attempt)
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
    riders = [{"work_id": rider["work_id"], "revision_id": rider["revision_id"], "evidence": "probe: original"}]
    status, body = _begin(service, workspace_id, item, authorization_ref=auth, identity={"owner_session_started_at": started_at, "diff_sha256": diff_sha, "riders": riders})
    assert status == 200 and body["result"]["status"] == "applied", body
    attempt = body["result"]["attempt"]
    # Identical payload resumes.
    status, body = _begin(service, workspace_id, item, authorization_ref=auth, attempt_id=attempt["attempt_id"], identity={"owner_session_started_at": started_at, "diff_sha256": diff_sha, "riders": riders})
    assert status == 200 and body["result"]["event"]["event_type"] == "attempt_resumed", body
    # Changed rider evidence under the same authorization refuses.
    changed = [{"work_id": rider["work_id"], "revision_id": rider["revision_id"], "evidence": "probe: tampered"}]
    status, body = _begin(service, workspace_id, item, authorization_ref=auth, attempt_id=attempt["attempt_id"], identity={"owner_session_started_at": started_at, "diff_sha256": diff_sha, "riders": changed})
    assert status == 200 and body["result"]["status"] == "refused", body
    assert body["result"]["event"]["reason_code"] == "authorization_identity_mismatch"
