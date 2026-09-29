"""OMP-406-s07: end-to-end `omp-work alarms` CLI over the alarm dispatcher,
the Grokbot sender, and the credential watcher.

Defended contracts:

- without ``OMP_GROKBOT_ALERT_URL``/``OMP_GROKBOT_ALERT_TOKEN_FILE`` ``alarms
  run`` exits 2 and performs no HTTP request, and ``_alarm_client`` builds a
  client from a ``client.json`` (base_url, workspace_id, bearer_file);
- against a real PostgreSQL workspace, ``alarms init`` snapshots the current
  watermark, ``watch-credentials`` records one ``credential_appeared`` signal
  for a new key, and ``alarms run`` posts exactly one alert per classified
  domain event (the three refused owner-approval attempts plus the five other
  kinds), each carrying the event id, sequence, and sha256 of its
  ``omp_audit.domain_events`` row; a rerun posts nothing;
- ``alarms digest --day <today UTC>`` counts only rest events, so it reports
  the ``create_work_batch`` event and none of the alert sequences.
"""

from __future__ import annotations

import http.server
import json
import os
from pathlib import Path
import secrets
import socket
import threading
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from omp_work.__main__ import _alarm_client, main
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    AttestIntakeAdmissionCommand,
    AttestIntakeAdmissionPayload,
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

OWNER_SCOPES = ("work.read", "work.mutate", "work.approve", "work.close", "work.execute")
AUTOMATION_SCOPES = ("work.read", "work.mutate")


class _AlertStub(http.server.BaseHTTPRequestHandler):
    recorded: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        self.recorded.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw.decode("utf-8")) if raw else None,
            }
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def alert_stub():
    _AlertStub.recorded = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _AlertStub)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}/api/alerts"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


def _bearer(
    directory: Path,
    name: str,
    *,
    token: str,
    actor_id: UUID,
    actor_kind: str,
    workspace_id: UUID,
    scopes: tuple[str, ...],
) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
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


def _client_json(tmp_path: Path, base_url: str, workspace_id: UUID, bearer: Path) -> Path:
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


def _set_alert_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, token: str
) -> None:
    token_file = tmp_path / "grokbot-token"
    token_file.write_text(f"  {token}  \n")
    monkeypatch.setenv("OMP_GROKBOT_ALERT_URL", url)
    monkeypatch.setenv("OMP_GROKBOT_ALERT_TOKEN_FILE", str(token_file))


def _attest(workspace_id: UUID, work_id: UUID) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=AttestIntakeAdmissionCommand(
            type="attest_intake_admission",
            payload=AttestIntakeAdmissionPayload(
                work_id=work_id,
                revision_id=uuid4(),
                plan_receipt_id=uuid4(),
                fable_advice_receipt_id=uuid4(),
                native_acceptance_receipt_id=uuid4(),
            ),
        ),
    )


