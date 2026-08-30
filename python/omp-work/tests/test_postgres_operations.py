from __future__ import annotations
from dataclasses import replace
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

from omp_work.operations.artifacts import decrypt_file, encrypt_file
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority
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
    return OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data", port=_free_port())


def test_pinned_migration_set_is_forward_only() -> None:
    files = migrations()
    assert [ordinal for ordinal, _ in files] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
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


def test_latest_backup_can_select_exact_completed_id(monkeypatch: pytest.MonkeyPatch, config: OperationsConfig) -> None:
    monkeypatch.setattr(
        backup,
        "_aws",
        lambda *_: json.dumps(
            {
                "Contents": [
                    {"Key": "work-ledger/v1/base/2026/08/17/wrong/COMPLETE", "LastModified": "2026-08-17T02:00:00Z"},
                    {"Key": "work-ledger/v1/base/2026/08/17/wanted/COMPLETE", "LastModified": "2026-08-17T01:00:00Z"},
                ]
            }
        ).encode(),
    )
    prefix, _ = backup._latest_backup(config, "wanted")
    assert prefix == "work-ledger/v1/base/2026/08/17/wanted/"


def test_backup_completion_marker_uses_regular_empty_file(monkeypatch: pytest.MonkeyPatch, config: OperationsConfig) -> None:
    marker_uploads: list[tuple[bool, int]] = []
    monkeypatch.setattr(backup, "validate_bundle", lambda **kwargs: None)
    monkeypatch.setattr(backup, "_record_evidence", lambda *args, **kwargs: None)

    def fake_run(arguments: list[str], **kwargs: object) -> bytes:
        if arguments[0] == "pg_dump":
            Path(arguments[arguments.index("--file") + 1]).write_bytes(b"dump")
        elif arguments[0] == "pg_basebackup":
            physical_dir = Path(arguments[arguments.index("--pgdata") + 1])
            physical_dir.mkdir()
            (physical_dir / "base.tar.gz").write_bytes(b"physical")
        elif arguments[0] == "tar":
            Path(arguments[arguments.index("-cf") + 1]).write_bytes(b"archive")
        return b""

    def fake_encrypt(source: Path, destination: Path, passphrase: Path) -> str:
        destination.write_bytes(source.read_bytes())
        return "0" * 64

    def fake_aws(config: OperationsConfig, arguments: list[str]) -> bytes:
        if arguments[0:2] == ["s3api", "put-object"] and arguments[arguments.index("--key") + 1].endswith("/COMPLETE"):
            body = Path(arguments[arguments.index("--body") + 1])
            marker_uploads.append((body.is_file(), body.stat().st_size))
        return b"{}"

    monkeypatch.setattr(backup, "_run", fake_run)
    monkeypatch.setattr(backup, "encrypt_file", fake_encrypt)
    monkeypatch.setattr(backup, "_aws", fake_aws)

    backup.create(config)

    assert marker_uploads == [(True, 0)]


