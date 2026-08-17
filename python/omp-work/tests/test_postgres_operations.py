from __future__ import annotations
from datetime import datetime, timezone
import json

import os
from pathlib import Path
import secrets
import socket
import subprocess
import re
from uuid import UUID, uuid4

import psycopg

import httpx
import pytest

import omp_work.integration.exporter as exporter_module
from omp_work.integration.exporter import LinearExporter
from omp_work.operations.artifacts import decrypt_file, encrypt_file
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres
from omp_work.operations import backup
from omp_work.operations.database import bootstrap, collect_health, migration_set_sha256, migrations
from omp_work.v1.models import CommandEnvelope, CreateWorkBatchCommand, CreateWorkBatchPayload, CreateWorkInput
from omp_work.v1.store import PostgresWorkStore, WorkStoreError

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup", "gpg-passphrase", "operator-actor-id"):
        path = credentials / role
        path.write_text(str(uuid4()) if role == "operator-actor-id" else secrets.token_urlsafe(24))
        path.chmod(0o600)
    linear = credentials / "linear-export.json"
    linear.write_text(json.dumps({"kind": "oauth", "access_token": "read-only-token", "scopes": ["read"], "expires_at": "2099-01-01T00:00:00Z"}))
    linear.chmod(0o600)
    return OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data", port=_free_port())


def test_pinned_migration_set_is_forward_only() -> None:
    files = migrations()
    assert [ordinal for ordinal, _ in files] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert all(path.name.startswith(f"{ordinal:04d}_") for ordinal, path in files)
    assert len(migration_set_sha256()) == 64


def test_latest_backup_uses_completion_timestamp(monkeypatch: pytest.MonkeyPatch, config: OperationsConfig) -> None:
    monkeypatch.setattr(
        backup,
        "_aws",
        lambda *_: json.dumps(
            {
                "Contents": [
                    {"Key": "work-ledger/v1/base/z/older/COMPLETE", "LastModified": "2026-08-01T00:00:00Z"},
                    {"Key": "work-ledger/v1/base/a/newer/COMPLETE", "LastModified": "2026-08-02T00:00:00Z"},
                ]
            }
        ).encode(),
    )
    prefix, _ = backup._latest_backup(config)
    assert prefix == "work-ledger/v1/base/a/newer/"


