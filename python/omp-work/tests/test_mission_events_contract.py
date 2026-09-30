"""OMP-415: mission-event contract — commands, reads, and subscription rules.

The two GET routes return the store dict as the store wrote it. Page views
land in a later slice.
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
from omp_work.v1.models import (
    MISSION_EVENT_TYPES,
    AdvanceEventCursor,
    AdvanceEventCursorCommand,
    CommandEnvelope,
    DeleteEventSubscriptionCommand,
    PutEventSubscription,
    PutEventSubscriptionCommand,
    RecordFinding,
    RecordFindingCommand,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkService
from omp_work.v1.store import PostgresWorkStore, WorkStoreError

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
READER = UUID("00000000-0000-7000-8000-0000000000a1")
ADMIN = UUID("00000000-0000-7000-8000-0000000000a2")
PLAIN = UUID("00000000-0000-7000-8000-0000000000a3")
OTHER = UUID("00000000-0000-7000-8000-0000000000a4")
MISSION = UUID("00000000-0000-7000-8000-0000000000b1")
FINDING = UUID("00000000-0000-7000-8000-0000000000b2")
SUBSCRIPTION = UUID("00000000-0000-7000-8000-0000000000b3")

MISSION_EVENTS_READ = "GET /v1/workspaces/{workspace_id}/mission-events"
EVENT_SUBSCRIPTIONS_READ = "GET /v1/workspaces/{workspace_id}/event-subscriptions"
_FK_READS = (
    "GET /v1/work-items/{key}/revisions/{selector}",
    "GET /v1/receipts/{receipt_id}",
    "GET /v1/workspaces/{workspace_id}/work-items",
    "GET /v1/workspaces/{workspace_id}/events",
)
_COMMANDS = (
    "record_finding",
    "put_event_subscription",
    "delete_event_subscription",
    "advance_event_cursor",
)


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "finding_id": str(FINDING),
        "mission_id": str(MISSION),
        "severity": "high",
        "title": "Cache stampede on the read path",
        "evidence_refs": ("receipt:abc",),
    }
    base.update(overrides)
    return base


def _subscription(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subscription_id": str(SUBSCRIPTION),
        "client_id": str(READER),
        "push_url": "https://hooks.example/omp",
        "event_types": ["mission.started", "important_finding"],
    }
    base.update(overrides)
    return base


def _envelope(command: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


def test_event_types_reject_empty_unknown_mixed_ops_and_duplicates() -> None:
    with pytest.raises(ValidationError):
        PutEventSubscription.model_validate(_subscription(event_types=[]))
    with pytest.raises(ValidationError):
        PutEventSubscription.model_validate(_subscription(event_types=["not-an-event"]))
    with pytest.raises(ValidationError):
        PutEventSubscription.model_validate(
            _subscription(event_types=[*MISSION_EVENT_TYPES, "ops.alarm"])
        )
    with pytest.raises(ValidationError):
        PutEventSubscription.model_validate(
            _subscription(event_types=["ops.alarm", "ops.digest"])
        )
    with pytest.raises(ValidationError):
        PutEventSubscription.model_validate(
            _subscription(event_types=["mission.started", "mission.started"])
        )
    alarm = PutEventSubscription.model_validate(
        _subscription(event_types=["ops.alarm"], push_url=None, client_id=None)
    )
    assert alarm.event_types == ("ops.alarm",)
    digest = PutEventSubscription.model_validate(_subscription(event_types=["ops.digest"]))
    assert digest.event_types == ("ops.digest",)
    every = PutEventSubscription.model_validate(
        _subscription(event_types=list(MISSION_EVENT_TYPES))
    )
    assert every.event_types == MISSION_EVENT_TYPES


def test_record_finding_rejects_missing_refs_too_many_refs_and_bad_severity() -> None:
    missing = _finding()
    missing.pop("evidence_refs")
    with pytest.raises(ValidationError):
        RecordFinding.model_validate(missing)
    with pytest.raises(ValidationError):
        RecordFinding.model_validate(_finding(evidence_refs=tuple("r" for _ in range(21))))
    with pytest.raises(ValidationError):
        RecordFinding.model_validate(_finding(severity="severe"))


def test_advance_cursor_rejects_negative_after_sequence() -> None:
    with pytest.raises(ValidationError):
        AdvanceEventCursor.model_validate(
            {"subscription_id": str(SUBSCRIPTION), "after_sequence": -1}
        )


def test_each_command_parses_in_a_command_envelope() -> None:
    finding = _envelope({"type": "record_finding", "payload": _finding()})
    assert isinstance(finding.command, RecordFindingCommand)
    assert finding.command.payload.severity == "high"
    assert finding.command.payload.evidence_refs == ("receipt:abc",)

    subscription = _envelope(
        {"type": "put_event_subscription", "payload": _subscription()}
    )
    assert isinstance(subscription.command, PutEventSubscriptionCommand)
    assert subscription.command.payload.event_types == (
        "mission.started",
        "important_finding",
    )
    assert subscription.command.payload.push_url == "https://hooks.example/omp"

    deleted = _envelope(
        {
            "type": "delete_event_subscription",
            "payload": {"subscription_id": str(SUBSCRIPTION)},
        }
    )
    assert isinstance(deleted.command, DeleteEventSubscriptionCommand)
    assert deleted.command.payload.subscription_id == SUBSCRIPTION

    cursor = _envelope(
        {
            "type": "advance_event_cursor",
            "payload": {"subscription_id": str(SUBSCRIPTION), "after_sequence": 0},
        }
    )
    assert isinstance(cursor.command, AdvanceEventCursorCommand)
    assert cursor.command.payload.after_sequence == 0


def test_contract_closures_place_the_reads_after_ready_and_before_fk() -> None:
    contract = load_contract()
    ready = contract.reads.index("GET /v1/health/ready")
    assert contract.reads[ready + 1 : ready + 3] == (
        MISSION_EVENTS_READ,
        EVENT_SUBSCRIPTIONS_READ,
    )
    assert contract.reads[ready + 3 : ready + 7] == _FK_READS
    assert contract.reads[-4:] == _FK_READS
    for read in (MISSION_EVENTS_READ, EVENT_SUBSCRIPTIONS_READ):
        assert read in omp_work._READS
        assert read in contract.reads
    for command in _COMMANDS:
        assert command in omp_work._COMMAND_TYPES
        assert command in contract.command_types
    assert "work.events.admin" in omp_work._SCOPES
    assert "work.events.admin" in contract.scopes
    assert "work.events.admin" not in contract.security_policy.owner_host_scopes


def test_scope_mapping() -> None:
    assert WorkService._scopes["record_finding"] == "work.execute"
    assert WorkService._scopes["put_event_subscription"] == "work.read"
    assert WorkService._scopes["delete_event_subscription"] == "work.read"
    assert WorkService._scopes["advance_event_cursor"] == "work.read"


class _RecordingStore:
    def __init__(self) -> None:
        self.mission_events_calls: list[dict[str, object]] = []
        self.subscription_calls: list[dict[str, object]] = []

    def mission_events(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        *,
        after: int,
        limit: int,
        mission_id: UUID | None,
    ) -> dict[str, object]:
        self.mission_events_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "after": after,
                "limit": limit,
                "mission_id": mission_id,
            }
        )
        return {"mission_events": [{"raw": True}], "after": after}

    def event_subscriptions(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        *,
        client_id: UUID | None,
    ) -> dict[str, object]:
        self.subscription_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "client_id": client_id,
            }
        )
        return {
            "subscriptions": [{"raw": True}],
            "client_id": None if client_id is None else str(client_id),
        }


def _capabilities(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, actor_id, scopes in (
        ("reader", READER, ["work.read"]),
        ("admin", ADMIN, ["work.read", "work.events.admin"]),
        ("plain", PLAIN, ["work.mutate"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": f"{name}-token",
                    "actor_id": str(actor_id),
                    "actor_kind": "automation",
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _client(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(config, capabilities_dir=_capabilities(tmp_path), store=store)  # type: ignore[arg-type]
    )


def _get(client: TestClient, path: str, token: str, **params: str):
    return client.get(
        path,
        params=params,
        headers={
            "Authorization": f"Bearer {token}-token",
            "X-OMP-Contract-SHA256": contract_sha256(),
        },
    )


def test_mission_events_route_forwards_filters(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)
    response = _get(
        client,
        f"/v1/workspaces/{WORKSPACE}/mission-events",
        "reader",
        after_sequence="4",
        limit="7",
        mission_id=str(MISSION),
    )
    assert response.status_code == 200
    assert response.json() == {"mission_events": [{"raw": True}], "after": 4}
    assert store.mission_events_calls == [
        {
            "workspace_id": WORKSPACE,
            "actor_id": READER,
            "after": 4,
            "limit": 7,
            "mission_id": MISSION,
        }
    ]


def test_mission_events_route_rejects_limit_zero_and_missing_read_scope(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)
    route = f"/v1/workspaces/{WORKSPACE}/mission-events"
    bad_limit = _get(client, route, "reader", limit="0")
    assert bad_limit.status_code == 400
    assert bad_limit.json()["error"]["code"] == "invalid_request"
    refused = _get(client, route, "plain")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "forbidden"
    assert store.mission_events_calls == []


def test_event_subscriptions_route_forwards_admin_none_and_own_actor(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _client(tmp_path, store)
    route = f"/v1/workspaces/{WORKSPACE}/event-subscriptions"

    admin = _get(client, route, "admin")
    assert admin.status_code == 200
    assert admin.json()["client_id"] is None
    assert store.subscription_calls[-1]["client_id"] is None
    assert store.subscription_calls[-1]["actor_id"] == ADMIN

    reader = _get(client, route, "reader")
    assert reader.status_code == 200
    assert reader.json()["client_id"] == str(READER)
    assert store.subscription_calls[-1]["client_id"] == READER

    named = _get(client, route, "reader", client_id=str(OTHER))
    assert named.status_code == 403
    assert named.json()["error"]["code"] == "forbidden"
    assert len(store.subscription_calls) == 2

    admin_named = _get(client, route, "admin", client_id=str(OTHER))
    assert admin_named.status_code == 200
    assert store.subscription_calls[-1]["client_id"] == OTHER


def test_postgres_work_store_reads_are_unavailable(tmp_path: Path) -> None:
    store = PostgresWorkStore(
        OperationsConfig(
            config_dir=tmp_path / "config",
            state_dir=tmp_path / "state",
            data_dir=tmp_path / "data",
        )
    )
    with pytest.raises(WorkStoreError) as mission:
        store.mission_events(WORKSPACE, READER, after=0, limit=1, mission_id=None)
    assert mission.value.code == "unavailable"
    with pytest.raises(WorkStoreError) as subscriptions:
        store.event_subscriptions(WORKSPACE, READER, client_id=None)
    assert subscriptions.value.code == "unavailable"