def test_restore_drill_runs_on_clean_cluster_with_bounded_socket(monkeypatch: pytest.MonkeyPatch, config: OperationsConfig) -> None:
    deep_config = replace(config, state_dir=config.state_dir / ("nested-" * 20))
    prefix = f"{config.prefix}/base/2026/08/17/backup-id/"
    startup_options: list[str] = []
    restore_arguments: list[str] = []
    monkeypatch.setattr(backup, "validate_bundle", lambda **kwargs: None)
    monkeypatch.setattr(backup, "_latest_backup", lambda *args: (prefix, {}))
    monkeypatch.setattr(backup, "_record_evidence", lambda *args, **kwargs: "receipt")

    def fake_download(config: OperationsConfig, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if key.endswith("manifest.json.gpg"):
            destination.write_text(json.dumps({
                "backup_id": "backup-id",
                "contract_sha256": backup.contract_sha256(),
                "migration_set_sha256": backup.migration_set_sha256(),
                "objects": [{"key": f"{prefix}ledger.dump.gpg", "sha256": "0" * 64}],
            }))
        else:
            destination.write_bytes(b"dump")

    def fake_run(arguments: list[str], **kwargs: object) -> bytes:
        if arguments[0] == "pg_ctl":
            startup_options.append(arguments[arguments.index("-o") + 1])
        if arguments[0] == "pg_restore":
            restore_arguments.extend(arguments)
        if arguments[0] == "psql":
            return str(len(backup.migrations())).encode()
        return b""

    monkeypatch.setattr(backup, "_download_decrypt", fake_download)
    monkeypatch.setattr(backup, "_run", fake_run)

    assert backup.restore_drill(deep_config, reason="socket-boundary", backup_id="backup-id") == "receipt"
    socket_dir = startup_options[0].split(" -k ", 1)[1].split(" ", 1)[0]
    assert len(f"{socket_dir}/.s.PGSQL.65535") < 108
    assert "--no-owner" in restore_arguments and "--no-privileges" in restore_arguments


def test_restore_drill_forwards_requested_backup_id(monkeypatch: pytest.MonkeyPatch, config: OperationsConfig) -> None:
    """A final-window restore must drill the backup it just created, not a newer upload."""
    monkeypatch.setattr(backup, "validate_bundle", lambda **kwargs: None)

    def selected(config: OperationsConfig, backup_id: str | None = None) -> tuple[str, dict[str, object]]:
        raise RuntimeError(f"selected:{backup_id}")

    monkeypatch.setattr(backup, "_latest_backup", selected)
    with pytest.raises(RuntimeError, match="selected:wanted"):
        backup.restore_drill(config, reason="pre-activation-final", backup_id="wanted")




def test_populated_v13_to_v14_upgrade_migrates_legacy_closeout_intents(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    """OMP-47 upgrade rehearsal: a v13 database with real pending+completed
    closeout_intents rows migrates to close_attempts with the legacy state
    mapping, candidate/plan backfill, and FORCE RLS restored (the migration
    lifts FORCE for its own transaction because the migrator holds no
    workspace claim)."""
    import omp_work.operations.database as database_module

    with native_postgres(config.state_dir, config.port):
        original_migrate = database_module.migrate
        monkeypatch.setattr(database_module, "validate_bundle", lambda **kw: None)
        monkeypatch.setattr(database_module, "migrate", lambda cfg, **kw: original_migrate(cfg, target=13, **kw))
        bootstrap(config)
        monkeypatch.setattr(database_module, "migrate", original_migrate)
        workspace_id = uuid4()
        pending_work, pending_revision, pending_candidate = uuid4(), uuid4(), uuid4()
        done_work, done_revision, done_candidate = uuid4(), uuid4(), uuid4()
        plan_receipt_id, pending_intent, completed_intent = uuid4(), uuid4(), uuid4()
        now = datetime.now(timezone.utc)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
            cursor = connection.cursor()
            cursor.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))
            for work_id, revision_id, candidate_id, candidate_hash, commit, title in (
                (pending_work, pending_revision, pending_candidate, "a" * 64, "b" * 40, "legacy pending"),
                (done_work, done_revision, done_candidate, "c" * 64, "d" * 40, "legacy done"),
            ):
                cursor.execute("INSERT INTO omp_work.work_items(work_id,workspace_id,state) VALUES (%s,%s,'NOW')", (work_id, workspace_id))
                cursor.execute(
                    "INSERT INTO omp_work.work_revisions(revision_id,work_id,workspace_id,revision_number,title,description,scope,content_sha256,created_by,supplied_at) VALUES (%s,%s,%s,1,%s,'','',%s,'owner',%s)",
                    (revision_id, work_id, workspace_id, title, "e" * 64, now),
                )
                cursor.execute("UPDATE omp_work.work_items SET current_revision_id=%s WHERE work_id=%s", (revision_id, work_id))
                cursor.execute(
                    "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,allocated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (candidate_id, workspace_id, work_id, revision_id, candidate_hash, commit, now),
                )
                cursor.execute("UPDATE omp_work.work_items SET current_candidate_id=%s WHERE work_id=%s", (candidate_id, work_id))
            cursor.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issued_at) VALUES (%s,%s,%s,%s,%s,'plan','{}',%s,%s)",
                (plan_receipt_id, workspace_id, pending_work, pending_revision, pending_candidate, "0" * 64, now),
            )
            cursor.execute(
                "INSERT INTO omp_evidence.closeout_intents(intent_id,workspace_id,work_id,revision_id,candidate_id,state,requested_at) VALUES (%s,%s,%s,%s,%s,'pending',%s)",
                (pending_intent, workspace_id, pending_work, pending_revision, pending_candidate, now),
            )
            cursor.execute(
                "INSERT INTO omp_evidence.closeout_intents(intent_id,workspace_id,work_id,revision_id,candidate_id,state,requested_at,completed_at) VALUES (%s,%s,%s,%s,%s,'completed',%s,%s)",
                (completed_intent, workspace_id, done_work, done_revision, done_candidate, now, now),
            )
        original_migrate(config)
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT attempt_id,state,authorization_kind,authorization_ref,candidate_sha256,candidate_commit,plan_receipt_id,closeout_requested_at FROM omp_work.close_attempts")
            rows = {str(row[0]): row for row in cursor.fetchall()}
            assert len(rows) == 2
            pending_row = rows[str(pending_intent)]
            completed_row = rows[str(completed_intent)]
            assert pending_row[1] == "closeout_requested" and pending_row[2] == "legacy"
            assert pending_row[3] == f"legacy:{pending_intent}"
            assert pending_row[4] == "a" * 64 and pending_row[5] == "b" * 40
            assert str(pending_row[6]) == str(plan_receipt_id) and pending_row[7] is not None
            assert completed_row[1] == "completed" and completed_row[2] == "legacy" and completed_row[4] == "c" * 64
            cursor.execute(
                "SELECT relname, relforcerowsecurity FROM pg_class WHERE oid IN ('omp_work.close_attempts'::regclass,'omp_work.work_items'::regclass,'omp_work.candidates'::regclass,'omp_evidence.receipts'::regclass,'omp_work.audit_manifests'::regclass)"
            )
            assert all(force for _, force in cursor.fetchall()), "FORCE RLS restored on every table the migration touched"
            cursor.execute("SELECT count(*) FROM omp_work.audit_manifests")
            assert cursor.fetchone()[0] == 0


