"""OMP-405: agent stop control — contract closure, scopes, and the stop read.

Without a database: the contract closures must name the new read/commands/
scope/error code, the service must scope ``engage_stop`` to ``work.stop``,
``release_stop`` to ``work.approve`` and owner-only, and
``GET /v1/workspaces/{workspace_id}/stop`` must serve a ``work.stop``-only
grokbot and a ``work.read`` owner while refusing a principal with neither.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import omp_work
from omp_work import contract_sha256, load_contract
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import StopStatusView
from omp_work.v1.models import (
    OWNER_APPROVAL_COMMAND_TYPES,
    CommandEnvelope,
    EngageStopCommand,
    ReleaseStopCommand,
    StopReasonPayload,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.models import OperationReceipt, OperationState

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")


class _RecordingStore:
    """Fake WorkStore: records scope/actor and derives stop state from commands."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.stopped = False
        self.reason: str | None = None

    def execute(
        self, envelope, *, actor_id, actor_kind, required_scope
    ) -> tuple[OperationReceipt, dict[str, object]]:
        self.calls.append(
            {
                "command_type": envelope.command.type,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
            }
        )
        if envelope.command.type == "engage_stop":
            self.stopped, self.reason = True, envelope.command.payload.reason
        elif envelope.command.type == "release_stop":
            self.stopped, self.reason = False, envelope.command.payload.reason
        return (
            OperationReceipt(
                operation_id=envelope.operation_id,
                request_id=envelope.request_id,
                state=OperationState.APPLIED,
                request_sha256="0" * 64,
                result_sha256="1" * 64,
            ),
            {
                "type": envelope.command.type,
                "stopped": self.stopped,
                "reason": self.reason,
            },
        )

    def stop_status(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        return {
            "workspace_id": str(workspace_id),
            "stopped": self.stopped,
            "reason": self.reason,
            "changed_at": "2026-09-29T00:00:00+00:00" if self.reason else None,
            "changed_by_actor_kind": "grokbot" if self.reason else None,
        }


def _principal(principal_kind: str, scopes: frozenset[str]) -> Principal:
    return Principal(
        actor_id=uuid4(),
        actor_kind=principal_kind,
        workspaces=frozenset({WORKSPACE}),
        scopes=scopes,
    )


def _envelope(command_type: str, reason: str) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": {"type": command_type, "payload": {"reason": reason}},
        }
    )


def test_contract_closures_name_the_stop_read_commands_scope_and_error() -> None:
    contract = load_contract()
    stop_read = "GET /v1/workspaces/{workspace_id}/stop"
    assert stop_read in omp_work._READS
    assert stop_read in contract.reads
    # The read is declared before health/live, so the FK-1 tail stays pinned.
    assert contract.reads[contract.reads.index(stop_read) :][:3] == (
        stop_read,
        "GET /v1/health/live",
        "GET /v1/health/ready",
    )
    for command in ("engage_stop", "release_stop"):
        assert command in omp_work._COMMAND_TYPES
        assert command in contract.command_types
    assert "work.stop" in omp_work._SCOPES
    assert "work.stop" in contract.scopes
    assert "agent_stop_engaged" in omp_work._ERROR_CODES
    assert "agent_stop_engaged" in contract.error_codes
    assert "work.stop" in contract.security_policy.owner_host_scopes
    assert contract.security_policy.stop_client_scopes == ("work.stop",)


def test_stop_scope_mapping_and_owner_only_release_constant() -> None:
    assert WorkService._scopes["engage_stop"] == "work.stop"
    assert WorkService._scopes["release_stop"] == "work.approve"
    # release_stop is owner-gated in the service, not by the approval constant.
    assert "release_stop" not in OWNER_APPROVAL_COMMAND_TYPES


def test_envelope_discriminates_stop_commands() -> None:
    engage = CommandEnvelope.model_validate(
        json.loads(_envelope("engage_stop", "runaway loop").model_dump_json())
    )
    assert isinstance(engage.command, EngageStopCommand)
    assert engage.command.payload.reason == "runaway loop"
    release = CommandEnvelope.model_validate(
        json.loads(_envelope("release_stop", "all clear").model_dump_json())
    )
    assert isinstance(release.command, ReleaseStopCommand)


