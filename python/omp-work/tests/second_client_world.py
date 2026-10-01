"""OMP-424-s02: a disposable world for driving the stdlib second client over HTTP.

One ``world(root)`` call bootstraps a native PostgreSQL instance, seeds a
workspace with one project, mints an owner capability and a client capability,
designates the client as the workspace controller with an ssh-keygen signature,
and serves ``create_app`` with uvicorn on a free loopback port. The second
client runs in a separate ``python -I -S`` process against that address, so the
journey exercises the real HTTP contract rather than an in-process transport.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Generator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
import uvicorn

from omp_work import contract_sha256
from omp_work.operations.capabilities import (
    OWNER_SCOPES,
    capabilities_dir,
    provision_client,
    write_capability,
)
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    CommandEnvelope,
    CreateDecisionCommand,
    CreateDecisionPayload,
    RecordFinding,
    RecordFindingCommand,
    SetMissionStatusCommand,
    SetMissionStatusPayload,
)
from omp_work.v1.owner_controller import designation_message, write_designation
from omp_work.v1.owner_signature import NAMESPACE
from omp_work.v1.server import create_app
from pg_native import native_postgres, seed_authority

_SECOND_CLIENT = Path(__file__).resolve().parent / "fixtures" / "second_client.py"

_SOURCE_TEXT = "Deliver the second client journey proof."
_BUDGET = {
    "usd": "10.00",
    "tokens": 1000,
    "wall_clock_seconds": 3600,
    "max_subagents": 4,
}


@dataclass(frozen=True)
class Capability:
    """A minted client capability and the config file the fixture consumes."""

    path: Path
    config_path: Path
    actor_id: UUID
    token: str


@dataclass
class World:
    config: OperationsConfig
    workspace_id: UUID
    project_id: UUID
    mission_id: UUID
    owner_id: UUID
    base_url: str
    platform: WorkClient
    intake_request: Path
    owner_key: Path
    _root: Path = field(repr=False)

    def mint(self, name: str) -> Capability:
        """Provision a client capability, plus the fixture config that names it."""
        path = provision_client(self.config, self.workspace_id, name)
        data = json.loads(path.read_text(encoding="utf-8"))
        config_path = self._root / f"{name}-client.json"
        config_path.write_text(
            json.dumps(
                {
                    "base_url": self.base_url,
                    "workspace_id": str(self.workspace_id),
                    "project_id": str(self.project_id),
                    "mission_id": str(self.mission_id),
                    "capability": str(path),
                    "contract_sha256": contract_sha256(),
                }
            ),
            encoding="utf-8",
        )
        return Capability(
            path=path,
            config_path=config_path,
            actor_id=UUID(str(data["actor_id"])),
            token=str(data["token"]),
        )

    def designate(self, actor_id: UUID) -> None:
        """Sign the designation message with the owner key and record the controller."""
        completed = subprocess.run(
            [
                "ssh-keygen",
                "-Y",
                "sign",
                "-f",
                str(self.owner_key),
                "-n",
                NAMESPACE,
            ],
            input=designation_message(self.workspace_id, actor_id),
            capture_output=True,
            check=True,
        )
        write_designation(
            self.config.config_dir,
            self.workspace_id,
            actor_id,
            completed.stdout.decode("ascii"),
        )

    def client(self, capability: Capability, *args: str) -> tuple[int, dict[str, object]]:
        """Run the stdlib fixture in isolation and return ``(exit_code, stdout_json)``."""
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(_SECOND_CLIENT),
                "--config",
                str(capability.config_path),
                *args,
            ],
            capture_output=True,
            text=True,
        )
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError:
            body = {"error": completed.stdout, "stderr": completed.stderr}
        if not isinstance(body, dict):
            body = {"result": body}
        return completed.returncode, body

    def run_journey(
        self, capability: Capability
    ) -> list[tuple[str, dict[str, object], str | None]]:
        """Walk the client journey, returning ``[(step, client_output, state)]``.

        ``client_output`` is the parsed JSON body the client printed (for
        platform steps, the command result). ``state`` is that body's ``state``
        field, or None when it has none. Platform steps run in the order the
        store's status transitions allow: the mission is approved by the
        client's confirm before it starts, and running before it completes.
        """
        journey: list[tuple[str, dict[str, object], str | None]] = []

        def client_step(name: str, *args: str) -> dict[str, object]:
            code, body = self.client(capability, *args)
            if code != 0:
                raise AssertionError(f"client {name} exited {code}: {body}")
            state = body.get("state")
            journey.append((name, body, state if isinstance(state, str) else None))
            return body

        def platform_step(name: str, envelope: CommandEnvelope) -> dict[str, object]:
            body = self.platform.execute(envelope).result.model_dump(mode="json")
            journey.append((name, body, None))
            return body

        client_step("projects", "projects")
        client_step("context", "context")

        submitted = client_step("submit", "submit", "--request-file", str(self.intake_request))
        drafted = submitted["result"]
        assert isinstance(drafted, dict)
        decision_id = str(drafted["decision_id"])
        mission = drafted["mission"]
        assert isinstance(mission, dict)
        revision = int(mission["revision"])

        client_step(
            "confirm",
            "confirm",
            "--mission-id",
            str(self.mission_id),
            "--decision-id",
            decision_id,
            "--revision",
            str(revision),
        )

        platform_step("set_mission_status", self._status_envelope("running"))

        raised = platform_step("create_decision", self._decision_envelope())
        raised_decision_id = str(raised["decision_id"])

        client_step("decisions", "decisions")
        client_step(
            "answer",
            "answer",
            "--decision-id",
            raised_decision_id,
            "--answer",
            "proceed",
        )
        client_step("status", "status", "--mission-id", str(self.mission_id))

        platform_step("record_finding", self._finding_envelope())
        platform_step("completion", self._status_envelope("completed"))

        client_step("status", "status", "--mission-id", str(self.mission_id))
        client_step("events", "events")
        return journey

    def _status_envelope(self, target: str) -> CommandEnvelope:
        return self._envelope(
            SetMissionStatusCommand(
                type="set_mission_status",
                payload=SetMissionStatusPayload(
                    mission_id=self.mission_id,
                    target_status=target,
                    cause_kind="principal",
                ),
            )
        )

    def _decision_envelope(self) -> CommandEnvelope:
        return self._envelope(
            CreateDecisionCommand(
                type="create_decision",
                payload=CreateDecisionPayload(
                    decision_id=uuid4(),
                    project_id=self.project_id,
                    mission_id=str(self.mission_id),
                    question="Continue the journey after the raised finding?",
                    why_it_matters="The next step needs the owner's explicit answer.",
                    risk_of_delay="The mission stalls until the decision is answered.",
                    options=("proceed", "abort"),
                    evidence_refs=(f"mission:{self.mission_id}",),
                    risk_of_each_choice={
                        "proceed": "The mission continues.",
                        "abort": "The mission stops without completing.",
                    },
                    action_class=None,
                    resume_state="running",
                ),
            )
        )

    def _finding_envelope(self) -> CommandEnvelope:
        return self._envelope(
            RecordFindingCommand(
                type="record_finding",
                payload=RecordFinding(
                    finding_id=uuid4(),
                    mission_id=self.mission_id,
                    severity="high",
                    title="Journey finding",
                    evidence_refs=(f"mission:{self.mission_id}#finding",),
                ),
            )
        )

    def _envelope(self, command: object) -> CommandEnvelope:
        return CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=self.workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=command,
        )


def _config(root: Path) -> OperationsConfig:
    config_dir = root / "xdg" / "omp" / "work-ledger"
    credentials = config_dir / "credentials"
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
    return OperationsConfig(
        config_dir=config_dir,
        state_dir=root / "state",
        data_dir=root / "data",
        port=_free_port(),
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _seed_project(
    config: OperationsConfig, workspace_id: UUID, project_id: UUID
) -> None:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(uuid4())),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Second client project",
                json.dumps({}),
            ),
        )


def _generate_owner_key(root: Path) -> Path:
    key = root / "owner-signing"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _write_allowed_signers(config_dir: Path, public_key: Path) -> Path:
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = config_dir / "owner_allowed_signers"
    path.write_text(f"owner {public_key.read_text().strip()}\n", encoding="utf-8")
    return path


def _write_intake_request(root: Path, project_id: UUID, mission_id: UUID) -> Path:
    path = root / "intake-request.json"
    path.write_text(
        json.dumps(
            {
                "mission_id": str(mission_id),
                "intake": {
                    "archetype": "small_code_change",
                    "source": {
                        "text": _SOURCE_TEXT,
                        "sha256": hashlib.sha256(_SOURCE_TEXT.encode("utf-8")).hexdigest(),
                        "spans": [],
                    },
                    "goal": {
                        "id": "goal-1",
                        "statement": _SOURCE_TEXT,
                        "source_span_ids": [],
                    },
                    "acceptance_criteria": [
                        {
                            "id": "ac-1",
                            "statement": "The journey reaches completion.",
                            "source_span_ids": [],
                            "observable_outcome": "Mission status reads completed.",
                            "oracle": "automated_test",
                        }
                    ],
                },
                "scope": {
                    "project_id": str(project_id),
                    "risk_policy": "risk-parent",
                    "approval_policy": "approval-parent",
                    "effort_policy": "effort-parent",
                    "budget_policy": dict(_BUDGET),
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _serve(app: object) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=_free_port(),
            log_level="warning",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, name="omp-work-world", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn world failed to start")
        time.sleep(0.02)
    return server, thread


@contextlib.contextmanager
def world(
    root: Path, *, push_hosts: Iterable[str] = ()
) -> Generator[World]:
    """Boot a disposable work-ledger world under ``root`` and serve it over HTTP."""
    root = Path(root)
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kwargs: None
        )
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()

        workspace_id, project_id = uuid4(), uuid4()
        mission_id, owner_id = uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)
        _seed_project(config, workspace_id, project_id)

        capabilities = capabilities_dir(config)
        owner_bearer = write_capability(
            config,
            "owner",
            actor_id=owner_id,
            actor_kind="owner",
            workspaces=(workspace_id,),
            scopes=OWNER_SCOPES,
        )
        owner_key = _generate_owner_key(root)
        _write_allowed_signers(config.config_dir, Path(f"{owner_key}.pub"))
        (config.config_dir / "push-destinations.json").write_text(
            json.dumps({"allowed_hosts": [str(host) for host in push_hosts]}),
            encoding="utf-8",
        )
        intake_request = _write_intake_request(root, project_id, mission_id)

        server, thread = _serve(create_app(config, capabilities_dir=capabilities))
        platform = WorkClient(
            f"http://127.0.0.1:{server.config.port}", workspace_id, owner_bearer
        )
        try:
            yield World(
                config=config,
                workspace_id=workspace_id,
                project_id=project_id,
                mission_id=mission_id,
                owner_id=owner_id,
                base_url=f"http://127.0.0.1:{server.config.port}",
                platform=platform,
                intake_request=intake_request,
                owner_key=owner_key,
                _root=root,
            )
        finally:
            platform.close()
            server.should_exit = True
            thread.join(timeout=15)
