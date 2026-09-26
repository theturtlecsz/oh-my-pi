"""OMP-283: PostgreSQL refusal and authorization test suite for record_external_delivery.

Comprehensive refusal tests for unknown work items, terminal states (already-DONE,
CANCELED/CANCELLED, archived), revision conflicts, boundary and invalid evidence payloads,
and scope authorization boundaries (work.read, work.mutate, work.approve, work.execute vs work.close).
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
    root = tmp_path_factory.mktemp("external-delivery-suite")
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


def _register_token(
    service,
    token: str,
    workspace_ids: list[UUID],
    scopes: list[str],
    *,
    actor_kind: str = "agent",
) -> None:
    cap_file = service.capabilities / f"{token}.json"
    cap_file.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(uuid4()),
                "actor_kind": actor_kind,
                "workspaces": [str(ws) for ws in workspace_ids],
                "scopes": scopes,
            }
        )
    )
    cap_file.chmod(0o600)


def _headers(workspace_id: UUID, token: str = "owner-token") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
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
        headers=_headers(workspace_id, token),
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
        "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
        (str(workspace_id), str(OWNER)),
    )
    return conn


def test_record_external_delivery_refuses_unknown_work_item(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    unknown_work_id = uuid4()
    dummy_revision_id = uuid4()
    evidence = "delivered externally"

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(unknown_work_id),
                "revision_id": str(dummy_revision_id),
                "evidence": evidence,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request", body
    assert any("unknown work item" in diag for diag in body["error"]["diagnostics"])

    with _app_connection(service, workspace_id) as conn:
        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, unknown_work_id),
        ).fetchone()
        assert receipts["count"] == 0

        events = conn.execute(
            "SELECT count(*) AS count FROM omp_audit.domain_events WHERE workspace_id=%s AND aggregate_id=%s",
            (workspace_id, unknown_work_id),
        ).fetchone()
        assert events["count"] == 0


def test_record_external_delivery_refuses_already_done_item(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item to deliver once")
    work_id = item["work_id"]
    revision_id = item["revision_id"]
    evidence_1 = "First delivery: PR #101"

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": evidence_1,
            },
        },
    )
    assert status == 200, body
    first_receipt_id = body["result"]["receipt_id"]

    with _app_connection(service, workspace_id) as conn:
        done_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert done_row["state"] == "DONE"
        expected_version = done_row["row_version"]

    evidence_2 = "Second delivery attempt: PR #102"
    status_2, body_2 = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": evidence_2,
            },
        },
    )
    assert status_2 == 400, body_2
    assert body_2["error"]["code"] == "invalid_request", body_2
    assert any("work item state is DONE" in diag for diag in body_2["error"]["diagnostics"])

    with _app_connection(service, workspace_id) as conn:
        final_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert final_row["state"] == "DONE"
        assert final_row["row_version"] == expected_version

        receipts = conn.execute(
            "SELECT receipt_id, kind FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchall()
        assert len(receipts) == 1
        assert str(receipts[0]["receipt_id"]) == str(first_receipt_id)


@pytest.mark.parametrize("terminal_state", ["CANCELED", "CANCELLED"])
def test_record_external_delivery_refuses_canceled_items(
    service, terminal_state: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, f"Item to cancel {terminal_state}")
    work_id = item["work_id"]
    revision_id = item["revision_id"]

    with _app_connection(service, workspace_id) as conn:
        conn.execute(
            "UPDATE omp_work.work_items SET state=%s, row_version=row_version+1 WHERE workspace_id=%s AND work_id=%s",
            (terminal_state, workspace_id, work_id),
        )
        conn.commit()

        canceled_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert canceled_row["state"] == terminal_state
        initial_version = canceled_row["row_version"]

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": f"attempt delivery on {terminal_state} item",
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request", body
    assert any(
        f"work item state is {terminal_state}" in diag
        for diag in body["error"]["diagnostics"]
    )

    with _app_connection(service, workspace_id) as conn:
        final_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert final_row["state"] == terminal_state
        assert final_row["row_version"] == initial_version

        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 0


def test_record_external_delivery_refuses_archived_item(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item to archive")
    work_id = item["work_id"]
    revision_id = item["revision_id"]

    with _app_connection(service, workspace_id) as conn:
        conn.execute(
            "UPDATE omp_work.work_items SET archived=true, row_version=row_version+1 WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        )
        conn.commit()

        archived_row = conn.execute(
            "SELECT state, archived, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert archived_row["archived"] is True
        initial_version = archived_row["row_version"]
        initial_state = archived_row["state"]

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": "attempt delivery on archived item",
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request", body
    assert any("work item is archived" in diag for diag in body["error"]["diagnostics"])

    with _app_connection(service, workspace_id) as conn:
        final_row = conn.execute(
            "SELECT state, archived, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert final_row["archived"] is True
        assert final_row["state"] == initial_state
        assert final_row["row_version"] == initial_version

        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 0


def test_record_external_delivery_refuses_revision_conflict(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item with revision conflict")
    work_id = item["work_id"]
    correct_revision = item["revision_id"]
    conflicting_revision = uuid4()

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(conflicting_revision),
                "evidence": "stale delivery evidence",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence", body
    assert any("revision mismatch" in diag for diag in body["error"]["diagnostics"])

    with _app_connection(service, workspace_id) as conn:
        item_row = conn.execute(
            "SELECT state, current_revision_id, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert str(item_row["current_revision_id"]) == str(correct_revision)
        assert item_row["state"] == item["state"]
        assert item_row["row_version"] == item["row_version"]

        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 0


@pytest.mark.parametrize(
    "invalid_evidence",
    [
        "",
        "   ",
        "\t  \t",
        "delivered\nwith newline",
        "delivered\rwith cr",
        "delivered\x00with nul",
        "a" * 4097,
        "\u00e9" * 3000,
    ],
)
def test_record_external_delivery_refuses_invalid_evidence(
    service, invalid_evidence: str
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item invalid evidence guard")
    work_id = item["work_id"]
    revision_id = item["revision_id"]

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": invalid_evidence,
            },
        },
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request", body

    with _app_connection(service, workspace_id) as conn:
        item_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert item_row["state"] == item["state"]
        assert item_row["row_version"] == item["row_version"]

        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 0


def test_record_external_delivery_accepts_boundary_evidence(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    # 1. Exact 4096 ASCII bytes
    item_ascii = _create(service, workspace_id, "Item boundary 4096 ascii")
    evidence_ascii = "x" * 4096
    assert len(evidence_ascii.encode("utf-8")) == 4096

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(item_ascii["work_id"]),
                "revision_id": str(item_ascii["revision_id"]),
                "evidence": evidence_ascii,
            },
        },
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["payload_sha256"] == sha256({"evidence": evidence_ascii})

    # 2. Multibyte UTF-8 string of exactly 4096 bytes (2048 2-byte characters)
    item_multi = _create(service, workspace_id, "Item boundary 4096 multibyte")
    evidence_multi = "\u00e9" * 2048
    assert len(evidence_multi.encode("utf-8")) == 4096

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(item_multi["work_id"]),
                "revision_id": str(item_multi["revision_id"]),
                "evidence": evidence_multi,
            },
        },
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["payload_sha256"] == sha256({"evidence": evidence_multi})

    # 3. Leading and trailing spaces preserved verbatim
    item_spaces = _create(service, workspace_id, "Item verbatim whitespace")
    evidence_spaces = "   verbatim evidence with leading and trailing spaces   "

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(item_spaces["work_id"]),
                "revision_id": str(item_spaces["revision_id"]),
                "evidence": evidence_spaces,
            },
        },
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["payload_sha256"] == sha256({"evidence": evidence_spaces})

    with _app_connection(service, workspace_id) as conn:
        receipt = conn.execute(
            "SELECT payload, payload_sha256 FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, item_spaces["work_id"]),
        ).fetchone()
        assert receipt["payload"] == {"evidence": evidence_spaces}
        assert receipt["payload_sha256"] == sha256({"evidence": evidence_spaces})


@pytest.mark.parametrize(
    ("token_name", "scopes"),
    [
        ("reader-only", ["work.read"]),
        ("mutator-only", ["work.mutate"]),
        ("approver-only", ["work.approve"]),
        ("executor-only", ["work.execute"]),
        (
            "multi-non-close",
            ["work.read", "work.mutate", "work.approve", "work.execute"],
        ),
    ],
)
def test_record_external_delivery_refuses_insufficient_scopes(
    service, token_name: str, scopes: list[str]
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    _register_token(service, token_name, [workspace_id], scopes)

    item = _create(service, workspace_id, f"Item auth test {token_name}")
    work_id = item["work_id"]
    revision_id = item["revision_id"]

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "evidence": "attempt unauthorized delivery",
            },
        },
        token=token_name,
    )
    assert status == 403, body
    assert body["error"]["code"] == "forbidden", body

    with _app_connection(service, workspace_id) as conn:
        item_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert item_row["state"] == item["state"]
        assert item_row["row_version"] == item["row_version"]

        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 0


def test_record_external_delivery_allows_work_close_scope(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    closer_token = "closer-token"
    _register_token(service, closer_token, [workspace_id], ["work.close"])

    item = _create(service, workspace_id, "Item closer authorization")
    work_id = item["work_id"]
    revision_id = item["revision_id"]
    evidence = "delivered by agent with work.close"

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
        token=closer_token,
    )
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    assert body["result"]["work_id"] == str(work_id)

    with _app_connection(service, workspace_id) as conn:
        item_row = conn.execute(
            "SELECT state, row_version FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        ).fetchone()
        assert item_row["state"] == "DONE"
        assert item_row["row_version"] == item["row_version"] + 1


def test_record_external_delivery_refuses_foreign_workspace(service) -> None:
    workspace_a = uuid4()
    workspace_b = uuid4()
    _grant(service, workspace_a)
    _grant(service, workspace_b)

    token_a = "token-ws-a"
    _register_token(service, token_a, [workspace_a], ["work.close"])

    item_b = _create(service, workspace_b, "Item in workspace B")

    status, body = _command(
        service,
        workspace_b,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(item_b["work_id"]),
                "revision_id": str(item_b["revision_id"]),
                "evidence": "attempt cross workspace delivery",
            },
        },
        token=token_a,
    )
    assert status == 403, body
    assert body["error"]["code"] == "forbidden", body


def test_record_external_delivery_refuses_unauthenticated(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item unauthenticated test")

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "record_external_delivery",
            "payload": {
                "work_id": str(item["work_id"]),
                "revision_id": str(item["revision_id"]),
                "evidence": "no valid credentials",
            },
        },
        token="bogus-nonexistent-token",
    )
    assert status == 401, body
    assert body["error"]["code"] == "unauthenticated", body


def test_record_external_delivery_idempotency_replay_and_conflict(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)

    item = _create(service, workspace_id, "Item idempotency test")
    work_id = item["work_id"]
    revision_id = item["revision_id"]
    evidence = "PR #555 delivery"
    op_id = uuid4()

    cmd = {
        "type": "record_external_delivery",
        "payload": {
            "work_id": str(work_id),
            "revision_id": str(revision_id),
            "evidence": evidence,
        },
    }

    # First call: applied
    status, body = _command(service, workspace_id, cmd, operation_id=op_id)
    assert status == 200, body
    assert body["receipt"]["state"] == "applied"
    receipt_id = body["result"]["receipt_id"]

    # Second call with same op_id and matching payload: replayed
    status_2, body_2 = _command(service, workspace_id, cmd, operation_id=op_id)
    assert status_2 == 200, body_2
    assert body_2["receipt"]["state"] == "replayed"
    assert body_2["result"]["receipt_id"] == receipt_id

    # Third call with same op_id but differing payload: conflict
    conflicting_cmd = {
        "type": "record_external_delivery",
        "payload": {
            "work_id": str(work_id),
            "revision_id": str(revision_id),
            "evidence": "Different evidence for conflict",
        },
    }
    status_3, body_3 = _command(
        service, workspace_id, conflicting_cmd, operation_id=op_id
    )
    assert status_3 == 409, body_3
    assert body_3["error"]["code"] == "idempotency_conflict", body_3

    with _app_connection(service, workspace_id) as conn:
        receipts = conn.execute(
            "SELECT count(*) AS count FROM omp_evidence.receipts WHERE workspace_id=%s AND work_id=%s AND kind='external_delivery'",
            (workspace_id, work_id),
        ).fetchone()
        assert receipts["count"] == 1
