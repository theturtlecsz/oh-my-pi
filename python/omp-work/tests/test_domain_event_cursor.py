from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from uuid import UUID, uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import DomainEventView
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    FocusSlot,
    SetFocusCommand,
    SetFocusPayload,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkError
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700, exist_ok=True)
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


def test_regression_uncommitted_lock_reader_watermark_and_paging(
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

        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "owner.json",
            transport=tc._transport,
        )

        # 1. Raw session takes the workspace lock and inserts a domain_events row (seq N) uncommitted
        raw_conn = psycopg.connect(**config.connection_kwargs("omp_work_app"))
        raw_conn.autocommit = False
        raw_cur = raw_conn.cursor()
        raw_cur.execute("SET LOCAL search_path = pg_catalog")
        raw_cur.execute(
            "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
            (str(workspace_id), str(actor_id)),
        )
        raw_cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended('omp_audit.domain_events:' || %s, 0))",
            (workspace_id,),
        )
        raw_event_id = uuid4()
        raw_op_id = uuid4()
        raw_payload = {"source": "raw_session"}
        raw_payload_bytes = json.dumps(raw_payload)
        raw_payload_hash = hashlib.sha256(raw_payload_bytes.encode()).hexdigest()
        raw_event_hash = hashlib.sha256(b"raw-event-hash").hexdigest()
        raw_cur.execute(
            "INSERT INTO omp_audit.domain_events("
            "event_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,"
            "actor_id,actor_kind,capability_id,request_id,correlation_id,operation_id,"
            "causation_id,event_type,outcome,payload,payload_sha256,previous_event_sha256,event_sha256"
            ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING sequence",
            (
                raw_event_id,
                workspace_id,
                "workspace",
                workspace_id,
                1,
                actor_id,
                "owner",
                raw_op_id,
                uuid4(),
                uuid4(),
                raw_op_id,
                raw_op_id,
                "raw_uncommitted_event",
                "applied",
                raw_payload_bytes,
                raw_payload_hash,
                None,
                raw_event_hash,
            ),
        )
        seq_n = int(raw_cur.fetchone()[0])

        # 2. A service command started meanwhile shows wait_event_type 'Lock' in pg_stat_activity
        cmd_done = threading.Event()
        cmd_error: list[Exception | None] = [None]
        cmd_result: list[object] = [None]

        def run_service_command() -> None:
            try:
                command = CreateWorkBatchCommand(
                    type="create_work_batch",
                    payload=CreateWorkBatchPayload(
                        items=(
                            CreateWorkInput(client_ref="ref-reg-1", title="reg-work-1"),
                        )
                    ),
                )
                envelope = CommandEnvelope(
                    api_version="work.omp.dev/v1",
                    workspace_id=workspace_id,
                    operation_id=uuid4(),
                    request_id=uuid4(),
                    correlation_id=uuid4(),
                    command=command,
                )
                cmd_result[0] = client.execute(envelope)
            except Exception as e:
                cmd_error[0] = e
            finally:
                cmd_done.set()

        cmd_thread = threading.Thread(target=run_service_command)
        cmd_thread.start()

        # Verify wait_event_type 'Lock' in pg_stat_activity
        found_lock = False
        with psycopg.connect(
            **config.connection_kwargs("postgres"),
            row_factory=psycopg.rows.dict_row,
            autocommit=True,
        ) as stat_conn:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                rows = stat_conn.execute(
                    "SELECT wait_event_type, wait_event, query "
                    "FROM pg_stat_activity "
                    "WHERE backend_type = 'client backend' "
                    "  AND pid != pg_backend_pid() "
                    "  AND wait_event_type = 'Lock'"
                ).fetchall()
                if rows:
                    found_lock = True
                    break
                time.sleep(0.05)
        assert found_lock, "service command was not observed with wait_event_type 'Lock'"

        # 3. Reader page ends before N
        page_before = client.events(after_sequence=0)
        assert page_before.watermark_sequence < seq_n
        assert all(ev.sequence < seq_n for ev in page_before.events)
        saved_next_after_sequence = page_before.next_after_sequence
        assert saved_next_after_sequence < seq_n

        # 4. After commit the command finishes with seq > N
        raw_conn.commit()
        raw_conn.close()

        cmd_thread.join(timeout=10)
        assert cmd_done.is_set()
        assert cmd_error[0] is None
        assert cmd_result[0] is not None

        # 5. Paging from saved next_after_sequence returns N then it
        page_after = client.events(after_sequence=saved_next_after_sequence)
        sequences = [ev.sequence for ev in page_after.events]
        assert len(sequences) >= 2
        assert sequences[0] == seq_n
        assert sequences[1] > seq_n
        assert page_after.events[0].event_type == "raw_uncommitted_event"
        assert page_after.events[1].event_type == "create_work_batch"


