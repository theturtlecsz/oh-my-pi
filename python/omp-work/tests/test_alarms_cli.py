"""OMP-430: alarms CLI contracts.

Defended contracts:
- `main(["alarms", sub, "--state", p])` exits 2 without HTTP for init, run, digest.
- `alarms watch-credentials` via main(), with `_alarm_client` patched to a
  WorkClient over TestClient(create_app) on native_postgres, records one
  credential_appeared event for a new key.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.__main__ import main
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.client import WorkClient
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority


@pytest.mark.parametrize("sub", ["init", "run", "digest"])
def test_alarms_subcommands_removed_exit_2(tmp_path: Path, sub: str) -> None:
    state_file = tmp_path / "alarm_state.json"
    with pytest.raises(SystemExit) as exc:
        main(["alarms", sub, "--state", str(state_file)])
    assert exc.value.code == 2


def _config(tmp_path: Path) -> OperationsConfig:
    import secrets
    import socket

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


def _bearer(
    capabilities_dir: Path,
    name: str,
    *,
    token: str,
    actor_id: UUID,
    actor_kind: str,
    workspace_id: UUID,
    scopes: tuple[str, ...],
) -> Path:
    capabilities_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    path = capabilities_dir / name
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": actor_kind,
                "workspaces": [str(workspace_id)],
                "scopes": list(scopes),
            }
        )
    )
    path.chmod(0o600)
    return path


def _client_json(
    tmp_path: Path, base_url: str, workspace_id: UUID, bearer: Path
) -> Path:
    path = tmp_path / "client.json"
    path.write_text(
        json.dumps(
            {
                "base_url": base_url,
                "workspace_id": str(workspace_id),
                "owner_id": str(uuid4()),
                "bearer_file": str(bearer),
            }
        )
    )
    path.chmod(0o600)
    return path


def test_alarms_watch_credentials_records_credential_appeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        workspace_id, owner_id = uuid4(), uuid4()
        automation_id = uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)
        with psycopg.connect(**config.connection_kwargs("postgres"), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (workspace_id,),
            )

        capabilities = tmp_path / "capabilities"
        automation_bearer = _bearer(
            capabilities,
            "automation.json",
            token="automation-token",
            actor_id=automation_id,
            actor_kind="automation",
            workspace_id=workspace_id,
            scopes=("work.read", "work.mutate"),
        )
        config_path = _client_json(
            tmp_path, "http://testserver", workspace_id, automation_bearer
        )

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        automation = WorkClient(
            "http://testserver",
            workspace_id,
            automation_bearer,
            transport=tc._transport,
        )

        def alarm_client(path: str | Path) -> tuple[WorkClient, UUID]:
            return automation, workspace_id

        monkeypatch.setattr("omp_work.__main__._alarm_client", alarm_client)

        ssh_root = tmp_path / "ssh"
        ssh_root.mkdir()
        cred_state = tmp_path / "cred_state.json"

        # First scan: clean directory, nothing signalled
        assert (
            main(
                [
                    "alarms",
                    "watch-credentials",
                    "--state",
                    str(cred_state),
                    "--root",
                    str(ssh_root),
                    "--client-config",
                    str(config_path),
                ]
            )
            == 0
        )

        # Add a new SSH key
        (ssh_root / "id_ed25519.pub").write_text("ssh-ed25519 AAAA new key\n")

        # Second scan: detects new key, records credential_appeared event
        assert (
            main(
                [
                    "alarms",
                    "watch-credentials",
                    "--state",
                    str(cred_state),
                    "--root",
                    str(ssh_root),
                    "--client-config",
                    str(config_path),
                ]
            )
            == 0
        )

        with psycopg.connect(
            **config.connection_kwargs("postgres"),
            autocommit=True,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id, sequence, event_type, payload "
                    "FROM omp_audit.domain_events WHERE workspace_id = %s AND event_type = 'record_alarm_signal'",
                    (workspace_id,),
                )
                rows = cur.fetchall()

        credential_events = [
            r for r in rows if r[3].get("signal") == "credential_appeared"
        ]
        assert len(credential_events) == 1
