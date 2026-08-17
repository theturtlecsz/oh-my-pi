"""HOME-148: cutover coordinator — freeze marker contract, state gating, and rollback rules."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256 as bytes_sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.operations import cutover
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
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
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    with pytest.raises(cutover.CutoverBlocked) as err:
        cutover.execute(config, mapping_file=tmp_path / "map.json", plan_file=plan)
    joined = "; ".join(err.value.blockers)
    assert "rehearsal 1" in joined and "rehearsal 2" in joined and "no retained candidate" in joined


def test_pre_activation_deadline_keeps_ambiguous_submission_frozen_but_rolls_back_proven_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    marker = cutover._write_freeze_marker({"frozen_at": datetime.now(timezone.utc).isoformat()})
    frozen_at = datetime.now(timezone.utc) - timedelta(minutes=46)
    state: dict[str, object] = {}
    for activation_state in ("started", "completed"):
        window: dict[str, object] = {"state": "activating", "steps": {"activation": {"state": activation_state}}}
        cutover._enforce_pre_activation_deadline(config, state, window, frozen_at)
        assert marker.exists(), f"{activation_state} activation must keep Linear frozen"

    window = {"state": "activating", "steps": {"activation": {"state": "started"}}}
    with pytest.raises(cutover.CutoverBlocked, match="before activation"):
        cutover._enforce_pre_activation_deadline(config, state, window, frozen_at, activation_committed=False)
    assert not marker.exists()
    assert window["state"] == "failed"


def test_finalize_probes_provisioned_oauth_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    manifest = {"sealed": "manifest"}
    key = b"personal-key"
    _write_state(config, {
        "candidate": {"workspace_id": str(WORKSPACE)},
        "window": {
            "state": "executed",
            "port": config.port,
            "credential_sha256": bytes_sha256(key).hexdigest(),
            "steps": {"activation": {"manifest": manifest}},
        },
    })
    cutover._write_freeze_marker({"frozen_at": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(cutover, "_epoch_row", lambda *_: {
        "state": "active",
        "candidate_manifest_sha256": sha256(manifest),
        "revoked_at": None,
        "final_report_sha256": None,
    })
    monkeypatch.setattr(cutover, "_personal_key_status", lambda: "revoked")

    def wrong_backend(_backend: str) -> None:
        raise cutover.CutoverBlocked(["wrong backend"])

    monkeypatch.setattr(cutover, "_expect_backend", wrong_backend)
    monkeypatch.setattr(cutover, "_read_linear_key_bytes", lambda: key)
    observed: dict[str, object] = {}

    def refresh(path: Path) -> object:
        observed["path"] = path
        return SimpleNamespace(access_token=SimpleNamespace(get_secret_value=lambda: "oauth-token"))

    def projects(token: str) -> set[str]:
        observed["token"] = token
        return {"project-id"}

    monkeypatch.setattr(cutover, "refresh_credential", refresh)
    monkeypatch.setattr(cutover, "_linear_project_ids", projects)
    with pytest.raises(cutover.CutoverBlocked, match="installed backend"):
        cutover.finalize(config)
    assert observed == {"path": config.secret_path("linear-export.json"), "token": "oauth-token"}


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
        # The guard must also hold INSIDE the transaction, not just in the Python
        # fast path: a stamp landing between rollback()'s read and _rollback_epoch's
        # UPDATE must still refuse and leave authority intact.
        with pytest.raises(cutover.CutoverBlocked, match="repair/restore is the only path"):
            cutover._rollback_epoch(config, epoch_id, WORKSPACE)
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM omp_control.cutover_epochs WHERE epoch_id = %s", (str(epoch_id),))
            assert cur.fetchone() == ("active",), "epoch untouched by the in-transaction refusal"
            cur.execute("SELECT count(*) FROM omp_control.workspace_authority WHERE workspace_id = %s AND first_work_mutation_at IS NOT NULL", (str(WORKSPACE),))
            assert cur.fetchone() == (1,), "stamped authority row survives the refused rollback"



@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rehearsal_seeds_workspace_before_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh rehearsal database must contain its workspace root before any
    importer projection can reach a workspace foreign key."""
    config = _config(tmp_path)
    observed: list[bool] = []

    class WorkspaceProbeExporter:
        def __init__(self, scratch_config: OperationsConfig) -> None:
            self.config = scratch_config

        def full(self, workspace_id: UUID) -> None:
            with psycopg.connect(**self.config.connection_kwargs("postgres")) as connection:
                observed.append(connection.execute("SELECT 1 FROM omp_control.workspaces WHERE workspace_id=%s", (workspace_id,)).fetchone() == (1,))
            raise RuntimeError("workspace_probe_complete")

    monkeypatch.setattr(cutover, "LinearExporter", WorkspaceProbeExporter)
    monkeypatch.setattr(cutover, "_write_report", lambda *args, **kwargs: ("report", "0" * 64, "1" * 64))

    with pytest.raises(RuntimeError, match="workspace_probe_complete"):
        cutover.rehearse(config, ordinal=1, retain_candidate=False, mapping_file=tmp_path / "mapping.json")

    assert observed == [True]
    state = json.loads((config.config_dir / "cutover-state.json").read_text(encoding="utf-8"))
    assert state["rehearsals"]["1"]["stage"] == "full_export"

