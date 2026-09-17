from __future__ import annotations

import base64
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import secrets
import socket
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
    root = tmp_path_factory.mktemp("native-reads")
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
        from types import SimpleNamespace

        yield SimpleNamespace(
            client=TestClient(create_app(config, capabilities_dir=capabilities)),
            capabilities=capabilities,
            config=config,
        )


def _grant(service, workspace_id: UUID) -> None:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (workspace_id,),
            )
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
) -> tuple[int, dict]:
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
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
        json=envelope,
    )
    return response.status_code, response.json()


def _create_item(service, workspace_id: UUID, title: str = "item", description: str = "desc") -> dict:
    cmd = {
        "type": "create_work_batch",
        "payload": {
            "items": [
                {
                    "client_ref": "ref-" + secrets.token_hex(4),
                    "title": title,
                    "description": description,
                    "scope": "component:test",
                    "acceptance_criteria": ["AC-1"],
                }
            ],
            "relations": [],
        },
    }
    status, body = _command(service, workspace_id, cmd)
    assert status == 200, body
    return body["result"]["items"][0]


def test_exact_historical_survives_current_rev_change(service) -> None:
    ws = uuid4()
    _grant(service, ws)

    # 1. Create work item (revision 1)
    item = _create_item(service, ws, title="Initial Title", description="Initial Description")
    work_id = item["work_id"]
    rev1_id = item["revision_id"]
    key = item["key"]
    headers = _owner_headers(ws)

    # Read revision 1 by number (1)
    r = service.client.get(f"/v1/work-items/{key}/revisions/1", headers=headers)
    assert r.status_code == 200, r.text
    rev1_by_num = r.json()
    assert rev1_by_num["revision_number"] == 1
    assert rev1_by_num["revision_id"] == rev1_id
    assert rev1_by_num["work_id"] == work_id
    assert rev1_by_num["title"] == "Initial Title"
    assert rev1_by_num["description"] == "Initial Description"
    assert rev1_by_num["acceptance_criteria"] == ["AC-1"]

    # Read revision 1 by UUID selector on /work-items/{key}/revisions/{uuid}
    r = service.client.get(f"/v1/work-items/{key}/revisions/{rev1_id}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["revision_id"] == rev1_id

    # Read revisions list
    r = service.client.get(f"/v1/work-items/{key}/revisions", headers=headers)
    assert r.status_code == 200, r.text
    rev_list = r.json()
    assert len(rev_list["revisions"]) == 1
    assert rev_list["revisions"][0]["revision_number"] == 1

    # 2. Revise work item to revision 2
    rev2_id = str(uuid4())
    rev2_payload = {
        "revision_id": rev2_id,
        "work_id": work_id,
        "revision_number": 2,
        "title": "Revised Title",
        "description": "Revised Description",
        "scope": "component:test",
        "acceptance_criteria": ["AC-1", "AC-2"],
        "content_sha256": sha256(
            {"title": "Revised Title", "description": "Revised Description"}
        ),
        "created_by": "owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    status, body = _command(
        service,
        ws,
        {
            "type": "revise_work",
            "payload": {
                "work_id": work_id,
                "expected_revision_id": rev1_id,
                "revision": rev2_payload,
            },
        },
    )
    assert status == 200 and body["result"]["changed"] is True, body

    # Verify current work item view has updated to revision 2
    cur_item = service.client.get(f"/v1/work-items/{key}", headers=headers).json()
    assert cur_item["revision"]["revision_id"] == rev2_id
    assert cur_item["revision"]["title"] == "Revised Title"

    # 3. Exact historical reads for revision 1 SURVIVE unchanged
    r = service.client.get(f"/v1/work-items/{key}/revisions/1", headers=headers)
    assert r.status_code == 200
    rev1_survived = r.json()
    assert rev1_survived["revision_number"] == 1
    assert rev1_survived["revision_id"] == rev1_id
    assert rev1_survived["title"] == "Initial Title"
    assert rev1_survived["description"] == "Initial Description"
    assert rev1_survived["acceptance_criteria"] == ["AC-1"]

    r = service.client.get(f"/v1/work-items/{key}/revisions/{rev1_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["title"] == "Initial Title"

    # 4. Read revision 2 exact reads
    r = service.client.get(f"/v1/work-items/{key}/revisions/2", headers=headers)
    assert r.status_code == 200
    rev2_view = r.json()
    assert rev2_view["revision_number"] == 2
    assert rev2_view["revision_id"] == rev2_id
    assert rev2_view["title"] == "Revised Title"
    assert rev2_view["description"] == "Revised Description"
    assert rev2_view["acceptance_criteria"] == ["AC-1", "AC-2"]

    r = service.client.get(f"/v1/work-items/{key}/revisions/{rev2_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["revision_id"] == rev2_id

    # 5. List revisions returns both in order
    r = service.client.get(f"/v1/work-items/{key}/revisions", headers=headers)
    assert r.status_code == 200
    rev_list_all = r.json()
    assert len(rev_list_all["revisions"]) == 2
    assert rev_list_all["revisions"][0]["revision_number"] == 1
    assert rev_list_all["revisions"][1]["revision_number"] == 2


def test_wrong_workspace_and_work_identity_refusal(service) -> None:
    ws1 = uuid4()
    ws2 = uuid4()
    _grant(service, ws1)
    _grant(service, ws2)

    item1 = _create_item(service, ws1, title="Item 1")
    item2 = _create_item(service, ws1, title="Item 2")
    headers_ws1 = _owner_headers(ws1)
    headers_ws2 = _owner_headers(ws2)

    rev1_id = item1["revision_id"]

    # 1. Cross-work identity refusal within same workspace:
    # Asking for rev1_id using item2's key OMP-2
    r = service.client.get(
        f"/v1/work-items/{item2['key']}/revisions/{rev1_id}",
        headers=headers_ws1,
    )
    assert r.status_code == 400
    err = r.json()
    assert err["error"]["code"] == "invalid_request"
    assert "not_found" in err["error"]["diagnostics"]

    # Request non-existent revision selector
    r = service.client.get(
        f"/v1/work-items/{item1['key']}/revisions/999",
        headers=headers_ws1,
    )
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]

    # Request invalid non-integer non-UUID selector
    r = service.client.get(
        f"/v1/work-items/{item1['key']}/revisions/not-a-valid-selector",
        headers=headers_ws1,
    )
    assert r.status_code == 400
    assert "invalid_revision_selector" in r.json()["error"]["diagnostics"]

    # 2. Cross-workspace refusal (zero leakage of existence):
    # rev1 exists in ws1; query ws2 for rev1 by UUID via work-items route -> 400 not_found
    r = service.client.get(
        f"/v1/work-items/{item1['key']}/revisions/{rev1_id}",
        headers=headers_ws2,
    )
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]

    # Query ws2 for item1 key revisions list -> 400 not_found
    r = service.client.get(f"/v1/work-items/{item1['key']}/revisions", headers=headers_ws2)
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]

    # Query ws2 for item1 key revision 1 -> 400 not_found
    r = service.client.get(f"/v1/work-items/{item1['key']}/revisions/1", headers=headers_ws2)
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]


