"""OMP-283: PostgreSQL smoke test for the record_external_delivery store command.

Covers the atomic DONE transition (receipt + history) and the generic
append_evidence refusal for external_delivery receipts.
"""

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
import pytest
from fastapi.testclient import TestClient
from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority
from psycopg.rows import dict_row

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
    root = tmp_path_factory.mktemp("external-delivery-smoke")
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


def _command(
    service,
    workspace_id: UUID,
    command: dict,
    *,
    token: str = "owner-token",
    operation_id: UUID | None = None,
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


def _create(service, workspace_id: UUID, title: str = "item", **extra: object) -> dict:
    batch = {
        "type": "create_work_batch",
        "payload": {
            "items": [{"client_ref": "root", "title": title, **extra}],
            "relations": [],
        },
    }
    status, body = _command(service, workspace_id, batch)
    assert status == 200, body
    return body["result"]["items"][0]


def _app_connection(service, workspace_id: UUID):
    conn = psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), row_factory=dict_row
    )
    conn.execute(
        "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
        (str(workspace_id), str(OWNER)),
    )
    return conn


def test_record_external_delivery_success(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Delivered task")
    work_id = item["work_id"]
    revision_id = item["revision_id"]
    evidence = "PR #78 merged: commit 1bb66db962a3087f00fff1bd4cee299cfe46a444"
    expected_sha = sha256({"evidence": evidence})

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": evidence,
            },
        },
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied", body
    result = body["result"]
    assert result["type"] == "record_external_delivery"
    assert result["work_id"] == str(work_id)
    assert result["revision_id"] == str(revision_id)
    assert result["payload_sha256"] == expected_sha
    receipt_id = UUID(result["receipt_id"])

    with _app_connection(service, workspace_id) as conn:
        # 1. The item is DONE, its revision binding is unchanged, row_version bumped.
        item_row = conn.execute(
            "SELECT current_revision_id, state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert item_row is not None
        assert str(item_row["current_revision_id"]) == str(revision_id)
        assert item_row["state"] == "DONE"
        assert item_row["row_version"] == item["row_version"] + 1

        # 2. Exactly one external_delivery receipt, candidate_id NULL, canonical payload.
        receipts = conn.execute(
            "SELECT receipt_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256 FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchall()
        assert len(receipts) == 1
        receipt = receipts[0]
        assert str(receipt["receipt_id"]) == str(receipt_id)
        assert str(receipt["work_id"]) == str(work_id)
        assert str(receipt["revision_id"]) == str(revision_id)
        assert receipt["candidate_id"] is None
        assert receipt["kind"] == "external_delivery"
        assert receipt["payload"] == {"evidence": evidence}
        assert receipt["payload_sha256"] == expected_sha

        # 3. The latest domain event for the aggregate binds the receipt id and hash.
        event = conn.execute(
            "SELECT event_type, outcome, payload FROM omp_audit.domain_events WHERE workspace_id=%s AND aggregate_id=%s ORDER BY sequence DESC LIMIT 1",
            (workspace_id, work_id),
        ).fetchone()
        assert event is not None
        assert event["event_type"] == "record_external_delivery"
        assert event["outcome"] == "applied"
        event_payload = (
            event["payload"]
            if isinstance(event["payload"], dict)
            else json.loads(event["payload"])
        )
        assert event_payload["receipt_id"] == str(receipt_id)
        assert event_payload["payload_sha256"] == receipt["payload_sha256"]


def test_append_evidence_refuses_external_delivery(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item append guard")
    work_id = item["work_id"]
    revision_id = item["revision_id"]
    evidence = "delivered externally"
    receipt = {
        "receipt_id": str(uuid4()),
        "work_id": str(work_id),
        "revision_id": str(revision_id),
        "candidate_id": None,
        "kind": "external_delivery",
        "payload": {"evidence": evidence},
        "payload_sha256": sha256({"evidence": evidence}),
        "issuer": "owner",
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }

    status, body = _command(
        service,
        workspace_id,
        {"type": "append_evidence", "payload": {"receipt": receipt}},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any(
        "record_external_delivery" in diagnostic
        for diagnostic in body["error"]["diagnostics"]
    )

    with _app_connection(service, workspace_id) as conn:
        external = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert external["count"] == 0

        item_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert item_row is not None
        assert item_row["state"] == item["state"]
        assert item_row["row_version"] == item["row_version"]