# --- execute failure path ---

@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_execute_failure_before_authority_unfreezes_linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An execute that dies before any authority row exists must auto-unfreeze Linear
    and record the window as failed — the freeze marker is never left behind by a
    pre-authority crash."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    owner_id = uuid4()
    fingerprints = {"code": "c", "contract": "k", "migrations": "m"}

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pgdata = scratch / "candidate-pgdata"
    cutover._pg_init(config, pgdata, cutover._free_port(), scratch)
    try:
        boot = replace(config, port=cutover._free_port())
        cutover._pg_stop(pgdata)
        cutover._pg_start(pgdata, port=boot.port, socket_dir=scratch, log=scratch / "pg-boot.log")
        bootstrap(boot)
        with psycopg.connect(**boot.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(WORKSPACE),))
            conn.commit()
    finally:
        cutover._pg_stop(pgdata)
    # execute derives its scratch config from the candidate path; it must find the
    # role credentials there exactly like a rehearsal-retained candidate would.
    scratch_credentials = (scratch / "xdg") / "omp" / "work-ledger" / "credentials"
    scratch_credentials.mkdir(parents=True, mode=0o700)
    for source in config.credentials_dir.iterdir():
        if source.is_file():
            target = scratch_credentials / source.name
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o600)

    monkeypatch.setattr(cutover, "_fingerprints", lambda: fingerprints)
    monkeypatch.setattr(cutover, "FREEZE_DRAIN_SECONDS", 0.0)
    key_file = tmp_path / "linear.env"
    key_file.write_text("LINEAR_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "LINEAR_ENV_PATH", key_file)

    def _exploding_delta(*args: object, **kwargs: object) -> None:
        raise RuntimeError("delta exploded")

    monkeypatch.setattr(cutover, "_window_delta_export", _exploding_delta)

    batch_1, batch_2 = str(uuid4()), str(uuid4())
    _write_state(config, {
        "rehearsals": {
            "1": {"verdict": "pass", "run_id": "r1", "batch_id": batch_1, "fingerprints": fingerprints},
            "2": {"verdict": "pass", "run_id": "r2", "batch_id": batch_2, "fingerprints": fingerprints},
        },
        "candidate": {"pgdata": str(pgdata), "fingerprints": fingerprints, "batch_id": batch_2, "workspace_id": str(WORKSPACE), "owner_id": str(owner_id)},
    })
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="delta exploded"):
        cutover.execute(config, mapping_file=tmp_path / "map.json", plan_file=plan)
    assert not cutover.freeze_marker_path().exists(), "pre-authority failure must unfreeze Linear"
    saved = json.loads((config.config_dir / "cutover-state.json").read_text(encoding="utf-8"))
    assert saved["window"]["state"] == "failed"
    assert "candidate" not in saved, "failed candidate must not remain admissible"
    assert "2" not in saved["rehearsals"], "a fresh rehearsal 2 is required"
    archived = Path(saved["window"]["failed_candidate_path"])
    assert archived.is_dir() and archived.name == saved["window"]["attempt_id"]
    assert not scratch.exists(), "the poisoned source chain moved to immutable failure evidence"