def test_concurrent_commands_reader_restart_ordering(
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

        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)

        # Initialize workspace in omp_control.workspaces
        with psycopg.connect(
            **config.connection_kwargs("postgres"), autocommit=True
        ) as conn:
            conn.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
                (workspace_id,),
            )

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)

        # Launch >= 50 concurrent commands
        num_commands = 50
        errors: list[Exception] = []

        def execute_one(i: int) -> None:
            cl = WorkClient(
                "http://testserver",
                workspace_id,
                capabilities / "owner.json",
                transport=tc._transport,
            )
            owner_id = uuid4()
            envelope = CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=SetFocusCommand(
                    type="set_focus",
                    payload=SetFocusPayload(
                        slot=FocusSlot(
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            work_id=None,
                            version=1,
                        ),
                        expected_version=0,
                    ),
                ),
            )
            try:
                cl.execute(envelope)
            except Exception as err:
                errors.append(err)

        collected_events: list[DomainEventView] = []
        cursor = [0]
        stop_reader = threading.Event()
        reader_error: list[Exception | None] = [None]

        def reader_loop() -> None:
            try:
                cl = WorkClient(
                    "http://testserver",
                    workspace_id,
                    capabilities / "owner.json",
                    transport=tc._transport,
                )
                while not stop_reader.is_set():
                    page = cl.events(after_sequence=cursor[0], limit=10)
                    for ev in page.events:
                        collected_events.append(ev)
                    cursor[0] = page.next_after_sequence
                    time.sleep(0.01)
            except Exception as err:
                reader_error[0] = err

        reader_thread = threading.Thread(target=reader_loop)
        reader_thread.start()

        with ThreadPoolExecutor(max_workers=num_commands) as executor:
            futures = [executor.submit(execute_one, i) for i in range(num_commands)]
            for f in futures:
                f.result()

        stop_reader.set()
        reader_thread.join(timeout=5)
        assert reader_error[0] is None
        assert not errors, f"Concurrent commands had errors: {errors}"

        # Reader restart: instantiate a fresh client and continue from cursor
        restarted_client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "owner.json",
            transport=tc._transport,
        )
        while True:
            page = restarted_client.events(after_sequence=cursor[0], limit=20)
            for ev in page.events:
                collected_events.append(ev)
            cursor[0] = page.next_after_sequence
            if not page.has_more and cursor[0] >= page.watermark_sequence:
                break

        # Verification: each workspace event once, ascending
        assert len(collected_events) == num_commands
        seqs = [ev.sequence for ev in collected_events]
        assert seqs == sorted(seqs), "Events were not in strictly ascending order"
        assert len(set(seqs)) == len(seqs), "Duplicate event sequences found"
        event_ids = [ev.event_id for ev in collected_events]
        assert len(set(event_ids)) == len(event_ids), "Duplicate event IDs found"