def test_alarms_run_without_grokbot_env_exits_2_without_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alert_stub: str,
) -> None:
    monkeypatch.delenv("OMP_GROKBOT_ALERT_URL", raising=False)
    monkeypatch.delenv("OMP_GROKBOT_ALERT_TOKEN_FILE", raising=False)

    bearer = _bearer(
        tmp_path / "capabilities",
        "automation.json",
        token="automation-token",
        actor_id=uuid4(),
        actor_kind="automation",
        workspace_id=uuid4(),
        scopes=AUTOMATION_SCOPES,
    )
    config_path = _client_json(tmp_path, alert_stub, uuid4(), bearer)
    state_path = tmp_path / "alarm_state.json"

    client, workspace_id = _alarm_client(config_path)
    assert isinstance(client, WorkClient)
    assert workspace_id == UUID(json.loads(config_path.read_text())["workspace_id"])

    assert (
        main(["alarms", "run", "--state", str(state_path), "--client-config", str(config_path)])
        == 2
    )
    assert _AlertStub.recorded == []


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_alarms_cli_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    alert_stub: str,
) -> None:
    token = "grokbot-secret-token"
    _set_alert_env(monkeypatch, tmp_path, alert_stub, token)

    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        workspace_id, owner_id = uuid4(), uuid4()
        automation_id = uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)

        capabilities = tmp_path / "capabilities"
        owner_bearer = _bearer(
            capabilities,
            "owner.json",
            token="owner-token",
            actor_id=owner_id,
            actor_kind="owner",
            workspace_id=workspace_id,
            scopes=OWNER_SCOPES,
        )
        automation_bearer = _bearer(
            capabilities,
            "automation.json",
            token="automation-token",
            actor_id=automation_id,
            actor_kind="automation",
            workspace_id=workspace_id,
            scopes=AUTOMATION_SCOPES,
        )
        config_path = _client_json(tmp_path, "http://testserver", workspace_id, automation_bearer)

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        owner = WorkClient(
            "http://testserver", workspace_id, owner_bearer, transport=tc._transport
        )
        automation = WorkClient(
            "http://testserver", workspace_id, automation_bearer, transport=tc._transport
        )

        def alarm_client(path: str | Path) -> tuple[WorkClient, UUID]:
            return automation, workspace_id

        monkeypatch.setattr("omp_work.__main__._alarm_client", alarm_client)

        # 1. owner creates A and B in one batch, then alarms init snapshots the watermark.
        created = owner.execute(
            CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=CreateWorkBatchCommand(
                    type="create_work_batch",
                    payload=CreateWorkBatchPayload(
                        items=(
                            CreateWorkInput(client_ref="ref-a", title="work A"),
                            CreateWorkInput(client_ref="ref-b", title="work B"),
                        )
                    ),
                ),
            )
        )
        work_a = created.result.items[0].work_id
        work_b = created.result.items[1].work_id

        state_path = tmp_path / "alarm_state.json"
        assert (
            main(["alarms", "init", "--state", str(state_path), "--client-config", str(config_path)])
            == 0
        )
        assert state_path.exists()

        # 2. automation signals three kinds and the watcher records a new key.
        def signal(work_id: UUID, name: str) -> None:
            automation.execute(
                CommandEnvelope(
                    api_version="work.omp.dev/v1",
                    workspace_id=workspace_id,
                    operation_id=uuid4(),
                    request_id=uuid4(),
                    correlation_id=uuid4(),
                    command=RecordAlarmSignalCommand(
                        type="record_alarm_signal",
                        payload=RecordAlarmSignalPayload(
                            signal=name, work_id=work_id, subject=name
                        ),
                    ),
                )
            )

        signal(work_a, "cost_threshold")
        signal(work_a, "budget_exceeded")
        signal(work_b, "safety_check_failed")

        ssh_root = tmp_path / "ssh"
        ssh_root.mkdir()
        cred_state = tmp_path / "cred_state.json"
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
        (ssh_root / "id_ed25519.pub").write_text("ssh-ed25519 AAAA new key\n")
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

        # 3. three refused owner-approval attempts on A (automation lacks work.approve).
        for _ in range(3):
            with pytest.raises(Exception) as refused:
                automation.execute(_attest(workspace_id, work_a))
            assert getattr(refused.value, "status", None) == 403

        # 4. alarms run posts one alert per classified event.
        assert (
            main(["alarms", "run", "--state", str(state_path), "--client-config", str(config_path)])
            == 0
        )

        alerts = [req for req in _AlertStub.recorded if req["body"]["type"] == "alert"]
        assert len(alerts) == 8
        kinds: dict[str, int] = {}
        for request in alerts:
            kinds[request["body"]["kind"]] = kinds.get(request["body"]["kind"], 0) + 1
        assert kinds == {
            "cost_threshold": 1,
            "budget_exceeded": 1,
            "safety_check_failed": 1,
            "credential_appeared": 1,
            "owner_approval_attempt": 3,
            "repeated_failure": 1,
        }
        for request in alerts:
            assert request["headers"].get("Authorization") == f"Bearer {token}"
            assert request["headers"].get("Idempotency-Key")

        # every alert carries the id/sequence/sha of a real domain_events row
        with psycopg.connect(
            **config.connection_kwargs("postgres"), row_factory=psycopg.rows.dict_row
        ) as conn:
            rows = conn.execute(
                "SELECT event_id, sequence, event_sha256 "
                "FROM omp_audit.domain_events WHERE workspace_id = %s",
                (workspace_id,),
            ).fetchall()
        by_id = {str(row["event_id"]): row for row in rows}
        for request in alerts:
            event = request["body"]["event"]
            row = by_id[event["event_id"]]
            assert event["sequence"] == row["sequence"]
            assert event["event_sha256"] == row["event_sha256"]

        # rerun sends nothing new
        sent_before = len(_AlertStub.recorded)
        assert (
            main(["alarms", "run", "--state", str(state_path), "--client-config", str(config_path)])
            == 0
        )
        assert len(_AlertStub.recorded) == sent_before

        # 5. digest counts rest events only: the create_work_batch event, no alert sequence.
        today = datetime.now(UTC).date().isoformat()
        assert (
            main(
                [
                    "alarms",
                    "digest",
                    "--state",
                    str(state_path),
                    "--client-config",
                    str(config_path),
                    "--day",
                    today,
                ]
            )
            == 0
        )
        digest = _AlertStub.recorded[-1]["body"]
        assert digest["type"] == "digest"
        assert digest["by_event_type"] == {"create_work_batch": 1}
        assert digest["work_items"] == 2
        alert_sequences = {req["body"]["event"]["sequence"] for req in alerts}
        assert digest["first_sequence"] not in alert_sequences
        assert digest["last_sequence"] not in alert_sequences
