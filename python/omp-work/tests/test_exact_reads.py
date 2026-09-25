from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import secrets
import socket
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    ReviseWorkCommand,
    ReviseWorkPayload,
    WorkRevision,
)
from omp_work.v1.server import create_app

from omp_work.v1.service import WorkError
from omp_work.v1.store import _RECEIPT_FIELDS
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


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


def _make_envelope(workspace_id: UUID, command) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )


def test_exact_reads_suite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
        bootstrap(config)

        ws1, actor1 = uuid4(), uuid4()
        ws2, actor2 = uuid4(), uuid4()
        cand_actor = uuid4()
        cand_id = uuid4()

        seed_authority(config.connection_kwargs("postgres"), ws1, actor1)
        seed_authority(config.connection_kwargs("postgres"), ws2, actor2)

        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700)

        owner1_bearer = capabilities / "owner1.json"
        owner1_bearer.write_text(
            json.dumps(
                {
                    "token": "owner1-token",
                    "actor_id": str(actor1),
                    "actor_kind": "owner",
                    "workspaces": [str(ws1)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        owner1_bearer.chmod(0o600)

        owner2_bearer = capabilities / "owner2.json"
        owner2_bearer.write_text(
            json.dumps(
                {
                    "token": "owner2-token",
                    "actor_id": str(actor2),
                    "actor_kind": "owner",
                    "workspaces": [str(ws2)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        owner2_bearer.chmod(0o600)

        cand_bearer = capabilities / "candidate.json"
        cand_bearer.write_text(
            json.dumps(
                {
                    "token": "candidate-token",
                    "actor_id": str(cand_actor),
                    "actor_kind": "candidate",
                    "workspaces": [str(ws1)],
                    "scopes": ["work.candidate.read"],
                    "candidate_ids": [str(cand_id)],
                }
            )
        )
        cand_bearer.chmod(0o600)

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        transport = tc._transport
        client1 = WorkClient(
            "http://testserver",
            ws1,
            owner1_bearer,
            transport=transport,
        )
        client2 = WorkClient(
            "http://testserver",
            ws2,
            owner2_bearer,
            transport=transport,
        )


        # -------------------------------------------------------------
        # Part 1: Revisions (selector forms, revision 1 readable after 2, foreign key error)
        # -------------------------------------------------------------
        create_res = client1.execute(
            _make_envelope(
                ws1,
                CreateWorkBatchCommand(
                    type="create_work_batch",
                    payload=CreateWorkBatchPayload(
                        items=(
                            CreateWorkInput(
                                client_ref="ref-item-1",
                                title="Initial Work Item Title",
                                acceptance_criteria=("Criterion A", "Criterion B"),
                            ),
                        )
                    ),
                ),
            )
        )

        created_item = create_res.result.items[0]
        key = created_item.key
        rev1_id = created_item.revision_id
        work_id = created_item.work_id

        # Decimal string & int selector
        rev_by_num_str = client1.revision(key, "1")
        rev_by_num_int = client1.revision(key, 1)
        assert rev_by_num_str == rev_by_num_int
        assert rev_by_num_str.revision_id == rev1_id
        assert rev_by_num_str.revision_number == 1
        assert rev_by_num_str.title == "Initial Work Item Title"
        assert rev_by_num_str.acceptance_criteria == ("Criterion A", "Criterion B")

        # UUID selector string & UUID object
        rev_by_uuid_str = client1.revision(key, str(rev1_id))
        rev_by_uuid_obj = client1.revision(key, rev1_id)
        assert rev_by_uuid_str == rev_by_num_str
        assert rev_by_uuid_obj == rev_by_num_str

        # Revise work to create revision 2
        rev2_id = uuid4()
        rev2_obj = WorkRevision(
            revision_id=rev2_id,
            work_id=work_id,
            revision_number=2,
            title="Revised Work Item Title",
            description="Updated scope details",
            scope="updated",
            acceptance_criteria=("Criterion C",),
            content_sha256=sha256(
                {"title": "Revised Work Item Title", "description": "Updated scope details"}
            ),
            created_by="owner",
            created_at=datetime.now(timezone.utc),
        )
        revise_res = client1.execute(
            _make_envelope(
                ws1,
                ReviseWorkCommand(
                    type="revise_work",
                    payload=ReviseWorkPayload(
                        work_id=work_id,
                        expected_revision_id=rev1_id,
                        revision=rev2_obj,
                    ),
                ),
            )
        )


        # Revision 1 still readable after revision 2
        rev1_still = client1.revision(key, "1")
        assert rev1_still.revision_id == rev1_id
        assert rev1_still.revision_number == 1
        assert rev1_still.title == "Initial Work Item Title"
        assert rev1_still.acceptance_criteria == ("Criterion A", "Criterion B")

        rev1_still_uuid = client1.revision(key, str(rev1_id))
        assert rev1_still_uuid.revision_id == rev1_id
        assert rev1_still_uuid.revision_number == 1

        # Revision 2 readable
        rev2_by_num = client1.revision(key, "2")
        rev2_by_uuid = client1.revision(key, str(rev2_id))
        assert rev2_by_num.revision_id == rev2_id
        assert rev2_by_num.revision_number == 2
        assert rev2_by_num.title == "Revised Work Item Title"
        assert rev2_by_num.acceptance_criteria == ("Criterion C",)
        assert rev2_by_uuid == rev2_by_num

        # Foreign key → error (workspace 2 querying key from workspace 1)
        with pytest.raises(WorkError) as exc_info:
            client2.revision(key, "1")
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_request"

        # Unknown key → error
        with pytest.raises(WorkError) as exc_info:
            client1.revision("OMP-9999", "1")
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_request"

        # Invalid selectors
        for bad_selector in ("0", "-1", "bad", "999", str(uuid4())):
            with pytest.raises(WorkError) as exc_info:
                client1.revision(key, bad_selector)
            assert exc_info.value.status == 400
            assert exc_info.value.code == "invalid_request"

        # -------------------------------------------------------------
        # Part 2: Receipts (by id equals row; foreign → error)
        # -------------------------------------------------------------
        receipt_id = uuid4()
        cand_sha = "a" * 64
        rcpt_payload = {"evidence_type": "automated_test", "run": 42}
        rcpt_sha = "b" * 64

        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws1), str(actor1)),
            )
            conn.execute(
                "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, allocated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                (cand_id, ws1, work_id, rev1_id, cand_sha, "0" * 40),
            )
            conn.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issuer, issued_at, candidate_sha256, verdict, independent) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, now(), %s, %s, %s)",
                (receipt_id, ws1, work_id, rev1_id, cand_id, "verification", json.dumps(rcpt_payload), rcpt_sha, "test-auditor", cand_sha, "PASS", False),
            )

        # Receipt by id equals row
        receipt_view = client1.receipt(receipt_id)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True, row_factory=dict_row) as conn:
            conn.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws1), str(actor1)),
            )
            cur = conn.execute(
                f"SELECT {_RECEIPT_FIELDS} FROM omp_evidence.receipts WHERE workspace_id=%s AND receipt_id=%s",
                (ws1, receipt_id),
            )
            db_row = cur.fetchone()
            assert db_row is not None

        assert receipt_view.receipt_id == db_row["receipt_id"]
        assert receipt_view.work_id == db_row["work_id"]
        assert receipt_view.revision_id == db_row["revision_id"]
        assert receipt_view.candidate_id == db_row["candidate_id"]
        assert receipt_view.kind == db_row["kind"]
        assert receipt_view.payload == db_row["payload"]
        assert receipt_view.payload_sha256 == db_row["payload_sha256"]
        assert receipt_view.issuer == db_row["issuer"]
        assert receipt_view.issued_at == db_row["issued_at"]
        assert receipt_view.candidate_sha256 == db_row["candidate_sha256"]
        assert receipt_view.verdict == db_row["verdict"]
        assert receipt_view.independent == db_row["independent"]

        # Foreign receipt → error
        with pytest.raises(WorkError) as exc_info:
            client2.receipt(receipt_id)
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_request"

        # Unknown receipt → error
        with pytest.raises(WorkError) as exc_info:
            client1.receipt(uuid4())
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_request"

        # -------------------------------------------------------------
        # Part 3: 1,005 items (create_work_batch), limit 500: each work_id once, in order, past /tree's 1,000 cap
        # -------------------------------------------------------------
        ws_batch, actor_batch = uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), ws_batch, actor_batch)

        batch_bearer = capabilities / "owner_batch.json"
        batch_bearer.write_text(
            json.dumps(
                {
                    "token": "batch-token",
                    "actor_id": str(actor_batch),
                    "actor_kind": "owner",
                    "workspaces": [str(ws_batch)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        batch_bearer.chmod(0o600)

        client_batch = WorkClient("http://testserver", ws_batch, batch_bearer, transport=transport)


        batch_inputs = tuple(
            CreateWorkInput(client_ref=f"item-ref-{i:04d}", title=f"Batch Item {i:04d}")
            for i in range(1005)
        )
        batch_res = client_batch.execute(
            _make_envelope(
                ws_batch,
                CreateWorkBatchCommand(
                    type="create_work_batch",
                    payload=CreateWorkBatchPayload(items=batch_inputs),
                ),
            )
        )

        assert len(batch_res.result.items) == 1005
        expected_work_ids = [it.work_id for it in batch_res.result.items]

        # /tree caps at 1000 items
        tree_view = client_batch.tree()
        assert len(tree_view.items) == 1000

        # Cursor pagination with limit 500 retrieves all 1,005 items
        page1 = client_batch.work_items(limit=500)
        assert len(page1.items) == 500
        assert page1.next_created_at is not None
        assert page1.next_work_id is not None

        page2 = client_batch.work_items(
            after_created_at=page1.next_created_at,
            after_work_id=page1.next_work_id,
            limit=500,
        )
        assert len(page2.items) == 500
        assert page2.next_created_at is not None
        assert page2.next_work_id is not None

        page3 = client_batch.work_items(
            after_created_at=page2.next_created_at,
            after_work_id=page2.next_work_id,
            limit=500,
        )
        assert len(page3.items) == 5
        assert page3.next_created_at is None
        assert page3.next_work_id is None

        paginated_items = list(page1.items) + list(page2.items) + list(page3.items)
        assert len(paginated_items) == 1005

        paginated_ids = [it.work_id for it in paginated_items]
        # Each work_id once
        assert len(set(paginated_ids)) == 1005
        # In strict order
        for prev_item, curr_item in zip(paginated_items, paginated_items[1:]):
            assert (prev_item.created_at, prev_item.work_id) < (curr_item.created_at, curr_item.work_id)

        # -------------------------------------------------------------
        # Part 4: work.candidate.read-only principal → 403 on all three; half pair/bad limit → 400
        # -------------------------------------------------------------
        tc_cand = TestClient(
            app,
            headers={
                "Authorization": "Bearer candidate-token",
                "X-OMP-Workspace-ID": str(ws1),
                "X-OMP-Contract-SHA256": contract_sha256(),
            },
        )
        # All three return 403 for candidate principal
        r_rev = tc_cand.get(f"/v1/work-items/{key}/revisions/1")
        assert r_rev.status_code == 403
        assert r_rev.json()["error"]["code"] == "forbidden"

        r_rcpt = tc_cand.get(f"/v1/receipts/{receipt_id}")
        assert r_rcpt.status_code == 403
        assert r_rcpt.json()["error"]["code"] == "forbidden"

        r_items = tc_cand.get(f"/v1/workspaces/{ws1}/work-items")
        assert r_items.status_code == 403
        assert r_items.json()["error"]["code"] == "forbidden"

        # Half pair / bad limit → 400
        tc_owner = TestClient(
            app,
            headers={
                "Authorization": "Bearer owner1-token",
                "X-OMP-Workspace-ID": str(ws1),
                "X-OMP-Contract-SHA256": contract_sha256(),
            },
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        sample_uuid = str(uuid4())

        # Half pairs
        r_half1 = tc_owner.get(f"/v1/workspaces/{ws1}/work-items?after_created_at={now_iso}")
        assert r_half1.status_code == 400
        assert r_half1.json()["error"]["code"] == "invalid_request"

        r_half2 = tc_owner.get(f"/v1/workspaces/{ws1}/work-items?after_work_id={sample_uuid}")
        assert r_half2.status_code == 400
        assert r_half2.json()["error"]["code"] == "invalid_request"

        # Bad limits (1..500)
        for bad_limit in (0, 501, -1, 1000, "abc"):
            r_bad = tc_owner.get(f"/v1/workspaces/{ws1}/work-items?limit={bad_limit}")
            assert r_bad.status_code == 400
            assert r_bad.json()["error"]["code"] == "invalid_request"