def test_bootstrap_migrates_pinned_postgres(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
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
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute("SELECT state FROM omp_integration.raw_exports WHERE export_id=%s", (base_export_id,))
                assert cursor.fetchone() == ("complete",)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_integration.raw_exports SET state='blocked' WHERE export_id=%s", (base_export_id,))

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



def test_store_creates_and_replays_batch(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        workspace_id, operation_id, actor_id = uuid4(), uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
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


def test_authorization_uses_security_and_constraints(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        workspace_id = uuid4()
        other_workspace_id = uuid4()
        actor_id = config.actor_id()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))
                cursor.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (other_workspace_id,))
                cursor.execute("SELECT relforcerowsecurity FROM pg_class WHERE oid = 'omp_work.authorization_uses'::regclass")
                assert cursor.fetchone()[0] is True, "FORCE RLS must be enabled on authorization_uses"

        # 2. Setup a valid work item, candidate, plan receipt, attempt, and event
        work_id = uuid4()
        revision_id = uuid4()
        candidate_id = uuid4()
        plan_receipt_id = uuid4()
        attempt_id = uuid4()
        event_id = uuid4()
        app_kwargs = config.connection_kwargs("omp_work_app")

        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute("INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'OPEN')", (work_id, workspace_id))
                cursor.execute("INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at) VALUES (%s, %s, %s, 1, 'T', 'D', 'S', %s, 'test', clock_timestamp())", (revision_id, work_id, workspace_id, "0" * 64))
                cursor.execute("UPDATE omp_work.work_items SET current_revision_id = %s WHERE work_id = %s", (revision_id, work_id))
                cursor.execute("INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, allocated_at) VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp())", (candidate_id, workspace_id, work_id, revision_id, "a" * 64, "b" * 40))
                cursor.execute("INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, artifact_sha256, issuer, issued_at) VALUES (%s, %s, %s, %s, %s, 'plan', '{}', %s, %s, 'test', clock_timestamp())", (plan_receipt_id, workspace_id, work_id, revision_id, candidate_id, "c" * 64, "d" * 64))
                cursor.execute(
                    "INSERT INTO omp_work.close_attempts(attempt_id, workspace_id, work_id, revision_id, candidate_id, plan_receipt_id, candidate_sha256, candidate_commit, owner_session_id, owner_session_started_at, owner_session_start_commit, repository, diff_sha256, starting_dirty_paths, authorization_kind, authorization_ref, state)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 's1', clock_timestamp(), %s, 'repo', %s, ARRAY[]::text[], 'summary', 'token-1', 'active')",
                    (attempt_id, workspace_id, work_id, revision_id, candidate_id, plan_receipt_id, "a" * 64, "b" * 40, "b" * 40, "e" * 64),
                )
                cursor.execute(
                    "INSERT INTO omp_work.close_attempt_events(event_id, workspace_id, work_id, attempt_id, event_type, reason_code, reason, legal_next_actions, remaining_launches, remaining_reports, requires_fresh_authorization, rendered_text, rendered_sha256, requires_delivery)"
                    " VALUES (%s, %s, %s, %s, 'attempt_begun', 'attempt_begun', 'begun', ARRAY['action'], 3, 2, false, 'rendered', %s, true)",
                    (event_id, workspace_id, work_id, attempt_id, "f" * 64),
                )

        # 3. App role can INSERT into authorization_uses
        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute(
                    "INSERT INTO omp_work.authorization_uses(workspace_id, authorization_ref, use_kind, attempt_id, identity_sha256, owner_session_id, outcome, event_id)"
                    " VALUES (%s, 'token-1', 'begin', %s, %s, 's1', '{\"status\":\"applied\"}'::jsonb, %s)",
                    (workspace_id, attempt_id, "0" * 64, event_id),
                )

        # 4. Primary key enforcement on (workspace_id, authorization_ref)
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.errors.UniqueViolation):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute(
                        "INSERT INTO omp_work.authorization_uses(workspace_id, authorization_ref, use_kind, attempt_id, identity_sha256, owner_session_id, outcome, event_id)"
                        " VALUES (%s, 'token-1', 'resume', %s, %s, 's1', '{\"status\":\"applied\"}'::jsonb, %s)",
                        (workspace_id, attempt_id, "0" * 64, event_id),
                    )

        # 5. Foreign keys reject orphan attempt_id or event_id
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute(
                        "INSERT INTO omp_work.authorization_uses(workspace_id, authorization_ref, use_kind, attempt_id, identity_sha256, owner_session_id, outcome, event_id)"
                        " VALUES (%s, 'token-orphan', 'begin', %s, %s, 's1', '{\"status\":\"applied\"}'::jsonb, %s)",
                        (workspace_id, uuid4(), "0" * 64, event_id),
                    )

        # 6. Immutability trigger rejects UPDATE and DELETE
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with pytest.raises(psycopg.Error, match="immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("UPDATE omp_work.authorization_uses SET use_kind = 'resume' WHERE authorization_ref = 'token-1'")
            with pytest.raises(psycopg.Error, match="immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM omp_work.authorization_uses WHERE authorization_ref = 'token-1'")

        # 7. App role privilege revocation: UPDATE, DELETE, TRUNCATE refused
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("TRUNCATE TABLE omp_work.authorization_uses")

        # 8. Workspace isolation
        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(other_workspace_id), str(actor_id)))
                cursor.execute("SELECT count(*) FROM omp_work.authorization_uses WHERE authorization_ref = 'token-1'")
                assert cursor.fetchone()[0] == 0