def test_stop_reason_payload_bounds() -> None:
    assert StopReasonPayload(reason="x" * 500).reason == "x" * 500
    with pytest.raises(ValidationError):
        StopReasonPayload(reason="")
    with pytest.raises(ValidationError):
        StopReasonPayload(reason="x" * 501)


def test_grokbot_engages_but_only_the_owner_releases() -> None:
    store = _RecordingStore()
    service = WorkService(store)  # type: ignore[arg-type]
    grokbot = _principal("client", frozenset({"work.stop"}))

    _, result = service.execute(grokbot, _envelope("engage_stop", "runaway"))
    assert result == {"type": "engage_stop", "stopped": True, "reason": "runaway"}
    assert store.calls[-1]["required_scope"] == "work.stop"

    with pytest.raises(WorkError) as denied:
        service.execute(grokbot, _envelope("release_stop", "clear"))
    assert denied.value.code == "forbidden"
    assert denied.value.status == 403

    owner = _principal("owner", frozenset({"work.approve"}))
    _, released = service.execute(owner, _envelope("release_stop", "clear"))
    assert released == {"type": "release_stop", "stopped": False, "reason": "clear"}
    assert store.calls[-1]["required_scope"] == "work.approve"


def test_stop_status_requires_work_read_or_work_stop() -> None:
    service = WorkService(_RecordingStore())  # type: ignore[arg-type]

    with pytest.raises(WorkError) as denied:
        service.stop_status(_principal("owner", frozenset({"work.mutate"})), WORKSPACE)
    assert denied.value.code == "forbidden"
    assert denied.value.status == 403

    for scopes in (frozenset({"work.read"}), frozenset({"work.stop"})):
        view = service.stop_status(_principal("caller", scopes), WORKSPACE)
        assert StopStatusView.model_validate(view).stopped is False
        assert view["reason"] is None


def _capabilities_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_kind, scopes in (
        ("grokbot", "client", ["work.stop"]),
        ("owner", "owner", ["work.read"]),
        ("plain", "automation", ["work.mutate"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(uuid4()),
                    "actor_kind": actor_kind,
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def test_stop_read_serves_stop_and_read_scopes_and_refuses_others(
    tmp_path: Path,
) -> None:
    capabilities = _capabilities_dir(tmp_path)
    store = _RecordingStore()
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    client = TestClient(
        create_app(config, capabilities_dir=capabilities, store=store)  # type: ignore[arg-type]
    )
    route = f"/v1/workspaces/{WORKSPACE}/stop"

    def get(token: str):
        return client.get(
            route,
            headers={
                "Authorization": f"Bearer {token}-token",
                "X-OMP-Contract-SHA256": contract_sha256(),
                "X-OMP-Workspace-ID": str(WORKSPACE),
            },
        )

    stop_only = get("grokbot")
    assert stop_only.status_code == 200
    assert StopStatusView.model_validate(stop_only.json()).stopped is False

    read_owner = get("owner")
    assert read_owner.status_code == 200

    refused = get("plain")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"


def test_stop_read_reflects_engaged_state(tmp_path: Path) -> None:
    capabilities = _capabilities_dir(tmp_path)
    store = _RecordingStore()
    store.stopped, store.reason = True, "emergency halt"
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    client = TestClient(
        create_app(config, capabilities_dir=capabilities, store=store)  # type: ignore[arg-type]
    )
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/stop",
        headers={
            "Authorization": "Bearer grokbot-token",
            "X-OMP-Contract-SHA256": contract_sha256(),
            "X-OMP-Workspace-ID": str(WORKSPACE),
        },
    )
    assert response.status_code == 200
    view = StopStatusView.model_validate(response.json())
    assert view.stopped is True
    assert view.reason == "emergency halt"
    assert view.changed_by_actor_kind == "grokbot"
    assert view.changed_at is not None