def test_candidate_reader_cannot_use_privileged_new_reads(service) -> None:
    ws = uuid4()
    _grant(service, ws)

    item = _create_item(service, ws, title="Reader Test Item")
    work_id = item["work_id"]
    rev_id = item["revision_id"]
    key = item["key"]

    cand_id = uuid4()
    receipt_id = uuid4()

    # Direct DB seed for candidate and receipt
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, allocated_at) "
                "VALUES (%s, %s, %s, %s, %s, now())",
                (cand_id, ws, work_id, rev_id, "a" * 64),
            )
            cur.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issued_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())",
                (receipt_id, ws, work_id, rev_id, cand_id, "verification", json.dumps({"ok": True}), "b" * 64),
            )

    # Capability with ONLY work.candidate.read
    reader = service.capabilities / "candidate_reader.json"
    reader.write_text(
        json.dumps(
            {
                "token": "candidate-reader-token",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(ws)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(cand_id)],
            }
        )
    )
    reader.chmod(0o600)

    reader_headers = {
        "Authorization": "Bearer candidate-reader-token",
        "X-OMP-Workspace-ID": str(ws),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    # All new exact-read endpoints must return 403
    assert service.client.get(f"/v1/work-items/{key}/revisions", headers=reader_headers).status_code == 403
    assert service.client.get(f"/v1/work-items/{key}/revisions/1", headers=reader_headers).status_code == 403
    assert service.client.get(f"/v1/work-items/{key}/revisions/{rev_id}", headers=reader_headers).status_code == 403
    assert service.client.get(f"/v1/receipts/{receipt_id}", headers=reader_headers).status_code == 403
    assert service.client.get(f"/v1/workspaces/{ws}/work-items", headers=reader_headers).status_code == 403


def test_exact_receipt_resolves_source_and_version(service) -> None:
    ws = uuid4()
    _grant(service, ws)

    item = _create_item(service, ws, title="Receipt Target Item")
    work_id = item["work_id"]
    rev_id = item["revision_id"]
    cand_id = uuid4()
    receipt_id = uuid4()
    headers = _owner_headers(ws)

    # Direct seed candidate & receipt
    payload = {"verdict": "PASS", "test_count": 42}
    payload_hash = sha256(payload)
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, allocated_at) "
                "VALUES (%s, %s, %s, %s, %s, now())",
                (cand_id, ws, work_id, rev_id, "c" * 64),
            )
            cur.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issued_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())",
                (receipt_id, ws, work_id, rev_id, cand_id, "verification", json.dumps(payload), payload_hash),
            )

    # Fetch exact receipt
    r = service.client.get(f"/v1/receipts/{receipt_id}", headers=headers)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["receipt_id"] == str(receipt_id)
    assert data["work_id"] == str(work_id)
    assert data["revision_id"] == str(rev_id)
    assert data["candidate_id"] == str(cand_id)
    assert data["kind"] == "verification"
    assert data["payload"] == payload
    assert data["payload_sha256"] == payload_hash
    assert data["issued_at"] is not None
    assert data["issuer"] is None
    assert data["independent"] is None

    # Unknown receipt ID -> 400 not_found
    r = service.client.get(f"/v1/receipts/{uuid4()}", headers=headers)
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]

    # Cross-workspace read of receipt -> 400 not_found
    ws_other = uuid4()
    _grant(service, ws_other)
    r = service.client.get(f"/v1/receipts/{receipt_id}", headers=_owner_headers(ws_other))
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]


