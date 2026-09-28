from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
from uuid import UUID, uuid4

import pytest
from starlette.testclient import TestClient

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    AttestIntakeAdmissionCommand,
    AttestIntakeAdmissionPayload,
    CommandEnvelope,
    FocusSlot,
    SetFocusCommand,
    SetFocusPayload,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
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


def _bearer(
    directory: Path,
    name: str,
    *,
    token: str,
    actor_id: UUID,
    actor_kind: str,
    workspaces: list[UUID],
    scopes: list[str],
) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": actor_kind,
                "workspaces": [str(workspace) for workspace in workspaces],
                "scopes": scopes,
            }
        )
    )
    path.chmod(0o600)
    return path


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


def _refused(client: WorkClient, envelope: CommandEnvelope) -> WorkError:
    with pytest.raises(WorkError) as caught:
        client.execute(envelope)
    return caught.value


def test_refused_owner_approval_attempts_are_domain_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        workspace_id, owner_id = uuid4(), uuid4()
        limited_id, narrow_id, outsider_id = uuid4(), uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)

        capabilities = tmp_path / "capabilities"
        owner_file = _bearer(
            capabilities,
            "owner.json",
            token="owner-token",
            actor_id=owner_id,
            actor_kind="owner",
            workspaces=[workspace_id],
            scopes=["work.read", "work.mutate", "work.approve"],
        )
        limited_file = _bearer(
            capabilities,
            "limited.json",
            token="limited-token",
            actor_id=limited_id,
            actor_kind="task-agent",
            workspaces=[workspace_id],
            scopes=["work.read", "work.mutate"],
        )
        narrow_file = _bearer(
            capabilities,
            "narrow.json",
            token="narrow-token",
            actor_id=narrow_id,
            actor_kind="task-agent",
            workspaces=[workspace_id],
            scopes=["work.read"],
        )
        outsider_file = _bearer(
            capabilities,
            "outsider.json",
            token="outsider-token",
            actor_id=outsider_id,
            actor_kind="owner",
            workspaces=[uuid4()],
            scopes=["work.read", "work.mutate", "work.approve"],
        )

        app = create_app(config, capabilities_dir=capabilities)
        tc = TestClient(app)
        owner = WorkClient(
            "http://testserver",
            workspace_id,
            owner_file,
            transport=tc._transport,
        )
        limited = WorkClient(
            "http://testserver",
            workspace_id,
            limited_file,
            transport=tc._transport,
        )
        narrow = WorkClient(
            "http://testserver",
            workspace_id,
            narrow_file,
            transport=tc._transport,
        )
        outsider = WorkClient(
            "http://testserver",
            workspace_id,
            outsider_file,
            transport=tc._transport,
        )

        def event_ids() -> set[UUID]:
            return {event.event_id for event in owner.events().events}

        forbidden_work_id = uuid4()
        before = event_ids()
        forbidden = _refused(limited, _attest(workspace_id, forbidden_work_id))
        assert forbidden.code == "forbidden"
        assert forbidden.status == 403
        refused_scope = [
            event
            for event in owner.events().events
            if event.event_id not in before
        ]
        assert len(refused_scope) == 1
        scope_event = refused_scope[0]
        assert scope_event.event_type == "owner_approval_refused"
        assert scope_event.outcome == "refused"
        assert scope_event.aggregate_id == forbidden_work_id
        assert scope_event.actor_id == limited_id
        assert scope_event.actor_kind == "task-agent"
        assert scope_event.payload == {
            "type": "owner_approval_refused",
            "status": "refused",
            "command_type": "attest_intake_admission",
            "code": "forbidden",
            "diagnostics": [],
        }

        blocked_work_id = uuid4()
        before = event_ids()
        blocked = _refused(owner, _attest(workspace_id, blocked_work_id))
        assert blocked.code == "intake_admission_blocked"
        assert blocked.status == 409
        refused_blocked = [
            event
            for event in owner.events().events
            if event.event_id not in before
        ]
        assert len(refused_blocked) == 1
        blocked_event = refused_blocked[0]
        assert blocked_event.event_type == "owner_approval_refused"
        assert blocked_event.outcome == "refused"
        assert blocked_event.aggregate_id == blocked_work_id
        assert blocked_event.payload["command_type"] == "attest_intake_admission"
        assert blocked_event.payload["code"] == "intake_admission_blocked"
        assert blocked_event.payload["diagnostics"] == [
            "OMP-249 current revision and final candidate do not match",
        ]

        before = event_ids()
        focus = _refused(
            narrow,
            CommandEnvelope(
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
                            owner_id=narrow_id,
                            version=0,
                        ),
                        expected_version=0,
                    ),
                ),
            ),
        )
        assert focus.code == "forbidden"
        assert focus.status == 403
        assert event_ids() == before

        before = event_ids()
        outside = _refused(outsider, _attest(workspace_id, uuid4()))
        assert outside.code == "forbidden"
        assert outside.status == 403
        assert event_ids() == before

        recorded = owner.events().events
        assert [event.event_id for event in recorded] == [
            scope_event.event_id,
            blocked_event.event_id,
        ]
        assert [event.event_type for event in recorded] == [
            "owner_approval_refused",
            "owner_approval_refused",
        ]

        store = PostgresWorkStore(config)
        service = WorkService(store)

        def record_failed(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("record failed")

        monkeypatch.setattr(store, "record_refused_attempt", record_failed)
        missing_scope = Principal(
            actor_id=limited_id,
            actor_kind="task-agent",
            workspaces=frozenset({workspace_id}),
            scopes=frozenset({"work.read", "work.mutate"}),
        )
        with pytest.raises(RuntimeError, match="record failed"):
            service.execute(missing_scope, _attest(workspace_id, uuid4()))

        def execute_failed(*_args: object, **_kwargs: object) -> None:
            raise WorkStoreError("intake_admission_blocked", ("blocked",))

        monkeypatch.setattr(store, "execute", execute_failed)
        approver = Principal(
            actor_id=owner_id,
            actor_kind="owner",
            workspaces=frozenset({workspace_id}),
            scopes=frozenset({"work.read", "work.mutate", "work.approve"}),
        )
        with pytest.raises(RuntimeError, match="record failed"):
            service.execute(approver, _attest(workspace_id, uuid4()))
