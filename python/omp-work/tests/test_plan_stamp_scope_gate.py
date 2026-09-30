"""OMP-403-s07-s02: plan stamping waits for a linked mission's confirmed scope.

PostgreSQL-gated. Each case uses the command path:

1. The owner approves the mission and links the work: the stamp returns 200.
2. A narrowing revision leaves the mission approved: the stamp returns 200
   and no further approve_mission is applied.
3. Adding a criterion (``D29.material:a``) moves the mission to
   awaiting_confirmation. The stamp returns 409 ``approval_required`` with
   diagnostics ``["mission_scope_unconfirmed"]``, and grant_version, phase,
   and plan_stamp are unchanged with no candidate row. Automation then
   approves on a standing_mandate basis (200) and the stamp returns 200.
4. Adding a repository (``D29.material:c``): automation approve returns
   ``approval_required`` and the stamp is refused as in case 3. After the
   owner approves, the stamp returns 200.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import UTC, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

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
AUTOMATION = uuid4()
AUTOMATION_TOKEN = "automation-token"

_BUDGET = {
    "usd": "50",
    "tokens": 5000,
    "wall_clock_seconds": 1800,
    "max_subagents": 4,
}
_ITEM_BUDGET = {
    "usd": "10",
    "tokens": 1000,
    "wall_clock_seconds": 300,
    "max_subagents": 2,
}
_DESCRIPTION = "The request description"


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


def _capability(path: Path, token: str, actor_id: UUID, actor_kind: str) -> None:
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": actor_kind,
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
    path.chmod(0o600)


@pytest.fixture(scope="module")
def service(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("plan-stamp-scope-gate")
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
        _capability(capabilities / "owner.json", "owner-token", OWNER, "owner")
        _capability(capabilities / "automation.json", AUTOMATION_TOKEN, AUTOMATION, "automation")
        yield SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )


def _grant(service, workspace_id: UUID) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
    for name in ("owner.json", "automation.json"):
        path = service.capabilities / name
        data = json.loads(path.read_text())
        if str(workspace_id) not in data["workspaces"]:
            data["workspaces"].append(str(workspace_id))
            path.write_text(json.dumps(data))
            path.chmod(0o600)


def _owner_headers(workspace_id: UUID) -> dict[str, str]:
    return {
        "Authorization": "Bearer owner-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _command(
    service,
    workspace_id: UUID,
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


def _project(service, provenance: dict | None = None) -> tuple[UUID, UUID]:
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Mission project",
                json.dumps(provenance or {}),
            ),
        )
    return workspace_id, project_id


def _draft(project_id: UUID, **overrides) -> dict:
    draft = {
        "project_id": str(project_id),
        "objective": "Mission objective for plan gate testing",
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    draft.update(overrides)
    return draft


def _submit(service, workspace_id: UUID, project_id: UUID, **overrides) -> tuple[UUID, dict]:
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id, **overrides),
            },
        },
    )
    assert status == 200, body
    return mission_id, body["result"]["mission"]


def _approve(service, workspace_id: UUID, mission_id: UUID, revision: int = 1) -> dict:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": revision,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return body["result"]["mission"]


def _revise(service, workspace_id: UUID, mission_id: UUID, base_revision: int, draft: dict):
    return _command(
        service,
        workspace_id,
        {
            "type": "revise_mission",
            "payload": {
                "mission_id": str(mission_id),
                "base_revision": base_revision,
                "draft": draft,
            },
        },
    )


def _seed_budget(service, workspace_id: UUID, *, work_id: str, revision_id: str, budget: dict) -> None:
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(OWNER),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _create(service, workspace_id: UUID, title: str, **extra) -> dict:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [{"client_ref": "root", "title": title, **extra}],
                "relations": [],
            },
        },
    )
    assert status == 200, body
    return body["result"]["items"][0]


def _item(service, workspace_id: UUID, title: str, budget: dict | None) -> dict:
    item = _create(service, workspace_id, title, description=_DESCRIPTION)
    if budget is not None:
        _seed_budget(
            service,
            workspace_id,
            work_id=item["work_id"],
            revision_id=item["revision_id"],
            budget=budget,
        )
    return item


def _link(service, workspace_id: UUID, mission_id: UUID, work_id: str, *, operation_id=None):
    return _command(
        service,
        workspace_id,
        {
            "type": "link_mission_work",
            "payload": {"mission_id": str(mission_id), "work_id": str(work_id)},
        },
        operation_id=operation_id,
    )


def _setup_execution_grant(
    service, workspace_id: UUID, item: dict
) -> tuple[str, str, str]:
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
                        "work_id": str(item["work_id"]),
                        "revision_id": str(item["revision_id"]),
                        "position": 0,
                        "original_request": _DESCRIPTION,
                        "original_request_sha256": text_sha256(_DESCRIPTION),
                        "initial_git_baseline": head_commit,
                        "active_blocker_ids": [],
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
        workspace_id,
        {
            "type": "seal_execution_criteria",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "work_id": str(item["work_id"]),
                "expected_revision_id": str(item["revision_id"]),
                "criteria": ["AC-1: criteria one"],
                "description_sha256": text_sha256(_DESCRIPTION),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]
    return grant_id, new_rev_id, judge_sha


def _stamp(
    service,
    workspace_id: UUID,
    grant_id: str,
    work_id: str,
    revision_id: str,
    judge_sha: str,
    candidate_id: str,
    expected_grant_version: int = 2,
):
    plan_content = "## Approach\n1. Step one\n\n## Verification\n1. Check one"
    return _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": expected_grant_version,
                "work_id": str(work_id),
                "revision_id": str(revision_id),
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


def _approve_standing(
    service, workspace_id: UUID, mission_id: UUID, revision: int, *, token: str
) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": revision,
                "basis_kind": "standing_mandate",
                "basis_id": "mandate-scope-gate",
            },
        },
        token=token,
    )


def _linked_planning(service, **draft_overrides):
    workspace_id, project_id = _project(service)
    mission_id, _submitted = _submit(service, workspace_id, project_id, **draft_overrides)
    approved = _approve(service, workspace_id, mission_id)
    assert approved["status"] == "approved"
    item = _item(service, workspace_id, "scope gate item", _ITEM_BUDGET)
    status, body = _link(service, workspace_id, mission_id, item["work_id"])
    assert status == 200, body
    grant_id, rev_id, judge_sha = _setup_execution_grant(service, workspace_id, item)
    return workspace_id, project_id, mission_id, item, grant_id, rev_id, judge_sha


def _execution_state(service, grant_id: str, work_id: str) -> tuple[int, str, object]:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT g.grant_version, i.phase, i.plan_stamp"
            " FROM omp_work.execution_grants g"
            " JOIN omp_work.execution_grant_items i ON i.grant_id = g.grant_id"
            " WHERE g.grant_id=%s AND i.work_id=%s",
            (grant_id, work_id),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0]), str(row[1]), row[2]


def _candidate_count(service, candidate_id: str) -> int:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM omp_work.candidates WHERE candidate_id=%s",
            (candidate_id,),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _applied_count(service, workspace_id: UUID, mission_id: UUID, event_type: str) -> int:
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND aggregate_id=%s AND event_type=%s AND outcome='applied'",
            (workspace_id, mission_id, event_type),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


def _assert_stamp_unconfirmed(
    service,
    workspace_id: UUID,
    grant_id: str,
    work_id: str,
    revision_id: str,
    judge_sha: str,
    candidate_id: str,
) -> None:
    before = _execution_state(service, grant_id, work_id)
    status, body = _stamp(
        service, workspace_id, grant_id, work_id, revision_id, judge_sha, candidate_id
    )
    assert status == 409, body
    assert body["error"]["code"] == "approval_required"
    assert body["error"]["diagnostics"] == ["mission_scope_unconfirmed"]
    assert _execution_state(service, grant_id, work_id) == before
    grant_version, phase, plan_stamp = before
    assert grant_version == 2
    assert phase == "planning"
    assert plan_stamp is None
    assert _candidate_count(service, candidate_id) == 0


def test_owner_approved_linked_mission_stamps(service) -> None:
    workspace_id, _project_id, _mission_id, item, grant_id, rev_id, judge_sha = _linked_planning(
        service
    )
    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        str(uuid4()),
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
    assert body["result"]["item"]["plan_stamp_sha256"]


def test_narrowing_revision_stamps_without_another_approval(service) -> None:
    workspace_id, project_id, mission_id, item, grant_id, rev_id, judge_sha = _linked_planning(
        service,
        repositories=["repo-a", "repo-b"],
        acceptance_criteria=["criterion one", "criterion two"],
    )
    lower = {**_BUDGET, "usd": "40"}
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(
            project_id,
            repositories=["repo-a"],
            acceptance_criteria=["criterion one", "criterion two"],
            budget_policy=lower,
        ),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "approved"
    assert mission["revision"] == 2
    assert mission["transitions"][-1]["to_status"] == "approved"
    assert mission["repositories"] == ["repo-a"]

    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        str(uuid4()),
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
    assert _applied_count(service, workspace_id, mission_id, "approve_mission") == 1


def test_added_criterion_blocks_stamp_until_standing_mandate_approval(service) -> None:
    workspace_id, project_id, mission_id, item, grant_id, rev_id, judge_sha = _linked_planning(
        service,
        acceptance_criteria=["criterion one"],
    )
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(
            project_id,
            acceptance_criteria=["criterion one", "criterion two"],
        ),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["transitions"][-1]["cause_id"] == "D29.material:a"

    candidate_id = str(uuid4())
    _assert_stamp_unconfirmed(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        candidate_id,
    )

    status, body = _approve_standing(service, workspace_id, mission_id, 2, token=AUTOMATION_TOKEN)
    assert status == 200, body
    approved = body["result"]["mission"]
    assert approved["status"] == "approved"
    assert approved["approved_scope"]["basis_kind"] == "standing_mandate"

    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        candidate_id,
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"


def test_added_repository_refuses_automation_until_owner_approves(service) -> None:
    workspace_id, project_id, mission_id, item, grant_id, rev_id, judge_sha = _linked_planning(
        service,
        repositories=["repo-a"],
    )
    status, body = _revise(
        service,
        workspace_id,
        mission_id,
        1,
        _draft(project_id, repositories=["repo-a", "repo-b"]),
    )
    assert status == 200, body
    mission = body["result"]["mission"]
    assert mission["status"] == "awaiting_confirmation"
    assert mission["transitions"][-1]["cause_id"] == "D29.material:c"

    status, body = _approve_standing(service, workspace_id, mission_id, 2, token=AUTOMATION_TOKEN)
    assert status == 409, body
    assert body["error"]["code"] == "approval_required"

    candidate_id = str(uuid4())
    _assert_stamp_unconfirmed(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        candidate_id,
    )

    approved = _approve(service, workspace_id, mission_id, revision=2)
    assert approved["status"] == "approved"
    assert approved["approved_scope"]["approved_by_actor_kind"] == "owner"

    status, body = _stamp(
        service,
        workspace_id,
        grant_id,
        item["work_id"],
        rev_id,
        judge_sha,
        candidate_id,
    )
    assert status == 200, body
    assert body["result"]["type"] == "stamp_execution_plan"