def test_keyset_enumeration_beyond_1000(service) -> None:
    ws = uuid4()
    _grant(service, ws)
    headers = _owner_headers(ws)

    # Direct seed 1020 items in ws, grouping by timestamps to test tie-breaking on work_id
    total_items = 1020
    base_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=False
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            items_tuples = []
            aliases_tuples = []
            revisions_tuples = []
            for i in range(1, total_items + 1):
                work_id = uuid4()
                rev_id = uuid4()
                # 10 items share each second to verify tie-breaking on work_id
                created_at = base_time + timedelta(seconds=(i // 10))
                items_tuples.append((work_id, ws, "open", rev_id, created_at))
                aliases_tuples.append((work_id, ws, f"OMP-{i}", True, "local"))
                revisions_tuples.append((
                    rev_id,
                    work_id,
                    ws,
                    1,
                    f"Item {i}",
                    "desc",
                    "scope",
                    sha256({"title": f"Item {i}", "description": "desc"}),
                    "service",
                    created_at,
                ))

            cur.executemany(
                "INSERT INTO omp_work.work_items(work_id, workspace_id, state, current_revision_id, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                items_tuples,
            )
            cur.executemany(
                "INSERT INTO omp_work.work_aliases(work_id, workspace_id, key, primary_alias, origin) "
                "VALUES (%s, %s, %s, %s, %s)",
                aliases_tuples,
            )
            cur.executemany(
                "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                revisions_tuples,
            )
        conn.commit()

    # Paginate through all items using limit=200
    cursor: str | None = None
    all_retrieved: list[dict] = []
    page_count = 0

    while True:
        url = f"/v1/workspaces/{ws}/work-items?limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        r = service.client.get(url, headers=headers)
        assert r.status_code == 200, r.text
        page = r.json()
        items = page["items"]
        all_retrieved.extend(items)
        page_count += 1
        if page["exhausted"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] is not None
        cursor = page["next_cursor"]

    # Verify total retrieved
    assert len(all_retrieved) == total_items
    assert page_count == 6  # 200*5 + 20 = 1020

    # Verify no duplicates
    retrieved_work_ids = [it["work_id"] for it in all_retrieved]
    assert len(set(retrieved_work_ids)) == total_items

    # Verify keyset ordering is strictly ascending on (created_at, work_id)
    for idx in range(len(all_retrieved) - 1):
        prev = all_retrieved[idx]
        curr = all_retrieved[idx + 1]
        prev_created = datetime.fromisoformat(prev["revision"]["created_at"])
        curr_created = datetime.fromisoformat(curr["revision"]["created_at"])
        prev_key = (prev_created, UUID(prev["work_id"]))
        curr_key = (curr_created, UUID(curr["work_id"]))
        assert prev_key < curr_key, f"Ordering violation at index {idx}: {prev_key} not < {curr_key}"

    # Verify limit validation: 0 and 501 are rejected
    assert service.client.get(f"/v1/workspaces/{ws}/work-items?limit=0", headers=headers).status_code in (400, 422)
    assert service.client.get(f"/v1/workspaces/{ws}/work-items?limit=501", headers=headers).status_code in (400, 422)

    # Verify cursor validation:
    # 1. Malformed base64/JSON cursor
    r = service.client.get(f"/v1/workspaces/{ws}/work-items?cursor=not-a-valid-cursor", headers=headers)
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 2. Cross-workspace cursor mismatch
    other_ws = uuid4()
    foreign_cursor_payload = {
        "workspace_id": str(other_ws),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "work_id": str(uuid4()),
    }
    foreign_cursor = (
        base64.urlsafe_b64encode(json.dumps(foreign_cursor_payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    r = service.client.get(f"/v1/workspaces/{ws}/work-items?cursor={foreign_cursor}", headers=headers)
    assert r.status_code == 400
    assert "cursor_workspace_mismatch" in r.json()["error"]["diagnostics"]

    # 3. Naive timestamp rejection (strict AwareDatetime)
    naive_cursor_payload = {
        "workspace_id": str(ws),
        "created_at": "2026-01-01T00:00:00",
        "work_id": str(uuid4()),
    }
    naive_cursor = (
        base64.urlsafe_b64encode(json.dumps(naive_cursor_payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    r = service.client.get(f"/v1/workspaces/{ws}/work-items?cursor={naive_cursor}", headers=headers)
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 4. Extra-key rejection (extra forbid)
    extra_cursor_payload = {
        "workspace_id": str(ws),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "work_id": str(uuid4()),
        "extra_field": "forbidden",
    }
    extra_cursor = (
        base64.urlsafe_b64encode(json.dumps(extra_cursor_payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    r = service.client.get(f"/v1/workspaces/{ws}/work-items?cursor={extra_cursor}", headers=headers)
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]


def test_python_work_client_reads(service) -> None:
    import httpx
    from omp_work.v1.client import WorkClient
    from omp_work.v1.models import WorkRevision
    from omp_work.v1.api_models import EvidenceReceiptView, WorkRevisionListView, WorkItemsPage

    ws = uuid4()
    _grant(service, ws)

    item = _create_item(service, ws, title="Client Test Item", description="Client Test Desc")
    work_id = item["work_id"]
    rev_id = item["revision_id"]
    key = item["key"]

    owner_file = service.capabilities / "owner.json"
    work_client = WorkClient(
        base_url="http://testserver",
        workspace_id=ws,
        bearer_file=owner_file,
        transport=service.client._transport,
    )

    # 1. revision by int
    rev_by_int = work_client.revision(key, 1)
    assert isinstance(rev_by_int, WorkRevision)
    assert rev_by_int.revision_number == 1
    assert str(rev_by_int.revision_id) == rev_id

    # 2. revision by UUID string
    rev_by_uuid = work_client.revision(key, rev_id)
    assert isinstance(rev_by_uuid, WorkRevision)
    assert str(rev_by_uuid.revision_id) == rev_id

    # 3. revisions list
    rev_list = work_client.revisions(key)
    assert isinstance(rev_list, WorkRevisionListView)
    assert len(rev_list.revisions) == 1
    assert rev_list.revisions[0].revision_number == 1

    # 4. receipt
    cand_id = uuid4()
    rcpt_id = uuid4()
    legacy_rcpt_id = uuid4()
    payload = {"verdict": "PASS"}
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, allocated_at) "
                "VALUES (%s, %s, %s, %s, %s, now())",
                (cand_id, ws, work_id, rev_id, "d" * 64),
            )
            cur.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issued_at, issuer, independent) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)",
                (rcpt_id, ws, work_id, rev_id, cand_id, "verification", json.dumps(payload), sha256(payload), "owner", False),
            )
            cur.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issued_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())",
                (legacy_rcpt_id, ws, work_id, rev_id, cand_id, "verification", json.dumps(payload), sha256(payload)),
            )

    rcpt = work_client.receipt(rcpt_id)
    assert isinstance(rcpt, EvidenceReceiptView)
    assert rcpt.receipt_id == rcpt_id
    assert rcpt.payload == payload
    assert rcpt.issuer == "owner"
    assert rcpt.independent is False

    legacy_rcpt = work_client.receipt(legacy_rcpt_id)
    assert isinstance(legacy_rcpt, EvidenceReceiptView)
    assert legacy_rcpt.receipt_id == legacy_rcpt_id
    assert legacy_rcpt.payload == payload
    assert legacy_rcpt.issuer is None
    assert legacy_rcpt.independent is None

    # 5. work_items pagination
    page = work_client.work_items(limit=10)
    assert isinstance(page, WorkItemsPage)
    assert len(page.items) >= 1
    assert page.exhausted is True
    assert page.items[0].alias.key == key
    assert page.items[0].work_id == UUID(work_id)

    work_client.close()


def _make_revision(
    work_id: str | UUID,
    *,
    revision_id: str | UUID | None = None,
    revision_number: int = 2,
    title: str = "Revised Title",
    description: str = "Revised Description",
    scope: str = "component:test",
    acceptance_criteria: list[str] | None = None,
) -> dict[str, object]:
    rev_id = str(revision_id or uuid4())
    ac = acceptance_criteria or ["AC-1"]
    content = {
        "title": title,
        "description": description,
        "scope": scope,
        "acceptance_criteria": ac,
    }
    return {
        "revision_id": rev_id,
        "work_id": str(work_id),
        "revision_number": revision_number,
        "title": title,
        "description": description,
        "scope": scope,
        "acceptance_criteria": ac,
        "content_sha256": sha256(content),
        "created_by": "owner",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def test_concurrent_event_cursor_race_advisory_lock(service) -> None:
    import threading
    from unittest.mock import patch
    from omp_work.v1.store import PostgresWorkStore

    ws = uuid4()
    _grant(service, ws)

    # Create two items to act as different aggregates in the same workspace
    item1 = _create_item(service, ws, title="Item 1", description="Aggregate 1")
    item2 = _create_item(service, ws, title="Item 2", description="Aggregate 2")
    work_id_1 = item1["work_id"]
    work_id_2 = item2["work_id"]
    rev1_id = item1["revision_id"]
    rev2_id = item2["revision_id"]

    # Initial events check: establish baseline head
    headers = _owner_headers(ws)
    r_initial = service.client.get(f"/v1/workspaces/{ws}/events", headers=headers)
    assert r_initial.status_code == 200
    baseline_head = r_initial.json()["through_sequence"]
    assert baseline_head >= 2

    # We want T1 to execute revise_work on work_id_1, and pause inside _record_event after inserting into domain_events
    t1_request_id = uuid4()
    t1_entered_insert = threading.Event()
    t1_release = threading.Event()
    original_record_event = PostgresWorkStore._record_event

    def hooked_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs):
        original_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs)
        if envelope.request_id == t1_request_id:
            t1_entered_insert.set()
            assert t1_release.wait(timeout=10), "t1 timed out waiting for release"

    t1_res: dict[str, object] = {}
    t2_res: dict[str, object] = {}
    t1_error: BaseException | None = None
    t2_error: BaseException | None = None

    def run_t1():
        nonlocal t1_error
        try:
            envelope = {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(ws),
                "operation_id": str(uuid4()),
                "request_id": str(t1_request_id),
                "correlation_id": str(uuid4()),
                "command": {
                    "type": "revise_work",
                    "payload": {
                        "work_id": work_id_1,
                        "expected_revision_id": rev1_id,
                        "revision": _make_revision(work_id_1, revision_number=2, title="Item 1 Revision 2"),
                    },
                },
            }
            res = service.client.post("/v1/commands", headers=headers, json=envelope)
            t1_res["status"] = res.status_code
            t1_res["body"] = res.json()
        except BaseException as ex:
            t1_error = ex

    def run_t2():
        nonlocal t2_error
        try:
            envelope = {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(ws),
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "command": {
                    "type": "revise_work",
                    "payload": {
                        "work_id": work_id_2,
                        "expected_revision_id": rev2_id,
                        "revision": _make_revision(work_id_2, revision_number=2, title="Item 2 Revision 2"),
                    },
                },
            }
            res = service.client.post("/v1/commands", headers=headers, json=envelope)
            t2_res["status"] = res.status_code
            t2_res["body"] = res.json()
        except BaseException as ex:
            t2_error = ex

    with patch.object(PostgresWorkStore, "_record_event", hooked_record_event):
        thread_1 = threading.Thread(target=run_t1)
        thread_1.start()

        assert t1_entered_insert.wait(timeout=5), f"T1 failed to enter insert: res={t1_res}, err={t1_error}"

        # T1 is now paused in transaction holding pg_advisory_xact_lock on ws.
        # Launch T2 in the same workspace on a different aggregate (work_id_2).
        thread_2 = threading.Thread(target=run_t2)
        thread_2.start()

        # Give T2 a moment to run until it attempts to acquire the advisory lock
        thread_2.join(timeout=0.3)
        assert thread_2.is_alive(), f"T2 should be blocked waiting for advisory lock: res={t2_res}, err={t2_error}"
        assert "status" not in t2_res

        # Verify in pg_locks that there is an ungranted advisory lock waiting
        with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS blocked FROM pg_locks WHERE locktype = 'advisory' AND NOT granted")
                assert cur.fetchone()[0] >= 1, "Expected blocked advisory lock in pg_locks"

        # Concurrent reader poll: reader must NOT see missing T1 sequence,
        # nor can it advance past T1 because T1 is uncommitted and T2 is blocked.
        poll_mid = service.client.get(f"/v1/workspaces/{ws}/events", headers=headers)
        assert poll_mid.status_code == 200
        mid_body = poll_mid.json()
        assert mid_body["through_sequence"] == baseline_head
        # Sequences returned do not include any sequences beyond baseline_head
        for ev in mid_body["items"]:
            assert ev["sequence"] <= baseline_head

        # Now release T1 to commit
        t1_release.set()
        thread_1.join(timeout=5)
        thread_2.join(timeout=5)

        assert t1_res.get("status") == 200, t1_res
        assert t2_res.get("status") == 200, t2_res

    # Reader polls events after baseline_head: retrieves both events in exact order without skips
    poll_after = service.client.get(
        f"/v1/workspaces/{ws}/events?after_sequence={baseline_head}", headers=headers
    )
    assert poll_after.status_code == 200
    after_body = poll_after.json()
    new_events = after_body["items"]
    assert len(new_events) == 2

    # In strict sequence order with no gaps
    assert new_events[0]["sequence"] == baseline_head + 1
    assert new_events[1]["sequence"] == baseline_head + 2
    assert UUID(new_events[0]["aggregate_id"]) == UUID(work_id_1)
    assert UUID(new_events[1]["aggregate_id"]) == UUID(work_id_2)
    assert new_events[0]["event_type"] == "revise_work"
    assert new_events[1]["event_type"] == "revise_work"


def test_cross_workspace_event_sequence_isolation(service) -> None:
    import threading
    from unittest.mock import patch
    from omp_work.v1.store import PostgresWorkStore

    ws_a = uuid4()
    ws_b = uuid4()
    _grant(service, ws_a)
    _grant(service, ws_b)

    item_a = _create_item(service, ws_a, title="Item A")
    item_b = _create_item(service, ws_b, title="Item B")

    # Baseline heads
    r_a0 = service.client.get(f"/v1/workspaces/{ws_a}/events", headers=_owner_headers(ws_a))
    assert r_a0.status_code == 200
    head_a0 = r_a0.json()["through_sequence"]

    r_b0 = service.client.get(f"/v1/workspaces/{ws_b}/events", headers=_owner_headers(ws_b))
    assert r_b0.status_code == 200
    head_b0 = r_b0.json()["through_sequence"]

    t1_request_id = uuid4()
    t1_entered = threading.Event()
    t1_release = threading.Event()
    t1_res: dict[str, object] = {}
    t1_error: BaseException | None = None
    original_record_event = PostgresWorkStore._record_event

    def hooked_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs):
        original_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs)
        if envelope.request_id == t1_request_id:
            t1_entered.set()
            assert t1_release.wait(timeout=10), "t1 timed out waiting for release"

    with patch.object(PostgresWorkStore, "_record_event", hooked_record_event):
        def run_a():
            nonlocal t1_error
            try:
                envelope = {
                    "api_version": "work.omp.dev/v1",
                    "workspace_id": str(ws_a),
                    "operation_id": str(uuid4()),
                    "request_id": str(t1_request_id),
                    "correlation_id": str(uuid4()),
                    "command": {
                        "type": "revise_work",
                        "payload": {
                            "work_id": item_a["work_id"],
                            "expected_revision_id": item_a["revision_id"],
                            "revision": _make_revision(item_a["work_id"], revision_number=2, title="Item A Rev 2"),
                        },
                    },
                }
                res = service.client.post("/v1/commands", headers=_owner_headers(ws_a), json=envelope)
                t1_res["status"] = res.status_code
                t1_res["body"] = res.json()
            except BaseException as ex:
                t1_error = ex

        thread_a = threading.Thread(target=run_a)
        thread_a.start()
        assert t1_entered.wait(timeout=5), f"T1 failed to enter insert: res={t1_res}, err={t1_error}"

        # T1 in ws_a has allocated sequence but is uncommitted.
        # ws_b uses independent advisory lock; must execute immediately and allocate higher global sequence.
        status_b, body_b = _command(
            service,
            ws_b,
            {
                "type": "revise_work",
                "payload": {
                    "work_id": item_b["work_id"],
                    "expected_revision_id": item_b["revision_id"],
                    "revision": _make_revision(item_b["work_id"], revision_number=2, title="Item B Rev 2"),
                },
            },
        )
        assert status_b == 200, body_b

        # 1. Assert W_b head while W_a lower-seq transaction is still pending:
        # W_b head moved forward to S_b, completely unblocked by W_a
        r_b_mid = service.client.get(f"/v1/workspaces/{ws_b}/events", headers=_owner_headers(ws_b))
        assert r_b_mid.status_code == 200
        head_b_mid = r_b_mid.json()["through_sequence"]
        assert head_b_mid > head_b0
        # All events in W_b belong exclusively to W_b
        for ev in r_b_mid.json()["items"]:
            assert UUID(ev["workspace_id"]) == ws_b

        # 2. Assert W_a head while W_a lower-seq transaction is still pending:
        # W_a head remains at head_a0 because T1 has not committed yet
        r_a_mid = service.client.get(f"/v1/workspaces/{ws_a}/events", headers=_owner_headers(ws_a))
        assert r_a_mid.status_code == 200
        assert r_a_mid.json()["through_sequence"] == head_a0

        # Now release T1 in ws_a to commit
        t1_release.set()
        thread_a.join(timeout=5)
        assert t1_res.get("status") == 200, t1_res

    # 3. Assert W_a late event read after release / reader restart:
    # Reader querying ws_a after head_a0 now observes the committed event
    r_a_after = service.client.get(
        f"/v1/workspaces/{ws_a}/events?after_sequence={head_a0}", headers=_owner_headers(ws_a)
    )
    assert r_a_after.status_code == 200
    a_items = r_a_after.json()["items"]
    assert len(a_items) == 1
    assert a_items[0]["event_type"] == "revise_work"
    assert UUID(a_items[0]["aggregate_id"]) == UUID(item_a["work_id"])
    assert UUID(a_items[0]["workspace_id"]) == ws_a
    # Its sequence was lower than W_b's committed sequence head_b_mid
    assert a_items[0]["sequence"] < head_b_mid


