"""HOME-148: cutover authority — pre-activation fence, exact activation, replay, staleness, and epoch transitions."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import socket
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work import CONTRACT_VERSION, contract_sha256
from omp_work.integration.importer import TRANSFORMATION_VERSION
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap, migration_set_sha256
from omp_work.operations.fingerprints import code_fingerprint, config_fingerprint, transform_sha256
from omp_work.v1.models import (
    ActivateCutoverCommand,
    ActivateCutoverPayload,
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    CutoverManifest,
    ReconciliationCounts,
    ReconciliationHashes,
)
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from pg_native import native_postgres, seed_authority

pytestmark = pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")

BOUNDARY = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
RAW_HASH = "a" * 64
BACKUP_RECEIPT = "b" * 64
RESTORE_RECEIPT = "c" * 64
PARITY_GROUPS = {"work_items": "d" * 64, "states": "e" * 64}


def _config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup", "gpg-passphrase", "operator-actor-id"):
        path = credentials / role
        path.write_text(str(uuid4()) if role == "operator-actor-id" else secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data", port=port)


def _manifest(config: OperationsConfig, workspace_id: UUID, batch_id: UUID, epoch_id: UUID, **updates: object) -> CutoverManifest:
    data: dict[str, object] = {
        "epoch_id": epoch_id,
        "contract_version": CONTRACT_VERSION,
        "contract_sha256": contract_sha256(),
        "schema_sha256": migration_set_sha256(),
        "transform_version": TRANSFORMATION_VERSION,
        "transform_sha256": transform_sha256(),
        "source_boundary": BOUNDARY.isoformat(),
        "source_watermark": BOUNDARY.isoformat(),
        "raw_export_sha256": RAW_HASH,
        "import_batch_id": batch_id,
        "dimension_counts": {d: 0 for d in ReconciliationCounts.model_fields},
        "dimension_hashes": {dimension: "0" * 64 for dimension in ReconciliationHashes.model_fields},
        "parity_groups": PARITY_GROUPS,
        "anomalies": [],
        "parity_differences": [],
        "backup_receipt_sha256": BACKUP_RECEIPT,
        "restore_receipt_sha256": RESTORE_RECEIPT,
        "command_smoke_results": [{"command_type": "create_work_batch", "passed": True}],
        "code_fingerprint": code_fingerprint(),
        "config_fingerprint": config_fingerprint(config),
        "freeze_at": BOUNDARY.isoformat(),
        "actor": "operator",
    }
    data.update(updates)
    return CutoverManifest.model_validate(data)


def _seed_candidate(config: OperationsConfig, workspace_id: UUID, batch_id: UUID, export_id: UUID) -> None:
    with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
        connection.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(config.actor_id())))
        connection.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (workspace_id,))
        connection.execute(
            "INSERT INTO omp_integration.raw_exports (export_id,workspace_id,team_key,mode,source_started_at,source_boundary,state,storage_root,raw_export_sha256,manifest_sha256,completed_at) VALUES (%s,%s,'HOME','full',%s,%s,'complete','linear-exports/test',%s,%s,%s)",
            (export_id, workspace_id, BOUNDARY, BOUNDARY, RAW_HASH, "f" * 64, BOUNDARY),
        )
        connection.execute(
            "INSERT INTO omp_integration.import_batches (batch_id,workspace_id,export_id,transformation_version,mapping_file_sha256,state,reconciliation_sha256,parity_hashes,artifact_root,staged_at,reconciled_at,promoted_at) VALUES (%s,%s,%s,%s,%s,'promoted',%s,%s::jsonb,'linear-imports/test',%s,%s,%s)",
            (
                batch_id,
                workspace_id,
                export_id,
                TRANSFORMATION_VERSION,
                "1" * 64,
                "2" * 64,
                json.dumps({"dimension_counts": {d: 0 for d in ReconciliationCounts.model_fields}, "dimension_hashes": {d: "0" * 64 for d in ReconciliationHashes.model_fields}, "parity_groups": PARITY_GROUPS}),
                BOUNDARY,
                BOUNDARY,
                BOUNDARY,
            ),
        )
        for kind, outcome, receipt in (("backup", "passed", BACKUP_RECEIPT), ("restore_drill", "passed:manual", RESTORE_RECEIPT)):
            connection.execute(
                "INSERT INTO omp_control.operations_evidence (kind, started_at, contract_sha256, migration_set_sha256, outcome, receipt_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
                (kind, BOUNDARY, contract_sha256(), migration_set_sha256(), outcome, receipt),
            )


def _envelope(workspace_id: UUID, command: object, operation_id: UUID | None = None) -> CommandEnvelope:
    return CommandEnvelope(api_version="work.omp.dev/v1", workspace_id=workspace_id, operation_id=operation_id or uuid4(), request_id=uuid4(), correlation_id=uuid4(), command=command)


def _activate_envelope(workspace_id: UUID, manifest: CutoverManifest, operation_id: UUID | None = None) -> CommandEnvelope:
    return _envelope(workspace_id, ActivateCutoverCommand(type="activate_cutover", payload=ActivateCutoverPayload(manifest=manifest)), operation_id)


def _create_envelope(workspace_id: UUID, operation_id: UUID | None = None) -> CommandEnvelope:
    return _envelope(workspace_id, CreateWorkBatchCommand(type="create_work_batch", payload=CreateWorkBatchPayload(items=(CreateWorkInput(client_ref="a", title="first"),))), operation_id)


def _activate(store: PostgresWorkStore, workspace_id: UUID, manifest: CutoverManifest, actor_id: UUID, operation_id: UUID | None = None):
    return store.execute(_activate_envelope(workspace_id, manifest, operation_id), actor_id=actor_id, actor_kind="operator", required_scope="work.operate")


def test_pre_activation_mutations_reject_with_zero_canonical_rows(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id = uuid4(), uuid4()
        store = PostgresWorkStore(config)
        with pytest.raises(WorkStoreError, match="cutover_invariant"):
            store.execute(_create_envelope(workspace_id), actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            assert connection.execute("SELECT count(*) FROM omp_work.work_items").fetchone() == (0,)


def test_activation_applies_replays_once_and_stamps_first_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id, batch_id, export_id, epoch_id, operation_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        _seed_candidate(config, workspace_id, batch_id, export_id)
        store = PostgresWorkStore(config)

        receipt, result = _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, epoch_id), actor_id, operation_id)
        assert receipt.state.value == "applied" and result["authority"] == "work"
        replay_receipt, replay_result = _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, epoch_id), actor_id, operation_id)
        assert replay_receipt.state.value == "replayed" and replay_result == result

        with pytest.raises(WorkStoreError, match="cutover_invariant"):
            _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, uuid4()), actor_id)

        first = store.execute(_create_envelope(workspace_id), actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        assert first[0].state.value == "applied"
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            stamped = connection.execute("SELECT first_work_mutation_at FROM omp_control.workspace_authority WHERE workspace_id=%s", (workspace_id,)).fetchone()[0]
        assert stamped is not None
        store.execute(_create_envelope(workspace_id), actor_id=actor_id, actor_kind="owner", required_scope="work.mutate")
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            assert connection.execute("SELECT first_work_mutation_at FROM omp_control.workspace_authority WHERE workspace_id=%s", (workspace_id,)).fetchone()[0] == stamped

        authority = store.read(workspace_id, actor_id, "authority", "")
        assert authority["authority"] == "work" and authority["epoch_id"] == str(epoch_id) and authority["first_work_mutation_at"] is not None


@pytest.mark.parametrize(
    "override, diagnostic",
    [
        ({"command_smoke_results": [{"command_type": "create_work_batch", "passed": False}]}, "command_smoke_failed"),
        ({"backup_receipt_sha256": "9" * 64}, "backup_receipt_mismatch"),
        ({"parity_groups": {"work_items": "9" * 64, "states": "e" * 64}}, "parity_group_mismatch"),
        ({"raw_export_sha256": "9" * 64}, "source_boundary_mismatch"),
        ({"anomalies": [{"code": "pagination_count_hash_gap", "disposition": "blocking"}]}, "manifest_invariants_failed"),
    ],
)
def test_activation_rejects_invalid_manifests(tmp_path: Path, override: dict[str, object], diagnostic: str) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id, batch_id, export_id = uuid4(), uuid4(), uuid4(), uuid4()
        _seed_candidate(config, workspace_id, batch_id, export_id)
        store = PostgresWorkStore(config)
        with pytest.raises(WorkStoreError) as captured:
            _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, uuid4(), **override), actor_id)
        assert captured.value.code == "cutover_invariant" and diagnostic in captured.value.diagnostics
        with psycopg.connect(**config.connection_kwargs("postgres")) as connection:
            assert connection.execute("SELECT count(*) FROM omp_control.workspace_authority").fetchone() == (0,)


def test_stale_batch_rejects(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id, batch_id, export_id = uuid4(), uuid4(), uuid4(), uuid4()
        _seed_candidate(config, workspace_id, batch_id, export_id)
        newer = datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
            connection.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(config.actor_id())))
            connection.execute(
                "INSERT INTO omp_integration.raw_exports (export_id,workspace_id,team_key,mode,source_started_at,source_boundary,state,storage_root,raw_export_sha256,manifest_sha256,completed_at) VALUES (%s,%s,'HOME','delta',%s,%s,'complete','linear-exports/newer',%s,%s,%s)",
                (uuid4(), workspace_id, newer, newer, "3" * 64, "4" * 64, newer),
            )
        store = PostgresWorkStore(config)
        with pytest.raises(WorkStoreError) as captured:
            _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, uuid4()), actor_id)
        assert captured.value.code == "cutover_invariant" and "stale_import_batch" in captured.value.diagnostics


def test_epoch_transitions_are_one_way(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id, batch_id, export_id, epoch_id = uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
        _seed_candidate(config, workspace_id, batch_id, export_id)
        store = PostgresWorkStore(config)
        _activate(store, workspace_id, _manifest(config, workspace_id, batch_id, epoch_id), actor_id)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as connection:
            connection.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(config.actor_id())))
            # pre-write rollback is representable: active -> rolled_back
            connection.execute("UPDATE omp_control.cutover_epochs SET state='rolled_back', recovery_path='pre_write' WHERE epoch_id=%s", (epoch_id,))
            with pytest.raises(psycopg.Error, match="invalid cutover epoch transition"):
                connection.execute("UPDATE omp_control.cutover_epochs SET state='sealed' WHERE epoch_id=%s", (epoch_id,))
            with pytest.raises(psycopg.Error, match="immutable"):
                connection.execute("UPDATE omp_control.cutover_epochs SET candidate_manifest='{}'::jsonb WHERE epoch_id=%s", (epoch_id,))


def test_seeded_authority_read_reports_work(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        workspace_id, actor_id = uuid4(), uuid4()
        store = PostgresWorkStore(config)
        assert store.read(workspace_id, actor_id, "authority", "")["authority"] == "linear"
        seed_authority(config.connection_kwargs("postgres"), workspace_id, actor_id)
        assert store.read(workspace_id, actor_id, "authority", "")["authority"] == "work"