def test_empty_page_and_permissions(
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
        other_workspace_id = uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)

        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)

        # 1. Full owner (work.read, work.mutate)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)

        # 2. Candidate reader only (work.candidate.read with candidate_ids)
        (capabilities / "candidate_reader.json").write_text(
            json.dumps(
                {
                    "token": "candidate-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "agent",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.candidate.read"],
                    "candidate_ids": [str(uuid4())],
                }
            )
        )
        (capabilities / "candidate_reader.json").chmod(0o600)

        # 3. work.read with candidate_ids
        (capabilities / "bounded_reader.json").write_text(
            json.dumps(
                {
                    "token": "bounded-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "agent",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read"],
                    "candidate_ids": [str(uuid4())],
                }
            )
        )
        (capabilities / "bounded_reader.json").chmod(0o600)

        # 4. Member of different workspace
        (capabilities / "outsider.json").write_text(
            json.dumps(
                {
                    "token": "outsider-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": "owner",
                    "workspaces": [str(other_workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "outsider.json").chmod(0o600)

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)

        owner_client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "owner.json",
            transport=tc._transport,
        )

        # Empty page on fresh workspace keeps after
        page_empty = owner_client.events(after_sequence=42)
        assert page_empty.events == ()
        assert page_empty.watermark_sequence == 0
        assert page_empty.next_after_sequence == 42
        assert page_empty.has_more is False

        # Add one event
        owner_client.execute(
            CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=CreateWorkBatchCommand(
                    type="create_work_batch",
                    payload=CreateWorkBatchPayload(
                        items=(CreateWorkInput(client_ref="p1", title="test"),)
                    ),
                ),
            )
        )

        # Paging past the event keeps after
        page_past = owner_client.events(after_sequence=100)
        assert page_past.events == ()
        assert page_past.watermark_sequence > 0
        assert page_past.next_after_sequence == 100
        assert page_past.has_more is False

        # work.candidate.read -> 403
        cand_client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "candidate_reader.json",
            transport=tc._transport,
        )
        with pytest.raises(WorkError) as exc_cand:
            cand_client.events()
        assert exc_cand.value.status == 403
        assert exc_cand.value.code == "forbidden"

        # work.read with candidate_ids -> 403
        bounded_client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "bounded_reader.json",
            transport=tc._transport,
        )
        with pytest.raises(WorkError) as exc_bounded:
            bounded_client.events()
        assert exc_bounded.value.status == 403
        assert exc_bounded.value.code == "forbidden"

        # Workspace outsider -> 403
        outsider_client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "outsider.json",
            transport=tc._transport,
        )
        with pytest.raises(WorkError) as exc_outsider:
            outsider_client.events()
        assert exc_outsider.value.status == 403
        assert exc_outsider.value.code == "forbidden"


def test_pagination_limits_and_validation(
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

        capabilities = tmp_path / "capabilities"
        capabilities.mkdir(mode=0o700, exist_ok=True)
        (capabilities / "owner.json").write_text(
            json.dumps(
                {
                    "token": "owner-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "owner",
                    "workspaces": [str(workspace_id)],
                    "scopes": ["work.read", "work.mutate"],
                }
            )
        )
        (capabilities / "owner.json").chmod(0o600)

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        client = WorkClient(
            "http://testserver",
            workspace_id,
            capabilities / "owner.json",
            transport=tc._transport,
        )

        # Create 5 events
        for i in range(5):
            client.execute(
                CommandEnvelope(
                    api_version="work.omp.dev/v1",
                    workspace_id=workspace_id,
                    operation_id=uuid4(),
                    request_id=uuid4(),
                    correlation_id=uuid4(),
                    command=CreateWorkBatchCommand(
                        type="create_work_batch",
                        payload=CreateWorkBatchPayload(
                            items=(CreateWorkInput(client_ref=f"lim-{i}", title=f"lim-{i}"),)
                        ),
                    ),
                )
            )

        # Page 1 (limit 2)
        p1 = client.events(after_sequence=0, limit=2)
        assert len(p1.events) == 2
        assert p1.has_more is True
        assert p1.next_after_sequence == p1.events[-1].sequence

        # Page 2 (limit 2)
        p2 = client.events(after_sequence=p1.next_after_sequence, limit=2)
        assert len(p2.events) == 2
        assert p2.has_more is True
        assert p2.next_after_sequence == p2.events[-1].sequence

        # Page 3 (limit 2)
        p3 = client.events(after_sequence=p2.next_after_sequence, limit=2)
        assert len(p3.events) == 1
        assert p3.has_more is False
        assert p3.next_after_sequence == p3.events[-1].sequence

        # Page 4 (empty)
        p4 = client.events(after_sequence=p3.next_after_sequence, limit=2)
        assert len(p4.events) == 0
        assert p4.has_more is False
        assert p4.next_after_sequence == p3.next_after_sequence

        # Query parameter validations (FastAPI RequestValidationError maps to 400)
        resp = tc.get(
            f"/v1/workspaces/{workspace_id}/events?after_sequence=-1",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert resp.status_code == 400

        resp = tc.get(
            f"/v1/workspaces/{workspace_id}/events?limit=0",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert resp.status_code == 400

        resp = tc.get(
            f"/v1/workspaces/{workspace_id}/events?limit=501",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert resp.status_code == 400