def test_same_aggregate_serializable_retry_and_hash_chain(service) -> None:
    import threading
    from unittest.mock import patch
    from omp_work.v1.store import PostgresWorkStore

    ws = uuid4()
    _grant(service, ws)

    # Create source work item and two target items
    src_item = _create_item(service, ws, title="Source Work")
    tgt1_item = _create_item(service, ws, title="Target 1")
    tgt2_item = _create_item(service, ws, title="Target 2")
    src_id = src_item["work_id"]
    tgt1_id = tgt1_item["work_id"]
    tgt2_id = tgt2_item["work_id"]

    headers = _owner_headers(ws)

    # We run two concurrent put_relation commands sharing the SAME source work (same aggregate)
    # put_relation runs under SERIALIZABLE transaction isolation.
    t1_request_id = uuid4()
    t1_entered_insert = threading.Event()
    t1_release = threading.Event()
    original_record_event = PostgresWorkStore._record_event

    def hooked_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs):
        original_record_event(self, cur, envelope, actor_id, actor_kind, result, **kwargs)
        if envelope.request_id == t1_request_id:
            t1_entered_insert.set()
            assert t1_release.wait(timeout=10), "t1 timed out waiting for release"

    t1_res: dict[str, object] = {}
    t2_res: dict[str, object] = {}
    t1_error: BaseException | None = None
    t2_error: BaseException | None = None

    def run_t1():
        nonlocal t1_error
        try:
            envelope = {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(ws),
                "operation_id": str(uuid4()),
                "request_id": str(t1_request_id),
                "correlation_id": str(uuid4()),
                "command": {
                    "type": "put_relation",
                    "payload": {
                        "relation": {
                            "workspace_id": str(ws),
                            "source_work_id": src_id,
                            "target_work_id": tgt1_id,
                            "kind": "blocks",
                        },
                    },
                },
            }
            res = service.client.post("/v1/commands", headers=headers, json=envelope)
            t1_res["status"] = res.status_code
            t1_res["body"] = res.json()
        except BaseException as ex:
            t1_error = ex

    def run_t2():
        nonlocal t2_error
        try:
            envelope = {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(ws),
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "command": {
                    "type": "put_relation",
                    "payload": {
                        "relation": {
                            "workspace_id": str(ws),
                            "source_work_id": src_id,
                            "target_work_id": tgt2_id,
                            "kind": "blocks",
                        },
                    },
                },
            }
            res = service.client.post("/v1/commands", headers=headers, json=envelope)
            t2_res["status"] = res.status_code
            t2_res["body"] = res.json()
        except BaseException as ex:
            t2_error = ex

    with patch.object(PostgresWorkStore, "_record_event", hooked_record_event):
        thread_1 = threading.Thread(target=run_t1)
        thread_1.start()

        assert t1_entered_insert.wait(timeout=5), f"T1 failed to enter insert: res={t1_res}, err={t1_error}"

        # T1 has taken snapshot, executed put_relation, acquired advisory lock, inserted event, and is paused.
        # Now launch T2: it takes a snapshot while T1 is uncommitted, executes put_relation, and
        # attempts to acquire the per-workspace advisory lock in _record_event.
        thread_2 = threading.Thread(target=run_t2)
        thread_2.start()

        # Wait for T2 to block waiting on advisory lock
        thread_2.join(timeout=0.3)
        assert thread_2.is_alive(), f"T2 should be blocked waiting for lock: res={t2_res}, err={t2_error}"

        # Release T1 to commit
        t1_release.set()
        thread_1.join(timeout=5)
        thread_2.join(timeout=5)

        # Both commands must succeed safely (T2 either commits or retries via native SSI retry loop in store.execute)
        assert t1_res.get("status") == 200, t1_res
        assert t2_res.get("status") == 200, t2_res
        assert t1_res["body"]["receipt"]["state"] == "applied"
        assert t2_res["body"]["receipt"]["state"] == "applied"

    # Verify contiguous per-aggregate previous_event_sha256 hash chain on source work item
    r = service.client.get(f"/v1/workspaces/{ws}/events", headers=headers)
    assert r.status_code == 200
    events = [e for e in r.json()["items"] if e["aggregate_id"] == src_id]
    # Exactly two events recorded under src_id aggregate (put_relation 1 and 2)
    assert len(events) == 2
    ev1, ev2 = events[0], events[1]
    assert ev1["previous_event_sha256"] is None
    assert ev2["previous_event_sha256"] == ev1["event_sha256"]

    # Verify canonical hash recompute for both events
    for ev in (ev1, ev2):
        expected_hash = sha256(
            {
                "aggregate_id": str(src_id),
                "operation_id": str(ev["operation_id"]),
                "previous_event_sha256": ev["previous_event_sha256"],
                "payload_sha256": ev["payload_sha256"],
            }
        )
        assert ev["event_sha256"] == expected_hash