def test_execution_grant_triggers_enforce_immutability_and_monotonicity(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        workspace_id = uuid4()
        actor_id = config.actor_id()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))
        work_id = uuid4()
        rev_id = uuid4()
        grant_id = uuid4()
        item_id = uuid4()
        now = datetime.now(timezone.utc)
        app_kwargs = config.connection_kwargs("omp_work_app")

        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute("INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'OPEN')", (work_id, workspace_id))
                cursor.execute(
                    "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at)"
                    " VALUES (%s, %s, %s, 1, 'T', 'D', 'S', %s, 'test', clock_timestamp())",
                    (rev_id, work_id, workspace_id, "0" * 64),
                )
                cursor.execute("UPDATE omp_work.work_items SET current_revision_id = %s WHERE work_id = %s", (rev_id, work_id))
                cursor.execute(
                    "INSERT INTO omp_work.execution_grants(grant_id, workspace_id, owner_id, repository, remote_ref, mode, max_continuations, max_close_attempts, max_no_progress, authorization_hash, provenance, judge_sha256, judge_manifest, focus_version_at_grant, created_at, expires_at, state)"
                    " VALUES (%s, %s, %s, 'oh-my-pi', 'refs/heads/main', 'single', 8, 3, 3, %s, '{}'::jsonb, %s, '{}'::jsonb, 0, %s, %s + interval '7 days', 'active')",
                    (grant_id, workspace_id, actor_id, "a" * 64, "b" * 64, now, now),
                )
                cursor.execute(
                    "INSERT INTO omp_work.execution_grant_items(item_id, workspace_id, grant_id, work_id, position, claimed_revision_id, initial_git_baseline, original_request, original_request_sha256, phase, activated_at)"
                    " VALUES (%s, %s, %s, %s, 0, %s, %s, 'req', %s, 'planning', %s)",
                    (item_id, workspace_id, grant_id, work_id, rev_id, "c" * 40, "d" * 64, now),
                )

        # 1. DELETE rejected on grants and items via trigger
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with pytest.raises(psycopg.Error, match="execution grants are immutable history"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM omp_work.execution_grants WHERE grant_id = %s", (grant_id,))
            with pytest.raises(psycopg.Error, match="execution grant items are immutable history"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("DELETE FROM omp_work.execution_grant_items WHERE item_id = %s", (item_id,))

        # 2. Immutable grant identity & admission columns
        for col, val in [
            ("grant_id", f"'{uuid4()}'"),
            ("workspace_id", f"'{uuid4()}'"),
            ("owner_id", f"'{uuid4()}'"),
            ("repository", "'other-repo'"),
            ("remote_ref", "'refs/heads/other'"),
            ("mode", "'queue'"),
            ("max_continuations", "10"),
            ("max_close_attempts", "5"),
            ("max_no_progress", "5"),
            ("authorization_hash", f"'{'f' * 64}'"),
            ("provenance", "'{\"tampered\": true}'::jsonb"),
            ("judge_sha256", f"'{'e' * 64}'"),
            ("judge_manifest", "'{\"tampered\": true}'::jsonb"),
            ("focus_version_at_grant", "5"),
            ("created_at", "clock_timestamp()"),
        ]:
            with psycopg.connect(**app_kwargs) as connection:
                with pytest.raises(psycopg.Error, match="identity and admission parameters are immutable"):
                    with connection.transaction(), connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                        cursor.execute(f"UPDATE omp_work.execution_grants SET {col} = {val} WHERE grant_id = %s", (grant_id,))

        # 3. Monotonic grant counters
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.Error, match="grant_version must be monotonic"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_work.execution_grants SET grant_version = -1 WHERE grant_id = %s", (grant_id,))
            with pytest.raises(psycopg.Error, match="continuations_scheduled must be monotonic"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_work.execution_grants SET continuations_scheduled = -1 WHERE grant_id = %s", (grant_id,))

        # 4. Immutable item identity & admission columns
        for col, val in [
            ("item_id", f"'{uuid4()}'"),
            ("workspace_id", f"'{uuid4()}'"),
            ("grant_id", f"'{uuid4()}'"),
            ("work_id", f"'{uuid4()}'"),
            ("position", "1"),
            ("claimed_revision_id", f"'{uuid4()}'"),
            ("initial_git_baseline", f"'{'0' * 40}'"),
            ("original_request", "'tampered'"),
            ("original_request_sha256", f"'{'1' * 64}'"),
        ]:
            with psycopg.connect(**app_kwargs) as connection:
                with pytest.raises(psycopg.Error, match="identity and admission claims are immutable"):
                    with connection.transaction(), connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                        cursor.execute(f"UPDATE omp_work.execution_grant_items SET {col} = {val} WHERE item_id = %s", (item_id,))

        # 5. Legal transitions and write-once terminal timestamps
        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute(
                    "UPDATE omp_work.execution_grants SET state='paused', paused_at=clock_timestamp(), grant_version=grant_version+1 WHERE grant_id = %s",
                    (grant_id,),
                )
                cursor.execute(
                    "UPDATE omp_work.execution_grants SET state='active', paused_at=NULL, continuations_scheduled=continuations_scheduled+1, grant_version=grant_version+1 WHERE grant_id = %s",
                    (grant_id,),
                )
                cursor.execute(
                    "UPDATE omp_work.execution_grants SET state='stopped', terminal_reason='stopped', stopped_at=clock_timestamp(), grant_version=grant_version+1 WHERE grant_id = %s",
                    (grant_id,),
                )

        # 6. Terminal state immutability
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.Error, match="terminal execution grant is immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_work.execution_grants SET state='active', grant_version=grant_version+1 WHERE grant_id = %s", (grant_id,))

        # 7. remote_ref CHECK constraint rejects forbidden patterns
        forbidden_refs = [
            "refs/heads/",
            "refs/heads/a..b",
            "refs/heads/x@{y",
            "refs/heads/a.lock",
            "refs/heads/a.lock/b",
            "refs/heads/a/",
            "refs/heads/a.",
            "refs/heads/a./b",
            "refs/heads/.a",
            "refs/heads/a//b",
            "refs/heads/a b",
            "refs/heads/a^b",
            "refs/heads/a~b",
            "refs/heads/a:b",
            "refs/heads/a?b",
            "refs/heads/a*b",
            "refs/heads/a[b",
            "refs/heads/a\\b",
            "refs/heads/@",
            "refs/tags/v1.0",
            "HEAD",
        ]
        for bad_ref in forbidden_refs:
            with psycopg.connect(**app_kwargs) as connection:
                with pytest.raises(psycopg.Error, match="execution_grants_remote_ref_check"):
                    with connection.transaction(), connection.cursor() as cursor:
                        cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                        bad_grant_id = uuid4()
                        cursor.execute(
                            "INSERT INTO omp_work.execution_grants(grant_id, workspace_id, owner_id, repository, remote_ref, mode, max_continuations, max_close_attempts, max_no_progress, authorization_hash, provenance, judge_sha256, judge_manifest, focus_version_at_grant, created_at, expires_at, state)"
                            " VALUES (%s, %s, %s, 'oh-my-pi', %s, 'single', 8, 3, 3, %s, '{}'::jsonb, %s, '{}'::jsonb, 0, %s, %s + interval '7 days', 'active')",
                            (bad_grant_id, workspace_id, actor_id, bad_ref, "a" * 64, "b" * 64, now, now),
                        )


def test_ledger_immutability_and_role_privilege_enforcement(config: OperationsConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    with native_postgres(config.state_dir, config.port):
        bootstrap(config)
        workspace_id = uuid4()
        actor_id = config.actor_id()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))

        work_id = uuid4()
        revision_id = uuid4()
        candidate_id = uuid4()
        receipt_id = uuid4()
        criterion_id = uuid4()
        now = datetime.now(timezone.utc)
        app_kwargs = config.connection_kwargs("omp_work_app")
        readonly_kwargs = config.connection_kwargs("omp_work_readonly")
        importer_kwargs = config.connection_kwargs("omp_work_importer")
        postgres_kwargs = config.connection_kwargs("postgres")

        # 1. Populate valid ledger items via app role
        with psycopg.connect(**app_kwargs) as connection:
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                cursor.execute("INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'OPEN')", (work_id, workspace_id))
                cursor.execute(
                    "INSERT INTO omp_work.work_revisions(revision_id, work_id, workspace_id, revision_number, title, description, scope, content_sha256, created_by, supplied_at)"
                    " VALUES (%s, %s, %s, 1, 'T', 'D', 'S', %s, 'test', %s)",
                    (revision_id, work_id, workspace_id, "0" * 64, now),
                )
                cursor.execute(
                    "INSERT INTO omp_work.acceptance_criteria(revision_id, workspace_id, position, criterion)"
                    " VALUES (%s, %s, 0, 'Criterion 1')",
                    (revision_id, workspace_id),
                )
                cursor.execute(
                    "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, allocated_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (candidate_id, workspace_id, work_id, revision_id, "a" * 64, "b" * 40, now),
                )
                cursor.execute(
                    "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, artifact_sha256, issuer, issued_at)"
                    " VALUES (%s, %s, %s, %s, %s, 'plan', '{}', %s, %s, 'test', %s)",
                    (receipt_id, workspace_id, work_id, revision_id, candidate_id, "c" * 64, "d" * 64, now),
                )
                cursor.execute(
                    "INSERT INTO omp_work.work_aliases(work_id, workspace_id, key, origin)"
                    " VALUES (%s, %s, 'OMP-100', 'local')",
                    (work_id, workspace_id),
                )
                event_id = uuid4()
                cursor.execute(
                    "INSERT INTO omp_audit.domain_events(event_id, workspace_id, aggregate_type, aggregate_id, aggregate_version, actor_id, actor_kind, capability_id, request_id, correlation_id, causation_id, operation_id, event_type, outcome, payload, payload_sha256, previous_event_sha256, event_sha256, occurred_at)"
                    " VALUES (%s, %s, 'work_item', %s, 1, %s, 'owner', %s, %s, %s, %s, %s, 'work_created', 'success', '{}'::jsonb, %s, NULL, %s, %s)",
                    (event_id, workspace_id, work_id, actor_id, uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), "e" * 64, "f" * 64, now),
                )

        # 2. Trigger enforcement: direct UPDATE and DELETE rejected as immutable
        for table, where in [
            ("omp_work.candidates", f"candidate_id = '{candidate_id}'"),
            ("omp_evidence.receipts", f"receipt_id = '{receipt_id}'"),
            ("omp_work.acceptance_criteria", f"revision_id = '{revision_id}' AND position = 0"),
            ("omp_work.work_revisions", f"revision_id = '{revision_id}'"),
            ("omp_work.work_aliases", f"work_id = '{work_id}'"),
            ("omp_audit.domain_events", f"event_id = '{event_id}'"),
        ]:
            with psycopg.connect(**postgres_kwargs) as connection:
                with pytest.raises(psycopg.Error, match="immutable"):
                    with connection.transaction(), connection.cursor() as cursor:
                        cursor.execute(f"UPDATE {table} SET workspace_id = '{uuid4()}' WHERE {where}")
                with pytest.raises(psycopg.Error, match="immutable"):
                    with connection.transaction(), connection.cursor() as cursor:
                        cursor.execute(f"DELETE FROM {table} WHERE {where}")

        # 3. Role privilege enforcement: omp_work_app cannot DELETE or TRUNCATE
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("DELETE FROM omp_work.candidates")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("DELETE FROM omp_evidence.receipts")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("TRUNCATE TABLE omp_work.work_items")

        # 4. Role privilege enforcement: omp_work_readonly cannot INSERT or UPDATE
        with psycopg.connect(**readonly_kwargs) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'OPEN')", (uuid4(), workspace_id))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_work.work_items SET state = 'CLOSED'")

        # 5. Role privilege enforcement: omp_work_importer cannot INSERT into candidates or receipts
        with psycopg.connect(**importer_kwargs) as connection:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, allocated_at) VALUES (%s, %s, %s, %s, %s, %s, %s)", (uuid4(), workspace_id, work_id, revision_id, "0" * 64, "0" * 40, now))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issuer, issued_at) VALUES (%s, %s, %s, %s, %s, 'plan', '{}', %s, 'test', %s)", (uuid4(), workspace_id, work_id, revision_id, candidate_id, "0" * 64, now))

        # 6. Provenance immutability trigger enforcement
        with psycopg.connect(**app_kwargs) as connection:
            with pytest.raises(psycopg.Error, match="provenance is immutable"):
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)", (str(workspace_id), str(actor_id)))
                    cursor.execute("UPDATE omp_work.work_items SET provenance = '{\"tampered\": true}'::jsonb WHERE work_id = %s", (work_id,))