def test_execute_resume_converges_started_managed_handoff_before_authority_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash after the managed-runtime step starts may leave PostgreSQL on either
    port; resume must converge it to the final port before deciding authority."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    owner_id = uuid4()
    staging_port = cutover._free_port()
    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    key_file = tmp_path / "linear.env"
    key_file.write_text("LINEAR_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "LINEAR_ENV_PATH", key_file)
    frozen_at = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    plan_sha = bytes_sha256(plan.read_bytes()).hexdigest()
    started: list[int] = []
    probed: list[int] = []
    monkeypatch.setattr(cutover, "provision_owner", lambda *args, **kwargs: None)
    monkeypatch.setattr(cutover, "provision_cutover", lambda *args, **kwargs: None)
    monkeypatch.setattr(cutover, "_start_managed_runtime", lambda candidate_config, **kwargs: started.append(candidate_config.port))

    def authority(candidate_config: OperationsConfig, workspace_id: UUID) -> dict[str, object]:
        probed.append(candidate_config.port)
        return {"first_work_mutation_at": None}

    monkeypatch.setattr(cutover, "_authority_row", authority)
    monkeypatch.setattr(cutover, "_epoch_row", lambda *args, **kwargs: {"state": "active"})
    monkeypatch.setattr(cutover, "_authority_present", lambda *args, **kwargs: True)
    _write_state(config, {
        "rehearsals": {},
        "candidate": {
            "pgdata": str(tmp_path / "candidate-pgdata"),
            "workspace_id": str(WORKSPACE),
            "owner_id": str(owner_id),
            "batch_id": str(uuid4()),
            "fingerprints": {},
        },
        "window": {
            "state": "activating",
            "candidate_pgdata": str(tmp_path / "candidate-pgdata"),
            "port": staging_port,
            "managed_port": config.port,
            "http_port": cutover._free_port(),
            "plan_sha256": plan_sha,
            "credential_sha256": bytes_sha256(b"test-key").hexdigest(),
            "frozen_at": frozen_at,
            "steps": {
                "freeze": {
                    "state": "completed",
                    "input_sha256": sha256({"workspace_id": str(WORKSPACE), "plan_sha256": plan_sha}),
                    "output": {"frozen_at": frozen_at},
                },
                "managed_runtime": {"state": "started"},
            },
        },
    })

    with pytest.raises(cutover.CutoverBlocked, match="hard deadline"):
        cutover.execute(config, mapping_file=tmp_path / "map.json", plan_file=plan)

    assert started == [config.port]
    assert probed == [config.port], "authority is never queried on the stale staging port"
    saved = json.loads((config.config_dir / "cutover-state.json").read_text(encoding="utf-8"))
    assert saved["window"]["port"] == config.port
    assert saved["window"]["steps"]["managed_runtime"]["state"] == "completed"

@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_execute_past_hard_deadline_mutates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume past the T+60 hard ceiling initiates no mutation. Work authority
    remains active and Linear frozen so the operator can run the still-legal
    pre-first-mutation rollback."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    owner_id = uuid4()
    fingerprints = {"code": "c", "contract": "k", "migrations": "m"}

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    pgdata = scratch / "candidate-pgdata"
    cutover._pg_init(config, pgdata, cutover._free_port(), scratch)
    epoch_id = uuid4()
    try:
        boot = replace(config, port=cutover._free_port())
        cutover._pg_stop(pgdata)
        cutover._pg_start(pgdata, port=boot.port, socket_dir=scratch, log=scratch / "pg-boot.log")
        bootstrap(boot)
        with psycopg.connect(**boot.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(WORKSPACE),))
            cur.execute(
                "INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256) VALUES (%s, %s, 'active', '{}'::jsonb, %s)",
                (str(WORKSPACE), str(epoch_id), "0" * 64),
            )
            cur.execute("INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id) VALUES (%s, %s)", (str(WORKSPACE), str(epoch_id)))
            conn.commit()
    finally:
        cutover._pg_stop(pgdata)
    scratch_credentials = (scratch / "xdg") / "omp" / "work-ledger" / "credentials"
    scratch_credentials.mkdir(parents=True, mode=0o700)
    for source in config.credentials_dir.iterdir():
        if source.is_file():
            target = scratch_credentials / source.name
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            target.chmod(0o600)

    key_file = tmp_path / "linear.env"
    key_file.write_text("LINEAR_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setattr(cutover, "LINEAR_ENV_PATH", key_file)
    monkeypatch.setattr(cutover, "_fingerprints", lambda: fingerprints)

    plan = tmp_path / "plan.md"
    plan.write_text("# plan\n", encoding="utf-8")
    plan_sha = bytes_sha256(plan.read_bytes()).hexdigest()
    frozen_at = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    marker = cutover._write_freeze_marker({"frozen_at": frozen_at, "workspace_id": str(WORKSPACE), "actor": "owner"})
    window_port = cutover._free_port()
    _write_state(config, {
        "rehearsals": {},
        "candidate": {"pgdata": str(pgdata), "fingerprints": fingerprints, "batch_id": str(uuid4()), "workspace_id": str(WORKSPACE), "owner_id": str(owner_id)},
        "window": {
            "state": "activating",
            "candidate_pgdata": str(pgdata),
            "port": window_port,
            "http_port": cutover._free_port(),
            "plan_sha256": plan_sha,
            "credential_sha256": bytes_sha256(b"test-key").hexdigest(),
            "frozen_at": frozen_at,
            "steps": {
                "freeze": {
                    "input_sha256": sha256({"workspace_id": str(WORKSPACE), "plan_sha256": plan_sha}),
                    "state": "completed",
                    "output": {"frozen_at": frozen_at, "drain_seconds": 0.0},
                },
            },
        },
    })
    with pytest.raises(cutover.CutoverBlocked, match="hard deadline"):
        cutover.execute(config, mapping_file=tmp_path / "map.json", plan_file=plan)
    assert marker.exists(), "authority present — the marker must stay"
    assert cutover._pg_status(pgdata) == 0, "active authority remains reachable for explicit rollback"
    try:
        with psycopg.connect(**replace(config, port=window_port).connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT first_work_mutation_at FROM omp_control.workspace_authority WHERE workspace_id = %s", (str(WORKSPACE),))
            assert cur.fetchone() == (None,), "no mutation landed past the deadline"
    finally:
        cutover._pg_stop(pgdata)


def test_focus_plan_item_refuses_write_past_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reads consume the window: a focus read that crosses T+60 must make the
    write re-check fail before any set_focus request is sent."""

    class _Clock(datetime):
        offset = 0.0

        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return datetime.now(tz) + timedelta(seconds=cls.offset)

    monkeypatch.setattr(cutover, "datetime", _Clock)
    executed: list[object] = []
    timeouts: list[float] = []
    plan_work_id = uuid4()

    class _StubClient:
        def __init__(self, base_url: str, workspace_id: object, capability: Path, *, timeout: float = 10) -> None:
            timeouts.append(timeout)

        def work_item(self, alias: str) -> dict[str, object]:
            return {}

        def focus(self, owner_id: object) -> object:
            _Clock.offset = 61 * 60  # the read crosses the hard ceiling
            return SimpleNamespace(work_id=uuid4(), version=0)

        def execute(self, envelope: object) -> object:
            executed.append(envelope)
            raise AssertionError("set_focus must not be sent past the deadline")

    monkeypatch.setattr(cutover, "WorkClient", _StubClient)
    config = _config(tmp_path)
    with pytest.raises(cutover.CutoverBlocked, match="hard deadline"):
        cutover._focus_plan_item(config, base_url="http://127.0.0.1:1", workspace_id=WORKSPACE, owner_id=uuid4(), plan_work_id=plan_work_id, frozen_at=datetime.now(timezone.utc))
    assert executed == [], "no production write past the deadline"
    assert timeouts == [10.0, 10.0], "each read gets a freshly bounded client before the crossing"


def test_focus_plan_item_bounds_write_by_remaining_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the window, the write client's timeout is no greater than the time
    actually remaining (capped at the default 10s)."""
    executed: list[object] = []
    timeouts: list[float] = []
    plan_work_id = uuid4()
    frozen_at = datetime.now(timezone.utc) - timedelta(minutes=59)  # ~60s left

    class _StubClient:
        def __init__(self, base_url: str, workspace_id: object, capability: Path, *, timeout: float = 10) -> None:
            timeouts.append(timeout)

        def work_item(self, alias: str) -> dict[str, object]:
            return {}

        def focus(self, owner_id: object) -> object:
            return SimpleNamespace(work_id=uuid4(), version=3)

        def execute(self, envelope: object) -> object:
            executed.append(envelope)
            return SimpleNamespace(receipt=SimpleNamespace(state="applied", diagnostics={}))

    monkeypatch.setattr(cutover, "WorkClient", _StubClient)
    config = _config(tmp_path)
    cutover._focus_plan_item(config, base_url="http://127.0.0.1:1", workspace_id=WORKSPACE, owner_id=uuid4(), plan_work_id=plan_work_id, frozen_at=frozen_at)
    assert len(executed) == 1, "the focus write lands inside the window"
    assert len(timeouts) == 3 and all(0 < t <= 60 for t in timeouts), f"both reads and the write are bounded by fresh remaining-window checks: {timeouts}"


def test_post_write_recovery_ignores_elapsed_ceiling_but_never_refocuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stamped recovery can finish after T+60, but it cannot introduce a new
    production focus mutation outside the original window."""
    old_freeze = datetime.now(timezone.utc) - timedelta(hours=2)
    cutover._enforce_cutover_deadline(old_freeze, post_write_recovery=True)
    with pytest.raises(cutover.CutoverBlocked, match="hard deadline"):
        cutover._enforce_cutover_deadline(old_freeze, post_write_recovery=False)
    executed: list[object] = []

    class _StubClient:
        def __init__(self, base_url: str, workspace_id: object, capability: Path, *, timeout: float = 10) -> None:
            assert timeout == 10.0

        def work_item(self, alias: str) -> dict[str, object]:
            return {}

        def focus(self, owner_id: object) -> object:
            return SimpleNamespace(work_id=uuid4(), version=2)

        def execute(self, envelope: object) -> object:
            executed.append(envelope)
            raise AssertionError("recovery must remain read-only")

    monkeypatch.setattr(cutover, "WorkClient", _StubClient)
    with pytest.raises(cutover.CutoverBlocked, match="no production mutation"):
        cutover._focus_plan_item(
            _config(tmp_path),
            base_url="http://127.0.0.1:1",
            workspace_id=WORKSPACE,
            owner_id=uuid4(),
            plan_work_id=uuid4(),
            frozen_at=None,
        )
    assert executed == []


def test_candidate_service_receives_rehearsal_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = replace(_config(tmp_path), prefix="work-ledger/v1/cutover/rehearsals/test")
    child_env: dict[str, str] = {}

    def fake_popen(*args: object, **kwargs: object) -> SimpleNamespace:
        env = kwargs["env"]
        assert isinstance(env, dict)
        child_env.update((str(key), str(value)) for key, value in env.items())
        return SimpleNamespace()

    monkeypatch.setattr(cutover.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cutover.httpx, "get", lambda *args, **kwargs: SimpleNamespace(status_code=200))

    cutover._serve(config, 54323)
    monkeypatch.setenv("OMP_WORK_S3_PREFIX", config.prefix)

    assert child_env["OMP_WORK_S3_PREFIX"] == config.prefix
    assert OperationsConfig.defaults().prefix == config.prefix


def test_installer_renders_managed_candidate_runtime(tmp_path: Path) -> None:
    """The durable units must start the promoted candidate on the exact live ports."""
    home = tmp_path / "home"
    pgdata = tmp_path / "candidate-pgdata"
    env = os.environ | {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    script = Path(__file__).resolve().parents[3] / "infra" / "work-ledger" / "install.sh"
    subprocess.run(
        ["bash", str(script), "--postgres-data", str(pgdata), "--postgres-port", "55431", "--http-port", "55432"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    units = home / ".config" / "systemd" / "user"
    postgres = (units / "omp-work-postgres.service").read_text(encoding="utf-8")
    service = (units / "omp-work-service.service").read_text(encoding="utf-8")
    assert f"ExecStart=/usr/bin/postgres -D {pgdata} -p 55431" in postgres
    assert "OMP_WORK_POSTGRES_PORT=55431" in service
    assert "ExecStart=" in service and "serve --port 55432" in service


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rollback_resumes_after_selector_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash boundary: _rollback_epoch committed but the selector swap failed. The
    retry must NOT refuse on the rolled_back epoch — authority is proven absent, so
    it resumes at the selector step and unfreezes Linear exactly once."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    selector_calls: list[str] = []
    fail_selector = True

    def flaky_selector(backend: str) -> None:
        selector_calls.append(backend)
        if fail_selector:
            raise cutover.CutoverBlocked(["install.sh --backend linear failed: simulated"])

    monkeypatch.setattr(cutover, "_run_selector", flaky_selector)
    monkeypatch.setattr(cutover, "_expect_backend", lambda backend: None)
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

        with pytest.raises(cutover.CutoverBlocked, match="simulated"):
            cutover.rollback(config)
        assert marker.exists(), "freeze marker survives the failed selector swap"
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT state FROM omp_control.cutover_epochs WHERE epoch_id = %s", (str(epoch_id),))
            assert cur.fetchone() == ("rolled_back",), "DB phase committed before the selector failure"
            cur.execute("SELECT count(*) FROM omp_control.workspace_authority WHERE workspace_id = %s", (str(WORKSPACE),))
            assert cur.fetchone() == (0,), "authority already deleted"

        fail_selector = False
        result = cutover.rollback(config)
        assert result["epoch_id"] == str(epoch_id)
        assert selector_calls == ["linear", "linear"], "selector retried on resume"
        assert not marker.exists(), "freeze marker removed on the resumed rollback"


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rollback_pre_mutation_restores_linear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    selector_calls: list[str] = []
    monkeypatch.setattr(cutover, "_run_selector", selector_calls.append)
    monkeypatch.setattr(cutover, "_expect_backend", lambda backend: None)
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


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_rollback_finds_database_after_managed_handoff_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash after PostgreSQL moved to the managed port must not strand rollback
    probing the persisted staging port while Linear remains frozen."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    staging_port = cutover._free_port()
    stopped: list[bool] = []
    monkeypatch.setattr(cutover, "_stop_managed_runtime", lambda: stopped.append(True))
    monkeypatch.setattr(cutover, "_run_selector", lambda backend: None)
    monkeypatch.setattr(cutover, "_expect_backend", lambda backend: None)
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
        state = _candidate_state(staging_port)
        state["candidate"]["pgdata"] = str(tmp_path / "pgdata")  # type: ignore[index]
        state["window"].update({  # type: ignore[union-attr]
            "managed_port": config.port,
            "steps": {"managed_runtime": {"state": "started"}},
        })
        _write_state(config, state)
        marker = cutover._write_freeze_marker({"frozen_at": "2026-08-17T00:00:00+00:00"})

        result = cutover.rollback(config)

        assert result["epoch_id"] == str(epoch_id)
        assert stopped == [True], "the managed units own the database found on the final port"
        assert not marker.exists()
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("SELECT state, recovery_path FROM omp_control.cutover_epochs WHERE epoch_id = %s", (str(epoch_id),))
            assert cur.fetchone() == ("rolled_back", "pre_write")


@pytest.mark.skipif(os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1", reason="set OMP_WORK_POSTGRES_INTEGRATION=1")
def test_status_reports_epoch_and_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    empty = cutover.status(config)
    assert empty["authority"] == "linear" and empty["epoch"] is None and empty["freeze_marker"] is False
    with native_postgres(tmp_path, config.port):
        bootstrap(config)
        state = _candidate_state(config.port)
        epoch_id = state["window"]["epoch_id"]
        with psycopg.connect(**config.connection_kwargs("postgres")) as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING", (str(WORKSPACE),))
            cur.execute(
                "INSERT INTO omp_control.cutover_epochs(workspace_id, epoch_id, state, candidate_manifest, candidate_manifest_sha256, revoked_at) VALUES (%s, %s, 'sealed', '{}'::jsonb, %s, clock_timestamp())",
                (str(WORKSPACE), str(epoch_id), "0" * 64),
            )
            cur.execute("INSERT INTO omp_control.workspace_authority(workspace_id, epoch_id, first_work_mutation_at) VALUES (%s, %s, clock_timestamp())", (str(WORKSPACE), str(epoch_id)))
            conn.commit()
        _write_state(config, state)
        report = cutover.status(config)
        assert report["authority"] == "work"
        epoch = report["epoch"]
        assert epoch["epoch_id"] == epoch_id and epoch["state"] == "sealed"
        assert epoch["first_work_mutation_at"] is not None and epoch["revoked_at"] is not None


def test_status_reports_unknown_when_authority_database_is_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An outage while Linear is frozen must never be mislabeled as Linear authority."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = _config(tmp_path)
    _write_state(config, _candidate_state(config.port))
    cutover._write_freeze_marker({"frozen_at": datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(cutover, "_epoch_row", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("database unavailable")))
    report = cutover.status(config)
    assert report["authority"] == "unknown"
    assert report["epoch"]["error"] == "RuntimeError: database unavailable"