def test_event_cursor_validation_and_candidate_refusal(service) -> None:
    ws = uuid4()
    _grant(service, ws)
    _create_item(service, ws, title="Cursor Item 1")
    _create_item(service, ws, title="Cursor Item 2")
    headers = _owner_headers(ws)

    # Establish head
    r = service.client.get(f"/v1/workspaces/{ws}/events", headers=headers)
    assert r.status_code == 200
    head = r.json()["through_sequence"]
    assert head >= 2

    # --- Strict Model Cursor Validation ---
    def _encode_cursor(payload: dict) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )

    # 1. Non-base64 / corrupt cursor
    r = service.client.get(f"/v1/workspaces/{ws}/events?cursor=bad!cursor", headers=headers)
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 2. Strict type checks: REJECT bool (int(True) silent coercion prevented)
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': True, 'through_sequence': head})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 3. Strict type checks: REJECT fractional numbers (int(1.5) silent coercion prevented)
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': 1.5, 'through_sequence': head})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 4. Strict type checks: REJECT extra fields
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': 0, 'through_sequence': head, 'extra': 'injected'})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 5. Cross-workspace cursor mismatch
    other_ws = uuid4()
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(other_ws), 'after_sequence': 0, 'through_sequence': head})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "cursor_workspace_mismatch" in r.json()["error"]["diagnostics"]

    # 6. Negative bounds in cursor
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': -1, 'through_sequence': head})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "negative_sequence_bound" in r.json()["error"]["diagnostics"]

    # 7. Invalid sequence window in cursor
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': head, 'through_sequence': head - 1})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "invalid_sequence_window" in r.json()["error"]["diagnostics"]

    # 8. Future bound in cursor
    r = service.client.get(
        f"/v1/workspaces/{ws}/events?cursor={_encode_cursor({'workspace_id': str(ws), 'after_sequence': 0, 'through_sequence': head + 100})}",
        headers=headers,
    )
    assert r.status_code == 400
    assert "future_bound" in r.json()["error"]["diagnostics"]

    # --- Query Parameter Validation (Upfront HTTP rejection) ---
    r = service.client.get(f"/v1/workspaces/{ws}/events?after_sequence=-1", headers=headers)
    assert r.status_code in (400, 422)
    assert r.json()["error"]["code"] == "invalid_request"

    r = service.client.get(f"/v1/workspaces/{ws}/events?through_sequence=-5", headers=headers)
    assert r.status_code in (400, 422)
    assert r.json()["error"]["code"] == "invalid_request"

    r = service.client.get(f"/v1/workspaces/{ws}/events?after_sequence=5&through_sequence=2", headers=headers)
    assert r.status_code == 400
    assert "invalid_sequence_window" in r.json()["error"]["diagnostics"]

    r = service.client.get(f"/v1/workspaces/{ws}/events?through_sequence={head + 100}", headers=headers)
    assert r.status_code == 400
    assert "future_bound" in r.json()["error"]["diagnostics"]

    # --- Rollback Sequence Holes Don't Require Contiguous Integers ---
    # Burn a sequence by rolling back an uncommitted event transaction
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            try:
                with conn.transaction():
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                        (str(ws), str(OWNER)),
                    )
                    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"omp_audit:events:{ws}",))
                    cur.execute(
                        "INSERT INTO omp_audit.domain_events(event_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,actor_id,actor_kind,capability_id,request_id,correlation_id,operation_id,causation_id,event_type,outcome,payload,payload_sha256,previous_event_sha256,event_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (uuid4(), ws, "workspace", ws, 1, OWNER, "owner", uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "aborted_test", "applied", "{}", "0"*64, None, "0"*64),
                    )
                    raise RuntimeError("burn sequence allocation via rollback")
            except RuntimeError:
                pass

    # Now commit a valid item after the rolled-back sequence
    _create_item(service, ws, title="Item After Rolled Back Sequence")
    r_hole = service.client.get(f"/v1/workspaces/{ws}/events", headers=headers)
    assert r_hole.status_code == 200
    events_with_hole = r_hole.json()["items"]
    # Verify events are strictly ascending even with an integer gap from the rollback
    seqs = [e["sequence"] for e in events_with_hole]
    for i in range(len(seqs) - 1):
        assert seqs[i] < seqs[i + 1]
    # Check that gap exists
    has_gap = any(seqs[i + 1] - seqs[i] > 1 for i in range(len(seqs) - 1))
    assert has_gap, "Expected non-contiguous integer gap from rollback"

    # --- Persist Opaque Continuation + Fixed Through Watermark + Restart Client ---
    # We want to paginate events with limit=2, persist cursor, write future events,
    # restart client, and verify page finish without gap or duplicate or future leak.
    r_p1 = service.client.get(f"/v1/workspaces/{ws}/events?limit=2", headers=headers)
    assert r_p1.status_code == 200
    p1 = r_p1.json()
    assert len(p1["items"]) == 2
    assert p1["next_cursor"] is not None
    watermark_through = p1["through_sequence"]
    saved_cursor = p1["next_cursor"]

    # Write future events to workspace
    _create_item(service, ws, title="Future Write 1")
    _create_item(service, ws, title="Future Write 2")

    # Restart reader client: instantiate fresh TestClient to simulate reader process restart
    restarted_client = TestClient(service.client.app)
    all_resumed: list[dict] = list(p1["items"])
    curr_cursor = saved_cursor

    while curr_cursor is not None:
        r_next = restarted_client.get(
            f"/v1/workspaces/{ws}/events?limit=2&cursor={curr_cursor}", headers=headers
        )
        assert r_next.status_code == 200
        p_next = r_next.json()
        assert p_next["through_sequence"] == watermark_through
        all_resumed.extend(p_next["items"])
        if p_next["exhausted"]:
            assert p_next["next_cursor"] is None
            break
        curr_cursor = p_next["next_cursor"]

    # All resumed events are <= watermark_through (future writes did NOT leak)
    for ev in all_resumed:
        assert ev["sequence"] <= watermark_through

    # No duplicates, strictly ascending
    resumed_ids = [e["event_id"] for e in all_resumed]
    assert len(resumed_ids) == len(set(resumed_ids))
    for i in range(len(all_resumed) - 1):
        assert all_resumed[i]["sequence"] < all_resumed[i + 1]["sequence"]

    # Catching up since watermark receives future writes
    r_catchup = restarted_client.get(
        f"/v1/workspaces/{ws}/events?after_sequence={watermark_through}", headers=headers
    )
    assert r_catchup.status_code == 200
    catchup_events = r_catchup.json()["items"]
    assert len(catchup_events) >= 2
    for ev in catchup_events:
        assert ev["sequence"] > watermark_through

    # Follow p1.next_sequence with after_sequence and fixed watermark through_sequence
    r_resumed = restarted_client.get(
        f"/v1/workspaces/{ws}/events?after_sequence={p1['next_sequence']}&through_sequence={watermark_through}",
        headers=headers,
    )
    assert r_resumed.status_code == 200
    p_resumed = r_resumed.json()
    assert p_resumed["through_sequence"] == watermark_through
    expected_hole_ids = [e["event_id"] for e in events_with_hole]
    combined_ids = [e["event_id"] for e in p1["items"]] + [e["event_id"] for e in p_resumed["items"]]
    assert combined_ids == expected_hole_ids
    assert len(combined_ids) == len(set(combined_ids))

    # Empty final poll returns no items and next_sequence unchanged
    r_empty = restarted_client.get(
        f"/v1/workspaces/{ws}/events?after_sequence={p_resumed['next_sequence']}&through_sequence={watermark_through}",
        headers=headers,
    )
    assert r_empty.status_code == 200
    p_empty = r_empty.json()
    assert p_empty["items"] == []
    assert p_empty["next_sequence"] == p_resumed["next_sequence"]

    # --- Candidate Reader 403 Refusal ---
    reader = service.capabilities / "candidate_reader_events.json"
    cand_id = uuid4()
    reader.write_text(
        json.dumps(
            {
                "token": "cand-token-events",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(ws)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(cand_id)],
            }
        )
    )
    reader.chmod(0o600)
    cand_headers = {
        "Authorization": "Bearer cand-token-events",
        "X-OMP-Workspace-ID": str(ws),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    r_cand = service.client.get(f"/v1/workspaces/{ws}/events", headers=cand_headers)
    assert r_cand.status_code == 403


def test_repositories_keyset_pagination(service) -> None:
    ws = uuid4()
    _grant(service, ws)
    headers = _owner_headers(ws)

    # Seed 503 rows with timestamp ties in isolated fixture DB
    base_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    records = []
    for i in range(503):
        r_id = uuid4()
        ts = base_ts + timedelta(seconds=i // 5)
        records.append(
            (
                r_id,
                ws,
                f"repo-{i:04d}",
                f"Repo {i}",
                f"https://github.com/example/repo-{i}",
                False,
                json.dumps({"origin": "native"}),
                ts,
            )
        )

    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.executemany(
                "INSERT INTO omp_work.repositories(repository_id, workspace_id, key, name, url, archived, provenance, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                records,
            )

    # Traverse via returned cursors limit 200
    all_retrieved: list[dict] = []
    cursor = None
    while True:
        url = f"/v1/workspaces/{ws}/repositories?limit=200"
        if cursor:
            url += f"&cursor={cursor}"
        r = service.client.get(url, headers=headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert UUID(data["workspace_id"]) == ws
        assert data["limit"] == 200
        all_retrieved.extend(data["repositories"])
        if data["exhausted"]:
            assert data["next_cursor"] is None
            break
        assert data["next_cursor"] is not None
        cursor = data["next_cursor"]

    # Assert exact seeded IDs once in stable order and terminal exhaustion
    assert len(all_retrieved) == 503
    retrieved_ids = [UUID(repo["repository_id"]) for repo in all_retrieved]
    assert len(retrieved_ids) == len(set(retrieved_ids))
    expected_records = sorted(records, key=lambda r: (r[7], r[0]))
    expected_ids = [r[0] for r in expected_records]
    assert retrieved_ids == expected_ids

    # Validate cross-workspace token denied
    other_ws = uuid4()
    foreign_cursor_payload = {
        "workspace_id": str(other_ws),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository_id": str(uuid4()),
    }
    foreign_cursor = (
        base64.urlsafe_b64encode(json.dumps(foreign_cursor_payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    r_foreign = service.client.get(
        f"/v1/workspaces/{ws}/repositories?cursor={foreign_cursor}", headers=headers
    )
    assert r_foreign.status_code == 400
    assert "cursor_workspace_mismatch" in r_foreign.json()["error"]["diagnostics"]

    # Validate malformed timestamp token 400 malformed_cursor
    malformed_cursor_payload = {
        "workspace_id": str(ws),
        "created_at": "not-a-valid-timestamp",
        "repository_id": str(uuid4()),
    }
    malformed_cursor = (
        base64.urlsafe_b64encode(json.dumps(malformed_cursor_payload).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    r_malformed = service.client.get(
        f"/v1/workspaces/{ws}/repositories?cursor={malformed_cursor}", headers=headers
    )
    assert r_malformed.status_code == 400
    assert "malformed_cursor" in r_malformed.json()["error"]["diagnostics"]


def test_repositories_read_and_work_item_view(service) -> None:
    ws = uuid4()
    _grant(service, ws)
    repo_id = uuid4()
    headers = _owner_headers(ws)

    # Register repository directly into omp_work.repositories
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.repositories(repository_id, workspace_id, key, name, url, archived, provenance) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (repo_id, ws, "core-repo", "Core Repository", "https://github.com/example/core", False, json.dumps({"origin": "native"})),
            )

    # Read repositories via GET /v1/workspaces/{ws}/repositories
    r = service.client.get(f"/v1/workspaces/{ws}/repositories", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert UUID(body["workspace_id"]) == ws
    assert body["next_cursor"] is None
    assert body["exhausted"] is True
    repos = body["repositories"]
    assert len(repos) == 1
    repo = repos[0]
    assert repo["repository_id"] == str(repo_id)
    assert repo["key"] == "core-repo"
    assert repo["name"] == "Core Repository"
    assert repo["url"] == "https://github.com/example/core"
    assert repo["archived"] is False
    assert repo["provenance"] == {"origin": "native"}

    # Create work item and link it to repository
    item = _create_item(service, ws, title="Repo Item")
    work_id = item["work_id"]
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "UPDATE omp_work.work_items SET repository_id = %s WHERE work_id = %s",
                (repo_id, UUID(work_id)),
            )

    # Read work items via GET /v1/workspaces/{ws}/work-items
    r_items = service.client.get(f"/v1/workspaces/{ws}/work-items", headers=headers)
    assert r_items.status_code == 200
    matched = [it for it in r_items.json()["items"] if it["work_id"] == work_id][0]
    assert matched["repository_id"] == str(repo_id)

    # Read single item via GET /v1/work-items/{key}
    r_single = service.client.get(f"/v1/work-items/{item['key']}", headers=headers)
    assert r_single.status_code == 200
    assert r_single.json()["repository_id"] == str(repo_id)

    # Candidate reader refusal (403)
    reader = service.capabilities / "candidate_reader_repo.json"
    cand_id = uuid4()
    reader.write_text(
        json.dumps(
            {
                "token": "cand-token-repo",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(ws)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(cand_id)],
            }
        )
    )
    reader.chmod(0o600)
    cand_headers = {
        "Authorization": "Bearer cand-token-repo",
        "X-OMP-Workspace-ID": str(ws),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    r_cand = service.client.get(f"/v1/workspaces/{ws}/repositories", headers=cand_headers)
    assert r_cand.status_code == 403


def test_python_work_client_events_and_repositories(service) -> None:
    from omp_work.v1.client import WorkClient
    from omp_work.v1.api_models import DomainEventsPage, RepositoryListView

    ws = uuid4()
    _grant(service, ws)
    _create_item(service, ws, title="Client Events Item")

    repo_id = uuid4()
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws), str(OWNER)),
            )
            cur.execute(
                "INSERT INTO omp_work.repositories(repository_id, workspace_id, key, name, url, archived, provenance) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (repo_id, ws, "client-repo", "Client Repository", "https://github.com/example/client-repo", False, json.dumps({})),
            )

    owner_file = service.capabilities / "owner.json"
    work_client = WorkClient(
        base_url="http://testserver",
        workspace_id=ws,
        bearer_file=owner_file,
        transport=service.client._transport,
    )

    # 1. events()
    events_page = work_client.events(limit=5)
    assert isinstance(events_page, DomainEventsPage)
    assert len(events_page.items) >= 1
    assert events_page.workspace_id == ws
    assert events_page.items[0].sequence >= 1
    assert events_page.items[0].event_sha256 is not None

    # 2. repositories()
    repos_view = work_client.repositories()
    assert isinstance(repos_view, RepositoryListView)
    assert len(repos_view.repositories) == 1
    assert repos_view.repositories[0].repository_id == repo_id
    assert repos_view.repositories[0].key == "client-repo"

    work_client.close()
