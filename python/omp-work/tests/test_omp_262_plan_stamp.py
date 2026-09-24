from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
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
    root = tmp_path_factory.mktemp("omp-262-plan-stamp")
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


def _grant(service, workspace_id: UUID) -> None:
    seed_authority(service.config.connection_kwargs("postgres"), workspace_id, OWNER)
    owner = service.capabilities / "owner.json"
    data = json.loads(owner.read_text())
    if str(workspace_id) not in data["workspaces"]:
        data["workspaces"].append(str(workspace_id))
        owner.write_text(json.dumps(data))
        owner.chmod(0o600)


def _owner_headers(workspace_id: UUID) -> dict[str, str]:
    return {
        "Authorization": "Bearer owner-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _command(service, workspace_id: UUID, command: dict) -> tuple[int, dict]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    response = service.client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id),
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


def _setup_planning_cycle(service, workspace_id: UUID, title: str = "Plan stamp work"):
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "root",
                        "title": title,
                        "description": "Initial description for planning cycle",
                    }
                ],
                "relations": [],
            },
        },
    )
    assert status == 200, body
    item = body["result"]["items"][0]
    work_id = item["work_id"]
    rev_id = item["revision_id"]

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
                        "work_id": str(work_id),
                        "revision_id": str(rev_id),
                        "position": 0,
                        "original_request": "Initial description for planning cycle",
                        "original_request_sha256": text_sha256("Initial description for planning cycle"),
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
                "description_sha256": text_sha256("Initial description for planning cycle"),
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    new_rev_id = body["result"]["revision"]["revision_id"]
    return work_id, new_rev_id, grant_id, judge_sha


