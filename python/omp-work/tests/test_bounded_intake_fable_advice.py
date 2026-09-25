from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    AppendEvidenceCommand,
    AppendEvidencePayload,
    Command,
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    EvidenceKind,
    EvidenceReceipt,
    RecordFableAdviceCommand,
)
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority


def _config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
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
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=port,
    )


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_append_evidence_rejects_reserved_issuers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        workspace_id, actor_id = uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            conn.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (workspace_id,),
            )
        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate", "work.approve", "work.close"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)

        client = TestClient(create_app(config, capabilities_dir=capabilities))
        headers = {
            "Authorization": "Bearer owner-token",
            "X-OMP-Workspace-ID": str(workspace_id),
            "X-OMP-Contract-SHA256": contract_sha256(),
        }

        # 1. Create a work item
        create_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=CreateWorkBatchCommand(
                type="create_work_batch",
                payload=CreateWorkBatchPayload(
                    items=(
                        CreateWorkInput(
                            client_ref="ref-1",
                            title="Test item for reserved issuer verification",
                        ),
                    )
                ),
            ),
        )
        create_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=create_envelope.model_dump(mode="json"),
        )
        assert create_resp.status_code == 200
        item_data = create_resp.json()["result"]["items"][0]
        work_id = item_data["work_id"]
        revision_id = item_data["revision_id"]

        # 2. Seed an item with a current candidate via plan evidence
        candidate_id = uuid4()
        candidate_sha = "3" * 64
        candidate_commit = "4" * 40
        plan_payload = {"body": "initial plan"}
        plan_receipt = EvidenceReceipt(
            receipt_id=uuid4(),
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            kind=EvidenceKind.PLAN,
            payload=plan_payload,
            payload_sha256=sha256(plan_payload),
            issuer="owner",
            issued_at="2026-09-25T10:00:00Z",
            candidate_sha256=candidate_sha,
            candidate_commit=candidate_commit,
        )
        plan_env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AppendEvidenceCommand(
                type="append_evidence",
                payload=AppendEvidencePayload(receipt=plan_receipt),
            ),
        )
        plan_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=plan_env.model_dump(mode="json"),
        )
        assert plan_resp.status_code == 200

        # 3. POST append_evidence (kind verification, valid payload_sha256, matching candidate) with issuer "service"
        forged_service_payload = {"verification": "result"}
        forged_service_receipt_id = uuid4()
        forged_service_receipt = EvidenceReceipt(
            receipt_id=forged_service_receipt_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            kind=EvidenceKind.VERIFICATION,
            payload=forged_service_payload,
            payload_sha256=sha256(forged_service_payload),
            issuer="service",
            issued_at="2026-09-25T10:01:00Z",
            candidate_sha256=candidate_sha,
            candidate_commit=candidate_commit,
        )
        forged_service_env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AppendEvidenceCommand(
                type="append_evidence",
                payload=AppendEvidencePayload(receipt=forged_service_receipt),
            ),
        )
        forged_service_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=forged_service_env.model_dump(mode="json"),
        )
        assert forged_service_resp.status_code == 400
        assert forged_service_resp.json()["error"]["code"] == "invalid_request"
        assert "reserved issuer" in forged_service_resp.json()["error"]["diagnostics"]

        # 4. POST append_evidence with issuer "work-service/x"
        forged_ws_payload = {"verification": "result"}
        forged_ws_receipt_id = uuid4()
        forged_ws_receipt = EvidenceReceipt(
            receipt_id=forged_ws_receipt_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            kind=EvidenceKind.VERIFICATION,
            payload=forged_ws_payload,
            payload_sha256=sha256(forged_ws_payload),
            issuer="work-service/x",
            issued_at="2026-09-25T10:02:00Z",
            candidate_sha256=candidate_sha,
            candidate_commit=candidate_commit,
        )
        forged_ws_env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AppendEvidenceCommand(
                type="append_evidence",
                payload=AppendEvidencePayload(receipt=forged_ws_receipt),
            ),
        )
        forged_ws_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=forged_ws_env.model_dump(mode="json"),
        )
        assert forged_ws_resp.status_code == 400
        assert forged_ws_resp.json()["error"]["code"] == "invalid_request"
        assert "reserved issuer" in forged_ws_resp.json()["error"]["diagnostics"]

        # Verify no receipt row inserted for either forged call
        conn_kwargs = config.connection_kwargs("postgres")
        with psycopg.connect(**conn_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) as count FROM omp_evidence.receipts WHERE workspace_id=%s AND issuer IN ('service', 'work-service/x')",
                    (workspace_id,),
                )
                assert cur.fetchone()["count"] == 0

        # 5. Control call with issuer "agent/x" still succeeds
        control_payload = {"verification": "control"}
        control_receipt_id = uuid4()
        control_receipt = EvidenceReceipt(
            receipt_id=control_receipt_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            kind=EvidenceKind.VERIFICATION,
            payload=control_payload,
            payload_sha256=sha256(control_payload),
            issuer="agent/x",
            issued_at="2026-09-25T10:03:00Z",
            candidate_sha256=candidate_sha,
            candidate_commit=candidate_commit,
        )
        control_env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AppendEvidenceCommand(
                type="append_evidence",
                payload=AppendEvidencePayload(receipt=control_receipt),
            ),
        )
        control_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=control_env.model_dump(mode="json"),
        )
        assert control_resp.status_code == 200
        control_data = control_resp.json()
        assert control_data["receipt"]["state"] == "applied"
        assert control_data["result"]["type"] == "append_evidence"
        assert control_data["result"]["receipt"]["receipt_id"] == str(control_receipt_id)
        assert control_data["result"]["receipt"]["issuer"] == "agent/x"

        # Verify control receipt row was inserted
        with psycopg.connect(**conn_kwargs, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) as count FROM omp_evidence.receipts WHERE workspace_id=%s AND receipt_id=%s",
                    (workspace_id, control_receipt_id),
                )
                assert cur.fetchone()["count"] == 1

        # 6. Replay forged check: pre-existing DB row with reserved issuer cannot be replayed via append_evidence
        preexisting_receipt_id = uuid4()
        preexisting_payload = {"service_trusted": True}
        preexisting_payload_sha = sha256(preexisting_payload)
        with psycopg.connect(**conn_kwargs) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,artifact_sha256,issuer,issued_at,candidate_sha256,candidate_commit,verdict,independent,remote_ref,remote_commit) VALUES(%s,%s,%s,%s,%s,'verification',%s,%s,NULL,'service',now(),%s,%s,NULL,false,NULL,NULL)",
                    (
                        preexisting_receipt_id,
                        workspace_id,
                        work_id,
                        revision_id,
                        candidate_id,
                        json.dumps(preexisting_payload),
                        preexisting_payload_sha,
                        candidate_sha,
                        candidate_commit,
                    ),
                )
        replay_forged_receipt = EvidenceReceipt(
            receipt_id=preexisting_receipt_id,
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            kind=EvidenceKind.VERIFICATION,
            payload=preexisting_payload,
            payload_sha256=preexisting_payload_sha,
            issuer="service",
            issued_at="2026-09-25T10:04:00Z",
            candidate_sha256=candidate_sha,
            candidate_commit=candidate_commit,
        )
        replay_forged_env = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=AppendEvidenceCommand(
                type="append_evidence",
                payload=AppendEvidencePayload(receipt=replay_forged_receipt),
            ),
        )
        replay_forged_resp = client.post(
            "/v1/commands",
            headers=headers,
            json=replay_forged_env.model_dump(mode="json"),
        )
        assert replay_forged_resp.status_code == 400
        assert replay_forged_resp.json()["error"]["code"] == "invalid_request"
        assert "reserved issuer" in replay_forged_resp.json()["error"]["diagnostics"]


def _record_fable_advice_dict() -> dict[str, object]:
    return {
        "type": "record_fable_advice",
        "payload": {
            "work_id": str(uuid4()),
            "revision_id": str(uuid4()),
            "advice_sha256": "a" * 64,
            "disposition": "considered",
            "intake_semantic_sha256": "b" * 64,
            "rule_bundle_sha256": "c" * 64,
        },
    }


def test_record_fable_advice_parses_via_command_discriminator() -> None:
    command = TypeAdapter(Command).validate_python(_record_fable_advice_dict())
    assert isinstance(command, RecordFableAdviceCommand)
    assert command.type == "record_fable_advice"
    assert command.payload.disposition == "considered"


def test_record_fable_advice_rejects_ignored_disposition() -> None:
    data = _record_fable_advice_dict()
    data["payload"]["disposition"] = "ignored"  # type: ignore[index]
    with pytest.raises(ValidationError):
        TypeAdapter(Command).validate_python(data)


def test_record_fable_advice_rejects_non_hex_advice_sha256() -> None:
    data = _record_fable_advice_dict()
    data["payload"]["advice_sha256"] = "z" * 64  # type: ignore[index]
    with pytest.raises(ValidationError):
        TypeAdapter(Command).validate_python(data)