class _LinearFixture:
    def __init__(self, variant: str = "complete", *, interrupt_second_team_page: bool = False) -> None:
        self.variant = variant
        self.interrupt_second_team_page = interrupt_second_team_page
        self.interrupted = False
        self.calls: list[tuple[str, str | None]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        operation_match = re.match(r"query (\w+)", payload["query"])
        assert operation_match is not None
        operation = operation_match.group(1)
        after = payload["variables"]["after"]
        self.calls.append((operation, after))
        if self.interrupt_second_team_page and operation == "teams":
            if after is None:
                return httpx.Response(200, json={"data": {operation: {"nodes": self.nodes(operation), "pageInfo": {"hasNextPage": True, "endCursor": "saved-cursor"}}}})
            if not self.interrupted:
                self.interrupted = True
                raise httpx.ConnectError("interrupted", request=request)
        return httpx.Response(200, json={"data": {operation: {"nodes": self.nodes(operation), "pageInfo": {"hasNextPage": False, "endCursor": None}}}})

    def nodes(self, operation: str) -> list[dict[str, object]]:
        issue_a = "00000000-0000-7000-8000-000000000145"
        issue_b = "00000000-0000-7000-8000-000000000146"
        issues = [{"id": issue_a, "identifier": "HOME-145", "title": "Exporter", "updatedAt": "2026-08-01T00:00:00Z", "team": {"key": "HOME"}}]
        if self.variant == "blocked":
            issues.append({"id": issue_b, "identifier": "HOME-145", "title": "Duplicate", "updatedAt": "2026-08-01T00:00:00Z", "team": {"key": "HOME"}})
        values: dict[str, list[dict[str, object]]] = {
            "teams": [{"id": "team-home", "key": "HOME", "name": "Home", "updatedAt": "2026-08-01T00:00:00Z"}],
            "initiatives": [],
            "projects": [],
            "projectUpdates": [],
            "projectMilestones": [],
            "issues": issues,
            "workflowStates": [],
            "issueLabels": [],
            "initiativeToProjects": [],
            "issueRelations": [],
            "comments": [],
            "attachments": (
                [{"id": "attachment-1", "title": "Unavailable", "updatedAt": "2026-08-01T00:00:00Z", "issue": {"id": issue_a, "identifier": "HOME-145", "team": {"key": "HOME"}}}]
                if self.variant == "quarantined"
                else []
            ),
        }
        return values[operation]


def test_bootstrap_migrates_pinned_postgres(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
            connection.execute("ALTER DATABASE omp_work SET timezone TO 'America/Detroit'")
        report = collect_health(config)
        assert report.live and report.ready
        assert report.postgres["major"] == 18
        assert report.migration["pending"] == []
        assert report.migration["drift"] == []
        assert report.wal["current_lsn"]
        assert report.wal["bytes_since_init"] >= 0
        assert report.capacity["database_bytes"] > 0

        workspace_id = uuid4()
        actor_id = config.actor_id()
        base_export_id = uuid4()
        boundary = datetime.now(timezone.utc)
        importer_kwargs = config.connection_kwargs("omp_work_importer")
        with psycopg.connect(**importer_kwargs) as connection:
            with pytest.raises(psycopg.Error, match="workspace claim required"):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT count(*) FROM omp_integration.raw_exports")
                    cursor.fetchone()
        with psycopg.connect(**importer_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute(
                    "INSERT INTO omp_integration.raw_exports (export_id,workspace_id,team_key,mode,source_started_at,state,storage_root) VALUES (%s,%s,'HOME','full',%s,'running','linear-exports/manual')",
                    (base_export_id, workspace_id, boundary),
                )
                cursor.execute("UPDATE omp_integration.raw_exports SET source_boundary=%s WHERE export_id=%s", (boundary, base_export_id))
        with psycopg.connect(**importer_kwargs) as connection:
            with pytest.raises(psycopg.Error, match="immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_integration.raw_exports SET source_boundary=%s WHERE export_id=%s", (boundary, base_export_id))
        with psycopg.connect(**importer_kwargs) as connection:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute(
                        "INSERT INTO omp_integration.raw_exports (export_id,workspace_id,team_key,mode,base_export_id,source_started_at,source_lower_bound,state,storage_root) VALUES (%s,%s,'HOME','delta',%s,%s,%s,'running','linear-exports/missing-base')",
                        (uuid4(), workspace_id, uuid4(), boundary, boundary),
                    )
        with psycopg.connect(**importer_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute(
                    "INSERT INTO omp_integration.extraction_cursors (export_id,workspace_id,stream,page_index,request_cursor,end_cursor,has_next_page,scanned_count,retained_count,cumulative_count,plaintext_sha256,ciphertext_sha256,artifact_path,variables_sha256) VALUES (%s,%s,'baseline:teams',0,NULL,NULL,false,1,1,1,%s,%s,'linear-exports/manual/page.json.gpg',%s)",
                    (base_export_id, workspace_id, "a" * 64, "b" * 64, "c" * 64),
                )
                cursor.execute(
                    "UPDATE omp_integration.raw_exports SET raw_export_sha256=%s,manifest_sha256=%s,state='complete',completed_at=clock_timestamp() WHERE export_id=%s",
                    ("d" * 64, "e" * 64, base_export_id),
                )
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with pytest.raises(psycopg.Error, match="immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM omp_integration.extraction_cursors WHERE export_id=%s", (base_export_id,))
            with pytest.raises(psycopg.Error, match="raw export is immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM omp_integration.raw_exports WHERE export_id=%s", (base_export_id,))
        for role in ("omp_work_readonly", "omp_work_backup"):
            with psycopg.connect(**config.connection_kwargs(role)) as connection:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("SELECT state FROM omp_integration.raw_exports WHERE export_id=%s", (base_export_id,))
                    assert cursor.fetchone() == ("complete",)
        with psycopg.connect(**config.connection_kwargs("omp_work_app")) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("SELECT state FROM omp_integration.raw_exports WHERE export_id=%s", (base_export_id,))

        artifact_dir = config.data_dir / "artifact-check"
        artifact_dir.mkdir(parents=True, mode=0o700)
        source = artifact_dir / "source.json"
        source.write_text('{"secret":true}')
        source.chmod(0o600)
        encrypted = artifact_dir / "source.json.gpg"
        encrypt_file(source, encrypted, config.secret_path("gpg-passphrase"), mode=0o400)
        source.unlink()
        assert encrypted.stat().st_mode & 0o777 == 0o400
        restored = artifact_dir / "restored.json"
        decrypt_file(encrypted, restored, config.secret_path("gpg-passphrase"))
        assert restored.read_text() == '{"secret":true}'
        assert restored.stat().st_mode & 0o777 == 0o600
        with pytest.raises(FileExistsError):
            encrypt_file(restored, encrypted, config.secret_path("gpg-passphrase"))

        original_commit = exporter_module.ExportLedger.commit
        fail_once = True

        def interrupted_commit(self: exporter_module.ExportLedger, *args: object, **kwargs: object) -> None:
            nonlocal fail_once
            if fail_once:
                fail_once = False
                raise RuntimeError("simulated_database_failure")
            original_commit(self, *args, **kwargs)

        monkeypatch.setattr(exporter_module.ExportLedger, "commit", interrupted_commit)
        crash_workspace = uuid4()
        crash_fixture = _LinearFixture()
        crash_exporter = LinearExporter(config, transport=httpx.MockTransport(crash_fixture.handler))
        with pytest.raises(RuntimeError, match="simulated_database_failure"):
            crash_exporter.full(crash_workspace)
        monkeypatch.setattr(exporter_module.ExportLedger, "commit", original_commit)
        crash_export_id = next(path.name for path in (config.data_dir / "linear-exports" / str(crash_workspace)).iterdir())
        crash_root = config.data_dir / "linear-exports" / str(crash_workspace) / crash_export_id
        first_page = next(crash_root.iterdir())
        first_ciphertext = first_page.read_bytes()
        resumed = crash_exporter.resume(UUID(crash_export_id))
        assert first_page.read_bytes() == first_ciphertext
        assert resumed.source_hashes.work_items["00000000-0000-7000-8000-000000000145"].key == "HOME-145"

        cursor_workspace = uuid4()
        cursor_fixture = _LinearFixture(interrupt_second_team_page=True)
        cursor_exporter = LinearExporter(config, transport=httpx.MockTransport(cursor_fixture.handler))
        with pytest.raises(RuntimeError, match="linear_transport_failed"):
            cursor_exporter.full(cursor_workspace)
        cursor_export_id = UUID(next(path.name for path in (config.data_dir / "linear-exports" / str(cursor_workspace)).iterdir()))
        cursor_exporter.resume(cursor_export_id)
        assert [after for operation, after in cursor_fixture.calls if operation == "teams"][:3] == [None, "saved-cursor", "saved-cursor"]

        blocked = LinearExporter(config, transport=httpx.MockTransport(_LinearFixture("blocked").handler)).full(uuid4())
        quarantined = LinearExporter(config, transport=httpx.MockTransport(_LinearFixture("quarantined").handler)).full(uuid4())
        with psycopg.connect(**config.connection_kwargs("omp_work_importer")) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                for manifest, state in ((blocked, "blocked"), (quarantined, "complete")):
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(manifest.workspace_id), str(actor_id)))
                    cursor.execute("SELECT state FROM omp_integration.raw_exports WHERE export_id=%s", (manifest.export_id,))
                    assert cursor.fetchone() == (state,)
        assert any(anomaly.disposition == "blocking" for anomaly in blocked.anomalies)
        assert quarantined.anomalies == (exporter_module.Anomaly(code="attachment_content_unavailable", disposition="quarantined"),)
        for manifest in (resumed, blocked, quarantined):
            root = config.data_dir / "linear-exports" / str(manifest.workspace_id) / str(manifest.export_id)
            assert root.stat().st_mode & 0o777 == 0o700
            assert all(path.suffix == ".gpg" and path.stat().st_mode & 0o777 == 0o400 for path in root.iterdir())
            assert not (config.state_dir / "staging" / str(manifest.export_id)).exists()

        config.state_dir.mkdir(exist_ok=True)
        dump = config.state_dir / "backup-role.dump"
        dump_result = subprocess.run(
            [
                "pg_dump",
                "--host",
                config.host,
                "--port",
                str(config.port),
                "--username",
                "omp_work_backup",
                "--format=custom",
                "--file",
                str(dump),
                config.database,
            ],
            capture_output=True,
            env=os.environ | {"PGPASSWORD": config.read_secret("omp_work_backup")},
        )
        assert dump_result.returncode == 0, dump_result.stderr.decode()
        assert dump.stat().st_size > 0
        assert {"BACKUP_MISSING", "RESTORE_DRILL_MISSING"} <= set(report.alerts)



def test_store_creates_and_replays_batch(config: OperationsConfig) -> None:
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        workspace_id, operation_id, actor_id = uuid4(), uuid4(), uuid4()
        command = CreateWorkBatchCommand(type="create_work_batch", payload=CreateWorkBatchPayload(items=(CreateWorkInput(client_ref="a", title=" first "), CreateWorkInput(client_ref="b", title="second"))))
        first = CommandEnvelope(api_version="work.omp.dev/v1", workspace_id=workspace_id, operation_id=operation_id, request_id=uuid4(), correlation_id=uuid4(), command=command)
        store = PostgresWorkStore(config)
        receipt, result = store.execute(first, actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        replay = first.model_copy(update={"request_id": uuid4()})
        replay_receipt, replay_result = store.execute(replay, actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        assert receipt.state.value == "applied" and replay_receipt.state.value == "replayed"
        assert result == replay_result and [item["key"] for item in result["items"]] == ["OMP-1", "OMP-2"]
        conflicting = first.model_copy(update={"command": CreateWorkBatchCommand(type="create_work_batch", payload=CreateWorkBatchPayload(items=(CreateWorkInput(client_ref="c", title="different"),)))})
        with pytest.raises(WorkStoreError, match="idempotency_conflict"):
            store.execute(conflicting, actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        with psycopg.connect(**config.connection_kwargs("omp_work_app")) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute("SELECT conflict_count FROM omp_control.idempotent_commands WHERE workspace_id=%s AND operation_id=%s", (workspace_id, operation_id))
                assert cursor.fetchone() == (1,)