def test_manual_plan_receipt_then_active_execution_stamp_reuses_candidate(service) -> None:
    """Manual /plan-equivalent receipt then active execution stamp reuses candidate,

    uses stored candidate SHA in new receipt/result, and leaves one candidate row.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    work_id, rev_id, grant_id, judge_sha = _setup_planning_cycle(service, workspace_id)

    plan_body = "## Approach\n1. Do work\n\n## Verification\n1. Verify work\n"
    plan_sha = text_sha256(plan_body)
    candidate_id = str(uuid4())
    stored_candidate_sha = "1" * 64

    # 1. Manual /plan-equivalent receipt via append_evidence
    manual_payload = {
        "title": "Manual Plan",
        "body": plan_body,
        "plan_file": "local://plan.md",
        "plan_sha256": plan_sha,
        "approach": ["1. Do work"],
        "verification": ["1. Verify work"],
        "paths": ["src/feature.ts"],
    }
    manual_receipt = {
        "receipt_id": str(uuid4()),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": candidate_id,
        "kind": "plan",
        "payload": manual_payload,
        "payload_sha256": sha256(manual_payload),
        "issuer": "owner",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": stored_candidate_sha,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": manual_receipt}},
    )
    assert status == 200, body

    # Verify candidate row exists after manual plan receipt
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        cand_rows = conn.execute(
            "SELECT * FROM omp_work.candidates WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchall()
        assert len(cand_rows) == 1
        assert str(cand_rows[0]["candidate_id"]) == candidate_id
        assert cand_rows[0]["candidate_sha256"] == stored_candidate_sha
        assert cand_rows[0]["kind"] == "planned"

    # 2. Active execution stamp_execution_plan reusing candidate_id
    execution_candidate_sha = "9" * 64  # Client-computed candidate SHA different from stored
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body,
                "plan_sha256": plan_sha,
                "approach": ["1. Do work"],
                "verification": ["1. Verify work"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": execution_candidate_sha,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body

    # Verify result uses stored candidate SHA, not the incoming execution_candidate_sha
    assert body["result"]["candidate"]["candidate_id"] == candidate_id
    assert body["result"]["candidate"]["candidate_sha256"] == stored_candidate_sha
    assert body["result"]["receipt"]["candidate_sha256"] == stored_candidate_sha
    assert body["result"]["item"]["phase"] == "executing"
    assert body["result"]["grant"]["grant_version"] == 3

    # Verify database state: exactly ONE candidate row remains
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        cand_rows = conn.execute(
            "SELECT * FROM omp_work.candidates WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchall()
        assert len(cand_rows) == 1, f"Expected 1 candidate row, got {len(cand_rows)}"
        assert str(cand_rows[0]["candidate_id"]) == candidate_id
        assert cand_rows[0]["candidate_sha256"] == stored_candidate_sha

        item_row = conn.execute(
            "SELECT current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert str(item_row["current_candidate_id"]) == candidate_id

        receipts = conn.execute(
            "SELECT receipt_id, kind, candidate_sha256 FROM omp_evidence.receipts WHERE workspace_id=%s AND candidate_id=%s ORDER BY issued_at",
            (workspace_id, candidate_id),
        ).fetchall()
        assert len(receipts) == 2
        for r in receipts:
            assert r["kind"] == "plan"
            assert r["candidate_sha256"] == stored_candidate_sha


def test_execution_plan_receipt_replan_reuses_candidate(service) -> None:
    """Execution receipt's nested stamp hash allows candidate reuse on replan."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    work_id, rev_id, grant_id, judge_sha = _setup_planning_cycle(service, workspace_id)

    plan_body = "## Approach\n1. Do work\n\n## Verification\n1. Verify work\n"
    plan_sha = text_sha256(plan_body)
    candidate_id = str(uuid4())

    # 1. First execution stamp (inserts candidate and execution receipt)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body,
                "plan_sha256": plan_sha,
                "approach": ["1. Do work"],
                "verification": ["1. Verify work"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body

    # 2. Second execution stamp with same candidate_id and same plan SHA (replan before close attempts)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 3,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": candidate_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body,
                "plan_sha256": plan_sha,
                "approach": ["1. Do work"],
                "verification": ["1. Verify work"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "2" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    assert body["result"]["candidate"]["candidate_sha256"] == "1" * 64
    assert body["result"]["grant"]["grant_version"] == 4

    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        cand_rows = conn.execute(
            "SELECT * FROM omp_work.candidates WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchall()
        assert len(cand_rows) == 1


def test_incompatible_final_candidate_rejected_without_writes(service) -> None:
    """Incompatible final candidate is rejected without writes."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    work_id, rev_id, grant_id, judge_sha = _setup_planning_cycle(service, workspace_id)

    plan_body = "## Approach\n1. Do work\n\n## Verification\n1. Verify work\n"
    plan_sha = text_sha256(plan_body)
    final_cand_id = str(uuid4())

    # Insert a candidate with kind='final'
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, kind, allocated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'final', %s)",
            (
                final_cand_id,
                workspace_id,
                work_id,
                rev_id,
                "f" * 64,
                "c" * 40,
                datetime.now(timezone.utc),
            ),
        )

    # Record state before call
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        grant_before = conn.execute(
            "SELECT grant_version FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        ).fetchone()
        grant_item_before = conn.execute(
            "SELECT phase, plan_stamp FROM omp_work.execution_grant_items WHERE workspace_id=%s AND grant_id=%s AND work_id=%s",
            (workspace_id, grant_id, work_id),
        ).fetchone()
        work_item_before = conn.execute(
            "SELECT current_candidate_id, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        receipts_count_before = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]

    # Attempt to stamp execution plan using the final candidate
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": grant_before["grant_version"],
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": final_cand_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body,
                "plan_sha256": plan_sha,
                "approach": ["1. Do work"],
                "verification": ["1. Verify work"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status in (400, 409), f"Expected rejection, got {status}: {body}"

    # Verify NO writes occurred
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        grant_after = conn.execute(
            "SELECT grant_version FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        ).fetchone()
        assert grant_after["grant_version"] == grant_before["grant_version"]

        grant_item_after = conn.execute(
            "SELECT phase, plan_stamp FROM omp_work.execution_grant_items WHERE workspace_id=%s AND grant_id=%s AND work_id=%s",
            (workspace_id, grant_id, work_id),
        ).fetchone()
        assert grant_item_after["phase"] == grant_item_before["phase"]
        assert grant_item_after["plan_stamp"] == grant_item_before["plan_stamp"]

        work_item_after = conn.execute(
            "SELECT current_candidate_id, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert work_item_after["current_candidate_id"] == work_item_before["current_candidate_id"]
        assert work_item_after["row_version"] == work_item_before["row_version"]

        receipts_count_after = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        assert receipts_count_after == receipts_count_before


def test_same_planned_id_with_different_plan_hash_or_body_rejected_without_writes(service) -> None:
    """Same planned ID with different plan hash/body is rejected without writes."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    work_id, rev_id, grant_id, judge_sha = _setup_planning_cycle(service, workspace_id)

    plan_body_1 = "## Approach\n1. Plan 1\n\n## Verification\n1. Verify 1\n"
    plan_sha_1 = text_sha256(plan_body_1)
    cand_id = str(uuid4())

    # 1. Create candidate with plan 1
    manual_payload = {
        "title": "Plan 1",
        "body": plan_body_1,
        "plan_file": "local://plan.md",
        "plan_sha256": plan_sha_1,
        "approach": ["1. Plan 1"],
        "verification": ["1. Verify 1"],
    }
    manual_receipt = {
        "receipt_id": str(uuid4()),
        "work_id": str(work_id),
        "revision_id": str(rev_id),
        "candidate_id": cand_id,
        "kind": "plan",
        "payload": manual_payload,
        "payload_sha256": sha256(manual_payload),
        "issuer": "owner",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": "1" * 64,
    }
    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": manual_receipt}},
    )
    assert status == 200, body

    # Record state before
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        grant_version_before = conn.execute(
            "SELECT grant_version FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        ).fetchone()["grant_version"]
        receipts_count_before = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]

    # Case A: Same candidate ID but different plan body/hash
    plan_body_2 = "## Approach\n1. Plan 2 Different\n\n## Verification\n1. Verify 2\n"
    plan_sha_2 = text_sha256(plan_body_2)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": grant_version_before,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": cand_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body_2,
                "plan_sha256": plan_sha_2,
                "approach": ["1. Plan 2 Different"],
                "verification": ["1. Verify 2"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "2" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status in (400, 409), f"Expected rejection for different plan SHA, got {status}: {body}"

    # Case B: plan_sha256 does not match exact plan_body bytes
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": grant_version_before,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": str(uuid4()),
                "plan_file": "local://plan.md",
                "plan_body": plan_body_1,
                "plan_sha256": "0" * 64,  # Mismatched SHA
                "approach": ["1. Plan 1"],
                "verification": ["1. Verify 1"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "3" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status in (400, 409), f"Expected rejection for invalid plan_sha256, got {status}: {body}"

    # Verify no writes occurred
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        grant_version_after = conn.execute(
            "SELECT grant_version FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        ).fetchone()["grant_version"]
        assert grant_version_after == grant_version_before

        grant_item_phase = conn.execute(
            "SELECT phase FROM omp_work.execution_grant_items WHERE workspace_id=%s AND grant_id=%s AND work_id=%s",
            (workspace_id, grant_id, work_id),
        ).fetchone()["phase"]
        assert grant_item_phase == "planning"

        receipts_count_after = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        assert receipts_count_after == receipts_count_before


def test_inactive_grant_returns_execution_grant_inactive_without_writes(service) -> None:
    """Stopped/inactive grant returns execution_grant_inactive with candidate/receipt/grant version unchanged."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    work_id, rev_id, grant_id, judge_sha = _setup_planning_cycle(service, workspace_id)

    # Transition grant to 'stopped' via set_execution_state
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "target_state": "stopped",
                "reason": "stopping execution for inactive test",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200, body
    stopped_grant_version = body["result"]["grant"]["grant_version"]

    # Record state before
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        cand_count_before = conn.execute(
            "SELECT count(*) as cnt FROM omp_work.candidates WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        receipts_count_before = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        work_item_before = conn.execute(
            "SELECT current_candidate_id, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()

    plan_body = "## Approach\n1. Do work\n\n## Verification\n1. Verify work\n"
    plan_sha = text_sha256(plan_body)
    new_cand_id = str(uuid4())

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "stamp_execution_plan",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": stopped_grant_version,
                "work_id": str(work_id),
                "revision_id": str(rev_id),
                "candidate_id": new_cand_id,
                "plan_file": "local://plan.md",
                "plan_body": plan_body,
                "plan_sha256": plan_sha,
                "approach": ["1. Do work"],
                "verification": ["1. Verify work"],
                "paths": ["src/feature.ts"],
                "candidate_sha256": "1" * 64,
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 409, f"Expected 409 for execution_grant_inactive, got {status}: {body}"
    assert body.get("error") == "execution_grant_inactive" or "execution_grant_inactive" in str(body)

    # Verify workflow candidate/receipt/grant version remain unchanged
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    ) as conn:
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(OWNER)),
        )
        grant_after = conn.execute(
            "SELECT state, grant_version FROM omp_work.execution_grants WHERE workspace_id=%s AND grant_id=%s",
            (workspace_id, grant_id),
        ).fetchone()
        assert grant_after["state"] == "stopped"
        assert grant_after["grant_version"] == stopped_grant_version

        cand_count_after = conn.execute(
            "SELECT count(*) as cnt FROM omp_work.candidates WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        assert cand_count_after == cand_count_before

        receipts_count_after = conn.execute(
            "SELECT count(*) as cnt FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()["cnt"]
        assert receipts_count_after == receipts_count_before

        work_item_after = conn.execute(
            "SELECT current_candidate_id, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert work_item_after["current_candidate_id"] == work_item_before["current_candidate_id"]
        assert work_item_after["row_version"] == work_item_before["row_version"]
