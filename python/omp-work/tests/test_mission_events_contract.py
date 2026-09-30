"""OMP-415: mission-event contract — commands, reads, views, and subscription rules.

The two GET routes validate the store page through MissionEventsPage /
EventSubscriptionsPage and return the JSON dump, so a store row with an
unknown event type or an empty event_types list is a 400. WorkClient reaches
both routes through the bound workspace and validates the pages.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

import omp_work
from omp_work import contract_sha256, load_contract
from omp_work.mission_events import derive_mission_events
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import (
    CommandResult,
    EventSubscriptionResult,
    EventSubscriptionView,
    EventSubscriptionsPage,
    FindingView,
    MissionEventView,
    MissionEventsPage,
    RecordFindingResult,
)
from omp_work.v1.client import WorkClient
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
    def __init__(
        self,
        *,
        mission_events_page: dict[str, object] | None = None,
        subscriptions_page: dict[str, object] | None = None,
    ) -> None:
        self.mission_events_calls: list[dict[str, object]] = []
        self.subscription_calls: list[dict[str, object]] = []
        self.mission_events_page = mission_events_page or _mission_events_page()
        self.subscriptions_page = subscriptions_page or {
            "subscriptions": [_subscription_view()]
        }

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
        return self.mission_events_page

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
        return self.subscriptions_page


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
    # The store's well-formed page comes back through the route unchanged.
    assert MissionEventsPage.model_validate(response.json()) == MissionEventsPage.model_validate(
        store.mission_events_page
    )
    assert response.json()["events"][0]["type"] == "mission.started"
    assert response.json()["next_after_sequence"] == 1
    assert store.mission_events_calls == [
        {
            "workspace_id": WORKSPACE,
            "actor_id": READER,
            "after": 4,
            "limit": 7,
            "mission_id": MISSION,
        }
    ]


def test_mission_events_page_with_unknown_type_is_400(tmp_path: Path) -> None:
    page = _mission_events_page()
    page["events"][0]["type"] = "mission.exploded"  # type: ignore[index]
    store = _RecordingStore(mission_events_page=page)
    client = _client(tmp_path, store)
    response = _get(client, f"/v1/workspaces/{WORKSPACE}/mission-events", "reader")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


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
    # The store's well-formed page comes back through the route unchanged.
    assert admin.json() == store.subscriptions_page
    assert EventSubscriptionsPage.model_validate(admin.json()).subscriptions[0].client_id == READER
    assert store.subscription_calls[-1]["client_id"] is None
    assert store.subscription_calls[-1]["actor_id"] == ADMIN

    reader = _get(client, route, "reader")
    assert reader.status_code == 200
    assert store.subscription_calls[-1]["client_id"] == READER

    named = _get(client, route, "reader", client_id=str(OTHER))
    assert named.status_code == 403
    assert named.json()["error"]["code"] == "forbidden"
    assert len(store.subscription_calls) == 2

    admin_named = _get(client, route, "admin", client_id=str(OTHER))
    assert admin_named.status_code == 200
    assert store.subscription_calls[-1]["client_id"] == OTHER


def test_event_subscriptions_page_with_empty_event_types_is_400(tmp_path: Path) -> None:
    store = _RecordingStore(
        subscriptions_page={"subscriptions": [_subscription_view(event_types=[])]}
    )
    client = _client(tmp_path, store)
    response = _get(
        client, f"/v1/workspaces/{WORKSPACE}/event-subscriptions", "reader"
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


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


def _subscription_view(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "subscription_id": str(SUBSCRIPTION),
        "client_id": str(READER),
        "push_url": "https://hooks.example/omp",
        "event_types": ["mission.started", "important_finding"],
        "cursor_sequence": 0,
        "deleted": False,
    }
    base.update(overrides)
    return base


def _mission_event_view(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "mission_event_id": str(SUBSCRIPTION),
        "sequence": 1,
        "mission_id": str(MISSION),
        "type": "mission.started",
        "trigger": "status:approved->running",
        "occurred_at": "2026-09-30T12:00:00+00:00",
        "source_event_id": str(SUBSCRIPTION),
        "evidence_refs": [
            {"kind": "domain_event", "ref": str(SUBSCRIPTION)},
        ],
    }
    base.update(overrides)
    return base


def _mission_events_page(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "events": [_mission_event_view()],
        "watermark_sequence": 1,
        "next_after_sequence": 1,
        "has_more": False,
    }
    base.update(overrides)
    return base


def test_each_result_validates_as_command_result() -> None:
    adapter = TypeAdapter(CommandResult)
    finding = adapter.validate_python({"type": "record_finding", "finding": _finding()})
    assert isinstance(finding, RecordFindingResult)
    assert finding.finding.evidence_refs == ("receipt:abc",)

    for command_type in (
        "put_event_subscription",
        "delete_event_subscription",
        "advance_event_cursor",
    ):
        parsed = adapter.validate_python(
            {"type": command_type, "subscription": _subscription_view()}
        )
        assert isinstance(parsed, EventSubscriptionResult)
        assert parsed.type == command_type
        assert parsed.subscription.cursor_sequence == 0
        assert parsed.subscription.deleted is False


def test_finding_view_rejects_object_refs_missing_refs_bad_severity_and_long_title() -> None:
    with pytest.raises(ValidationError):
        FindingView.model_validate(
            _finding(evidence_refs=({"kind": "evidence", "ref": "receipt:abc"},))
        )
    missing = _finding()
    missing.pop("evidence_refs")
    with pytest.raises(ValidationError):
        FindingView.model_validate(missing)
    with pytest.raises(ValidationError):
        FindingView.model_validate(_finding(severity="severe"))
    with pytest.raises(ValidationError):
        FindingView.model_validate(_finding(title="x" * 201))
    accepted = FindingView.model_validate(_finding(title="x" * 200, severity="critical"))
    assert accepted.title == "x" * 200
    assert accepted.severity == "critical"


def test_event_subscription_view_rejects_empty_unknown_and_mixed_ops() -> None:
    with pytest.raises(ValidationError):
        EventSubscriptionView.model_validate(_subscription_view(event_types=[]))
    with pytest.raises(ValidationError):
        EventSubscriptionView.model_validate(_subscription_view(event_types=["not-an-event"]))
    with pytest.raises(ValidationError):
        EventSubscriptionView.model_validate(
            _subscription_view(event_types=[*MISSION_EVENT_TYPES, "ops.alarm"])
        )
    with pytest.raises(ValidationError):
        EventSubscriptionView.model_validate(
            _subscription_view(event_types=["ops.alarm", "ops.digest"])
        )
    alarm = EventSubscriptionView.model_validate(
        _subscription_view(event_types=["ops.alarm"], push_url=None)
    )
    assert alarm.event_types == ("ops.alarm",)
    assert alarm.push_url is None
    page = EventSubscriptionsPage.model_validate({"subscriptions": [_subscription_view()]})
    assert page.subscriptions[0].subscription_id == SUBSCRIPTION


def test_derived_mission_event_validates_as_mission_event_view() -> None:
    event_id = str(uuid4())
    event = {
        "event_id": event_id,
        "sequence": 1,
        "outcome": "applied",
        "event_type": "set_mission_status",
        "occurred_at": datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
        "payload": {
            "mission": {
                "mission_id": str(MISSION),
                "transitions": [{"from_status": "approved", "to_status": "running"}],
            }
        },
    }
    rows = derive_mission_events([event], mission_for_work=lambda _work_id, _sequence: None)
    assert len(rows) == 1
    view = MissionEventView.model_validate(rows[0])
    assert view.type == "mission.started"
    assert view.sequence == 1
    assert view.mission_id == MISSION
    assert view.source_event_id == UUID(event_id)
    assert view.trigger == "status:approved->running"
    assert view.evidence_refs[0].kind == "domain_event"
    assert view.evidence_refs[0].ref == event_id
    page = MissionEventsPage.model_validate(
        {
            "events": rows,
            "watermark_sequence": 1,
            "next_after_sequence": 1,
            "has_more": False,
        }
    )
    assert page.events == (view,)


def _bearer(tmp_path: Path) -> Path:
    path = tmp_path / "bearer.json"
    path.write_text(json.dumps({"token": "events-token"}))
    path.chmod(0o600)
    return path


def test_work_client_mission_events_sends_bounded_query(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_mission_events_page())

    client = WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        _bearer(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    page = client.mission_events(after_sequence=3, limit=10, mission_id=MISSION)
    assert isinstance(page, MissionEventsPage)
    assert page.events[0].type == "mission.started"
    assert len(seen) == 1
    assert seen[0].url.path == f"/v1/workspaces/{WORKSPACE}/mission-events"
    assert dict(seen[0].url.params) == {
        "after_sequence": "3",
        "limit": "10",
        "mission_id": str(MISSION),
    }


def test_work_client_mission_events_omits_mission_id_when_none(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_mission_events_page())

    WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        _bearer(tmp_path),
        transport=httpx.MockTransport(handler),
    ).mission_events()
    assert dict(seen[0].url.params) == {"after_sequence": "0", "limit": "500"}


def test_work_client_event_subscriptions_sends_no_client_id_by_default(
    tmp_path: Path,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"subscriptions": [_subscription_view()]})

    client = WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        _bearer(tmp_path),
        transport=httpx.MockTransport(handler),
    )
    page = client.event_subscriptions()
    assert isinstance(page, EventSubscriptionsPage)
    assert page.subscriptions[0].subscription_id == SUBSCRIPTION
    assert seen[0].url.path == f"/v1/workspaces/{WORKSPACE}/event-subscriptions"
    assert dict(seen[0].url.params) == {}


def test_work_client_event_subscriptions_sends_named_client_id(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"subscriptions": [_subscription_view()]})

    WorkClient(
        "http://127.0.0.1:54322",
        WORKSPACE,
        _bearer(tmp_path),
        transport=httpx.MockTransport(handler),
    ).event_subscriptions(client_id=OTHER)
    assert dict(seen[0].url.params) == {"client_id": str(OTHER)}
