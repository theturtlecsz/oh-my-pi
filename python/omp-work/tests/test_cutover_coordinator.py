"""HOME-148: cutover coordinator — freeze marker contract, state gating, and rollback rules."""
from __future__ import annotations

import json
import os
import secrets
import socket
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.operations import cutover
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from pg_native import native_postgres

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")


def _config(tmp_path: Path) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup", "gpg-passphrase", "workspace-id", "operator-actor-id"):
        path = credentials / role
        path.write_text(str(WORKSPACE) if role == "workspace-id" else (str(uuid4()) if role == "operator-actor-id" else secrets.token_urlsafe(24)))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data", port=port)


def _write_state(config: OperationsConfig, state: dict[str, object]) -> None:
    (config.config_dir / "cutover-state.json").write_text(json.dumps(state), encoding="utf-8")


def _candidate_state(port: int) -> dict[str, object]:
    return {
        "rehearsals": {},
        "candidate": {"pgdata": "/nonexistent", "workspace_id": str(WORKSPACE), "owner_id": str(uuid4()), "batch_id": str(uuid4()), "fingerprints": {}},
        "window": {"state": "executed", "port": port, "http_port": 1, "epoch_id": str(uuid4())},
    }


# --- freeze marker contract (the TS fence reads this exact path) ---

def test_freeze_marker_write_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = cutover._write_freeze_marker({"frozen_at": "2026-08-17T00:00:00+00:00"})
    assert path == tmp_path / "omp-work" / "linear-frozen.json"
    assert json.loads(path.read_text(encoding="utf-8"))["frozen_at"] == "2026-08-17T00:00:00+00:00"
    assert (path.stat().st_mode & 0o777) == 0o600
    cutover._remove_freeze_marker()
    assert not path.exists()
    cutover._remove_freeze_marker()  # idempotent


# --- gating without a database ---

def test_execute_blocked_without_rehearsals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    with pytest.raises(cutover.CutoverBlocked) as err:
        cutover.execute(config, mapping_file=tmp_path / "map.json")
    joined = "; ".join(err.value.blockers)
    assert "rehearsal 1" in joined and "rehearsal 2" in joined and "no retained candidate" in joined


def test_rehearse_two_requires_passing_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    with pytest.raises(cutover.CutoverBlocked, match="rehearsal 2 requires a passing rehearsal 1"):
        cutover.rehearse(config, ordinal=2, retain_candidate=True, mapping_file=tmp_path / "map.json")
    _write_state(config, {"rehearsals": {"1": {"verdict": "pass", "fingerprints": {"code": "stale"}}}})
    with pytest.raises(cutover.CutoverBlocked, match="fingerprints drifted"):
        cutover.rehearse(config, ordinal=2, retain_candidate=True, mapping_file=tmp_path / "map.json")


def test_rollback_blocks_blind_unfreeze_without_epoch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Marker present but no locatable epoch: rollback must NOT unfreeze Linear."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    cutover._write_freeze_marker({"frozen_at": "2026-08-17T00:00:00+00:00"})
    with pytest.raises(cutover.CutoverBlocked, match="no epoch row found"):
        cutover.rollback(config)
    assert cutover.freeze_marker_path().exists(), "marker must survive the refused rollback"


# --- rollback against a real database ---

@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rollback_refused_after_first_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        epoch_id = uuid4()
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(WORKSPACE),))
            cur.execute(
                "INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES (%s, %s, 'active', '{}'::jsonb, %s)",
                (str(WORKSPACE), str(epoch_id), "0" * 64),
            )
            cur.execute("INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id, first_work_mutation_at) VALUES (%s, %s, clock_timestamp())", (str(WORKSPACE), str(epoch_id)))
            conn.commit()
        _write_state(config, _candidate_state(config.port))
        with pytest.raises(cutover.CutoverBlocked, match="repair/restore is the only path"):
            cutover.rollback(config)
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM omp_control.cutover_epochs WHERE epoch_id = %s", (str(epoch_id),))
            assert cur.fetchone() == ("active",), "epoch untouched by the refused rollback"


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rollback_pre_mutation_restores_linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    selector_calls: list[str] = []
    monkeypatch.setattr(cutover, "_run_selector", selector_calls.append)
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        epoch_id = uuid4()
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(WORKSPACE),))
            cur.execute(
                "INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES (%s, %s, 'active', '{}'::jsonb, %s)",
                (str(WORKSPACE), str(epoch_id), "0" * 64),
            )
            cur.execute("INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES (%s, %s)", (str(WORKSPACE), str(epoch_id)))
            conn.commit()
        _write_state(config, _candidate_state(config.port))
        marker = cutover._write_freeze_marker({"frozen_at": "2026-08-17T00:00:00+00:00"})
        result = cutover.rollback(config)
        assert result["epoch_id"] == str(epoch_id)
        assert selector_calls == ["linear"], "selector switched back to linear exactly once"
        assert not marker.exists(), "freeze marker removed last"
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT state, recovery_path FROM omp_control.cutover_epochs WHERE epoch_id = %s", (str(epoch_id),))
            assert cur.fetchone() == ("rolled_back", "pre_write")
            cur.execute("SELECT count(*) FROM omp_control.workspace_authority WHERE workspace_id = %s", (str(WORKSPACE),))
            assert cur.fetchone() == (0,), "work authority gone"
