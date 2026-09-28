"""OMP-406-s01-s01: record_alarm_signal command, models, scopes, and domain event persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

import omp_work
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.api_models import AlarmSignalResult, CommandResult
from omp_work.v1.models import (
    OWNER_APPROVAL_COMMAND_TYPES,
    OWNER_APPROVAL_REFUSED_EVENT,
    Command,
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres, seed_authority

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")


def test_alarm_signal_payload_valid() -> None:
    payload = RecordAlarmSignalPayload.model_validate(
        {
            "signal": "cost_threshold",
            "subject": "50% budget threshold reached",
            "detail": "Spent 500 of 1000 credits",
        }
    )
    assert payload.signal == "cost_threshold"
    assert payload.work_id is None
    assert payload.subject == "50% budget threshold reached"
    assert payload.detail == "Spent 500 of 1000 credits"


def test_alarm_signal_payload_bad_signal_rejected() -> None:
    with pytest.raises(ValidationError):
        RecordAlarmSignalPayload.model_validate(
            {"signal": "invalid_signal", "subject": "valid subject"}
        )


def test_alarm_signal_payload_empty_subject_rejected() -> None:
    with pytest.raises(ValidationError):
        RecordAlarmSignalPayload.model_validate(
            {"signal": "cost_threshold", "subject": ""}
        )
    with pytest.raises(ValidationError):
        RecordAlarmSignalPayload.model_validate(
            {"signal": "cost_threshold", "subject": "   "}
        )


def test_alarm_signal_payload_subject_length_bounds() -> None:
    with pytest.raises(ValidationError):
        RecordAlarmSignalPayload.model_validate(
            {"signal": "cost_threshold", "subject": "a" * 201}
        )

    valid_max = RecordAlarmSignalPayload.model_validate(
        {"signal": "cost_threshold", "subject": "a" * 200}
    )
    assert len(valid_max.subject) == 200

    stripped = RecordAlarmSignalPayload.model_validate(
        {"signal": "cost_threshold", "subject": "  warning alert  "}
    )
    assert stripped.subject == "warning alert"


def test_alarm_signal_payload_detail_length_bounds() -> None:
    with pytest.raises(ValidationError):
        RecordAlarmSignalPayload.model_validate(
            {
                "signal": "budget_exceeded",
                "subject": "Over budget",
                "detail": "d" * 501,
            }
        )

    valid_detail = RecordAlarmSignalPayload.model_validate(
        {
            "signal": "budget_exceeded",
            "subject": "Over budget",
            "detail": "d" * 500,
        }
    )
    assert len(valid_detail.detail) == 500

    default_detail = RecordAlarmSignalPayload.model_validate(
        {"signal": "safety_check_failed", "subject": "Check failed"}
    )
    assert default_detail.detail == ""


def test_record_alarm_signal_command_discrimination() -> None:
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": WORKSPACE,
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {
                "type": "record_alarm_signal",
                "payload": {
                    "signal": "credential_appeared",
                    "subject": "id_ed25519 found in ~/.ssh",
                },
            },
        }
    )
    assert isinstance(envelope.command, RecordAlarmSignalCommand)
    assert envelope.command.type == "record_alarm_signal"
    assert envelope.command.payload.signal == "credential_appeared"


def test_alarm_signal_result_discrimination() -> None:
    result = AlarmSignalResult.model_validate(
        {
            "type": "record_alarm_signal",
            "signal": "cost_threshold",
            "work_id": None,
            "subject": "Threshold alert",
            "detail": "diagnostic detail",
        }
    )
    assert result.type == "record_alarm_signal"
    assert result.signal == "cost_threshold"


def test_owner_approval_constants() -> None:
    assert OWNER_APPROVAL_COMMAND_TYPES == frozenset(
        {"attest_intake_admission", "publish_bounded_intake"}
    )
    assert OWNER_APPROVAL_REFUSED_EVENT == "owner_approval_refused"


def test_contract_lists_record_alarm_signal_command() -> None:
    contract = omp_work.load_contract()
    assert "record_alarm_signal" in contract.command_types
    assert "record_alarm_signal" in omp_work._COMMAND_TYPES


def test_record_alarm_signal_scope_is_work_mutate() -> None:
    assert WorkService._scopes["record_alarm_signal"] == "work.mutate"


def test_record_alarm_signal_scope_enforcement() -> None:
    service = WorkService(store=None)
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": WORKSPACE,
            "operation_id": uuid4(),
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "command": {
                "type": "record_alarm_signal",
                "payload": {
                    "signal": "cost_threshold",
                    "subject": "Approaching limit",
                },
            },
        }
    )
    principal = Principal(
        actor_id=uuid4(),
        actor_kind="owner",
        workspaces=frozenset({WORKSPACE}),
        scopes=frozenset({"work.read", "work.close"}),
    )
    with pytest.raises(WorkError) as exc:
        service.execute(principal, envelope)
    assert exc.value.code == "forbidden"
    assert exc.value.status == 403


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


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_record_alarm_signal_postgres_integration(
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

        store = PostgresWorkStore(config)
        service = WorkService(store)
        principal = Principal(
            actor_id=actor_id,
            actor_kind="owner",
            workspaces=frozenset({workspace_id}),
            scopes=frozenset({"work.read", "work.mutate"}),
        )

        # 1. Create a work item to get a valid work_id
        create_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=CreateWorkBatchCommand(
                type="create_work_batch",
                payload=CreateWorkBatchPayload(
                    items=(CreateWorkInput(client_ref="ref-1", title="test work"),)
                ),
            ),
        )
        _, batch_result = service.execute(principal, create_envelope)
        work_id = UUID(batch_result["items"][0]["work_id"])

        # 2. Record alarm signal with work_id
        op_id_with_work = uuid4()
        alarm_with_work_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=op_id_with_work,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordAlarmSignalCommand(
                type="record_alarm_signal",
                payload=RecordAlarmSignalPayload(
                    signal="cost_threshold",
                    work_id=work_id,
                    subject="80% budget threshold reached",
                    detail="Approaching hard cap",
                ),
            ),
        )
        receipt_with_work, result_with_work = service.execute(
            principal, alarm_with_work_envelope
        )
        assert result_with_work["type"] == "record_alarm_signal"
        assert result_with_work["signal"] == "cost_threshold"
        assert result_with_work["work_id"] == str(work_id)
        assert result_with_work["subject"] == "80% budget threshold reached"
        assert result_with_work["detail"] == "Approaching hard cap"

        events_page = service.events(principal, workspace_id)
        matching_with_work = [
            ev
            for ev in events_page["events"]
            if ev["operation_id"] == op_id_with_work
        ]
        assert len(matching_with_work) == 1
        ev1 = matching_with_work[0]
        assert ev1["event_type"] == "record_alarm_signal"
        assert ev1["aggregate_id"] == work_id
        assert ev1["aggregate_type"] == "work_item"
        assert ev1["outcome"] == "applied"
        assert ev1["payload"] == {
            "type": "record_alarm_signal",
            "signal": "cost_threshold",
            "work_id": str(work_id),
            "subject": "80% budget threshold reached",
            "detail": "Approaching hard cap",
        }

        # 3. Record alarm signal without work_id (aggregates to workspace_id)
        op_id_no_work = uuid4()
        alarm_no_work_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=op_id_no_work,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordAlarmSignalCommand(
                type="record_alarm_signal",
                payload=RecordAlarmSignalPayload(
                    signal="credential_appeared",
                    work_id=None,
                    subject="new authorized_keys line detected",
                    detail="key fingerprint sha256:abc",
                ),
            ),
        )
        receipt_no_work, result_no_work = service.execute(
            principal, alarm_no_work_envelope
        )
        assert result_no_work["type"] == "record_alarm_signal"
        assert result_no_work["signal"] == "credential_appeared"
        assert result_no_work["work_id"] is None
        assert result_no_work["subject"] == "new authorized_keys line detected"
        assert result_no_work["detail"] == "key fingerprint sha256:abc"

        events_page = service.events(principal, workspace_id)
        matching_no_work = [
            ev
            for ev in events_page["events"]
            if ev["operation_id"] == op_id_no_work
        ]
        assert len(matching_no_work) == 1
        ev2 = matching_no_work[0]
        assert ev2["event_type"] == "record_alarm_signal"
        assert ev2["aggregate_id"] == workspace_id
        assert ev2["aggregate_type"] == "workspace"
        assert ev2["outcome"] == "applied"
        assert ev2["payload"] == {
            "type": "record_alarm_signal",
            "signal": "credential_appeared",
            "work_id": None,
            "subject": "new authorized_keys line detected",
            "detail": "key fingerprint sha256:abc",
        }

        # 4. Unknown work_id raises invalid_request and records no event
        unknown_work_id = uuid4()
        op_id_unknown = uuid4()
        alarm_unknown_envelope = CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=op_id_unknown,
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordAlarmSignalCommand(
                type="record_alarm_signal",
                payload=RecordAlarmSignalPayload(
                    signal="safety_check_failed",
                    work_id=unknown_work_id,
                    subject="Safety check failed on absent item",
                    detail="missing work item",
                ),
            ),
        )
        with pytest.raises(WorkError) as exc_unknown:
            service.execute(principal, alarm_unknown_envelope)
        assert exc_unknown.value.code == "invalid_request"
        assert exc_unknown.value.status == 400
        assert exc_unknown.value.diagnostics == ("work_id:unknown",)

        events_page_after = service.events(principal, workspace_id)
        assert not any(
            ev["operation_id"] == op_id_unknown
            for ev in events_page_after["events"]
        )
