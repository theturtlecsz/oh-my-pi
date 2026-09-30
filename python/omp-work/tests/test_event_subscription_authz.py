"""OMP-415-s06: who may change a subscription, and which push URLs are refused.

A non-admin principal is 403 when it names another client or touches that
client's subscription, and the store is not executed. work.events.admin,
including the event-push capability, may. A push_url is 400 unless the
destination check accepts it.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import omp_work.event_push as event_push
from omp_work import contract_sha256
from omp_work.event_push import check_destination, load_allowed_hosts
from omp_work.operations.capabilities import EVENT_PUSH_SCOPES, OWNER_SCOPES
from omp_work.operations.config import OperationsConfig
from omp_work.v1.models import CommandEnvelope, OperationReceipt, OperationState
from omp_work.v1.server import create_app
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store_shared import WorkStoreError
from omp_work.__main__ import main

WORKSPACE = UUID("00000000-0000-7000-8000-000000000410")
CLIENT_A = UUID("00000000-0000-7000-8000-0000000004a1")
CLIENT_B = UUID("00000000-0000-7000-8000-0000000004b2")
ADMIN = UUID("00000000-0000-7000-8000-0000000004ad")
SUB_B = UUID("00000000-0000-7000-8000-0000000004c1")
SUB_A = UUID("00000000-0000-7000-8000-0000000004c2")


def _subscription(subscription_id: UUID, client_id: UUID) -> dict[str, object]:
    return {
        "subscription_id": str(subscription_id),
        "client_id": str(client_id),
        "push_url": None,
        "event_types": ["mission.started"],
        "cursor_sequence": 0,
        "deleted": False,
    }


class _Store:
    def __init__(self, subscriptions: list[dict[str, object]] | None = None) -> None:
        self.subscriptions = {
            str(item["subscription_id"]): dict(item) for item in (subscriptions or [])
        }
        self.executed: list[dict[str, object]] = []
        self.listed: list[UUID | None] = []

    def event_subscriptions(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        *,
        client_id: UUID | None,
    ) -> dict[str, object]:
        self.listed.append(client_id)
        rows = []
        for item in self.subscriptions.values():
            if item.get("deleted") is True:
                continue
            if client_id is not None and str(item["client_id"]) != str(client_id):
                continue
            rows.append(dict(item))
        return {"subscriptions": rows}

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        command = envelope.command
        sub_id = str(command.payload.subscription_id)
        self.executed.append(
            {
                "type": command.type,
                "subscription_id": command.payload.subscription_id,
                "actor_id": actor_id,
                "actor_kind": actor_kind,
            }
        )
        if command.type == "put_event_subscription":
            existing = self.subscriptions.get(sub_id)
            client = (
                existing["client_id"]
                if existing is not None
                else str(command.payload.client_id or actor_id)
            )
            record = {
                "subscription_id": sub_id,
                "client_id": str(client),
                "push_url": command.payload.push_url,
                "event_types": list(command.payload.event_types),
                "cursor_sequence": (
                    0 if existing is None else int(existing["cursor_sequence"])
                ),
                "deleted": False,
            }
        elif sub_id not in self.subscriptions or self.subscriptions[sub_id].get(
            "deleted"
        ):
            raise WorkStoreError("invalid_request", ("subscription_not_found",))
        elif command.type == "delete_event_subscription":
            record = dict(self.subscriptions[sub_id])
            record["deleted"] = True
        else:
            record = dict(self.subscriptions[sub_id])
            record["cursor_sequence"] = command.payload.after_sequence
            record["deleted"] = False
        self.subscriptions[sub_id] = record
        receipt = OperationReceipt(
            operation_id=envelope.operation_id,
            request_id=envelope.request_id,
            state=OperationState.APPLIED,
            request_sha256="0" * 64,
            result_sha256="1" * 64,
        )
        return receipt, {"type": command.type, "subscription": record}


def _config(root: Path) -> OperationsConfig:
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
    )


def _write_capability(
    directory: Path, name: str, actor_id: UUID, scopes: list[str]
) -> None:
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "token": f"{name}-token",
                "actor_id": str(actor_id),
                "actor_kind": "task-agent",
                "workspaces": [str(WORKSPACE)],
                "scopes": scopes,
            }
        )
    )
    path.chmod(0o600)


def _client(tmp_path: Path, store: _Store, *, names: tuple[str, ...]) -> TestClient:
    directory = tmp_path / "capabilities"
    actors = {
        "a": (CLIENT_A, ["work.read"]),
        "b": (CLIENT_B, ["work.read"]),
        "admin": (ADMIN, ["work.read", "work.events.admin"]),
    }
    for name in names:
        actor_id, scopes = actors[name]
        _write_capability(directory, name, actor_id, scopes)
    return TestClient(
        create_app(
            _config(tmp_path),
            capabilities_dir=directory,
            store=store,  # type: ignore[arg-type]
        )
    )


def _headers(name: str, token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or f'{name}-token'}",
        "X-OMP-Contract-SHA256": contract_sha256(),
    }


def _post(client: TestClient, name: str, command: dict[str, object], token: str | None = None):
    return client.post(
        "/v1/commands",
        headers=_headers(name, token),
        json={
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        },
    )


def _put(subscription_id: UUID, client_id: UUID | None = None, push_url: str | None = None):
    payload: dict[str, object] = {
        "subscription_id": str(subscription_id),
        "event_types": ["mission.started"],
    }
    if client_id is not None:
        payload["client_id"] = str(client_id)
    if push_url is not None:
        payload["push_url"] = push_url
    return {"type": "put_event_subscription", "payload": payload}


def _delete(subscription_id: UUID) -> dict[str, object]:
    return {
        "type": "delete_event_subscription",
        "payload": {"subscription_id": str(subscription_id)},
    }


def _advance(subscription_id: UUID) -> dict[str, object]:
    return {
        "type": "advance_event_cursor",
        "payload": {"subscription_id": str(subscription_id), "after_sequence": 1},
    }


def test_client_a_cannot_touch_b_and_unknown_ids_reach_the_store(tmp_path: Path) -> None:
    store = _Store([_subscription(SUB_B, CLIENT_B)])
    client = _client(tmp_path, store, names=("a", "admin"))

    naming_b = _post(client, "a", _put(uuid4(), client_id=CLIENT_B))
    assert naming_b.status_code == 403
    assert naming_b.json()["error"]["code"] == "forbidden"
    put_of_b = _post(client, "a", _put(SUB_B))
    assert put_of_b.status_code == 403
    delete_of_b = _post(client, "a", _delete(SUB_B))
    assert delete_of_b.status_code == 403
    advance_of_b = _post(client, "a", _advance(SUB_B))
    assert advance_of_b.status_code == 403
    assert store.executed == []

    unknown = uuid4()
    missing = _post(client, "a", _delete(unknown))
    assert missing.status_code == 400
    assert missing.json()["error"]["diagnostics"] == ["subscription_not_found"]
    assert store.executed == [
        {
            "type": "delete_event_subscription",
            "subscription_id": unknown,
            "actor_id": CLIENT_A,
            "actor_kind": "task-agent",
        }
    ]

    own = _post(client, "a", _put(SUB_A, client_id=CLIENT_A))
    assert own.status_code == 200
    assert store.executed[-1]["subscription_id"] == SUB_A
    assert store.executed[-1]["actor_id"] == CLIENT_A

    before = len(store.executed)
    assert _post(client, "admin", _put(uuid4(), client_id=CLIENT_B)).status_code == 200
    assert _post(client, "admin", _put(SUB_B)).status_code == 200
    assert _post(client, "admin", _advance(SUB_B)).status_code == 200
    assert _post(client, "admin", _delete(SUB_B)).status_code == 200
    assert [call["actor_id"] for call in store.executed[before:]] == [ADMIN, ADMIN, ADMIN, ADMIN]


def test_event_push_advances_any_cursor_and_lists_every_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert (
        main(
            [
                "ops",
                "capabilities",
                "event-push",
                "--workspace-id",
                str(WORKSPACE),
                "--name",
                "pusher",
            ]
        )
        == 0
    )
    cap_dir = tmp_path / "omp" / "work-ledger" / "capabilities"
    pusher = json.loads((cap_dir / "pusher.json").read_text())
    assert pusher["actor_kind"] == "automation"
    assert pusher["scopes"] == sorted(EVENT_PUSH_SCOPES)
    assert pusher["workspaces"] == [str(WORKSPACE)]
    assert EVENT_PUSH_SCOPES == ("work.read", "work.events.admin")
    assert "work.events.admin" not in OWNER_SCOPES
    assert (cap_dir / "pusher.json").stat().st_mode & 0o777 == 0o600

    owner = cap_dir / "owner.json"
    owner.write_text(json.dumps({"actor_kind": "owner", "token": "owner-token"}))
    owner.chmod(0o600)
    before = owner.read_text()
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "ops",
                "capabilities",
                "event-push",
                "--workspace-id",
                str(WORKSPACE),
                "--name",
                "owner",
            ]
        )
    assert exc.value.code != 0
    assert owner.read_text() == before

    store = _Store([_subscription(SUB_A, CLIENT_A), _subscription(SUB_B, CLIENT_B)])
    client = TestClient(
        create_app(
            OperationsConfig.defaults(),
            capabilities_dir=cap_dir,
            store=store,  # type: ignore[arg-type]
        )
    )
    advanced = _post(client, "pusher", _advance(SUB_B), token=pusher["token"])
    assert advanced.status_code == 200
    assert store.executed == [
        {
            "type": "advance_event_cursor",
            "subscription_id": SUB_B,
            "actor_id": UUID(pusher["actor_id"]),
            "actor_kind": "automation",
        }
    ]

    listed = client.get(
        f"/v1/workspaces/{WORKSPACE}/event-subscriptions",
        headers=_headers("pusher", pusher["token"]),
    )
    assert listed.status_code == 200
    assert store.listed == [None]
    clients = {row["client_id"] for row in listed.json()["subscriptions"]}
    assert clients == {str(CLIENT_A), str(CLIENT_B)}


def test_check_destination_refuses_unsafe_targets_and_accepts_a_public_ip(
    tmp_path: Path,
) -> None:
    allowed = frozenset({"allowed.example"})

    def explode(host: str) -> list[str]:
        raise AssertionError(host)

    assert (
        check_destination(
            "http://allowed.example/hook", allowed_hosts=allowed, resolve=explode
        )
        == "scheme"
    )
    assert (
        check_destination(
            "http://[", allowed_hosts=allowed, resolve=explode
        )
        == "scheme"
    )
    assert (
        check_destination(
            "https://user:secret@allowed.example/hook",
            allowed_hosts=allowed,
            resolve=explode,
        )
        == "userinfo"
    )
    assert (
        check_destination(
            "https://user@[", allowed_hosts=allowed, resolve=explode
        )
        == "userinfo"
    )
    assert (
        check_destination(
            "https://other.example/hook", allowed_hosts=allowed, resolve=explode
        )
        == "host_not_allowed"
    )
    assert (
        check_destination(
            "https://[", allowed_hosts=allowed, resolve=explode
        )
        == "host_not_allowed"
    )
    assert (
        check_destination(
            "https://[invalid-ipv6]", allowed_hosts=allowed, resolve=explode
        )
        == "host_not_allowed"
    )
    assert (
        check_destination(
            "https://allowed.example/hook",
            allowed_hosts=allowed,
            resolve=lambda host: ["127.0.0.1"],
        )
        == "blocked:not_global"
    )
    assert (
        check_destination(
            "https://allowed.example/hook",
            allowed_hosts=allowed,
            resolve=lambda host: ["169.254.169.254"],
        )
        == "blocked:metadata"
    )
    assert (
        check_destination(
            "https://allowed.example/hook",
            allowed_hosts=allowed,
            resolve=lambda host: ["8.8.8.8", "127.0.0.1"],
        )
        == "blocked:not_global"
    )
    seen: list[str] = []

    def resolve_public(host: str) -> list[str]:
        seen.append(host)
        return ["8.8.8.8"]

    assert (
        check_destination(
            "https://Allowed.Example./hook",
            allowed_hosts={"Allowed.Example"},
            resolve=resolve_public,
        )
        is None
    )
    assert seen == ["allowed.example"]

    def offline(host: str) -> list[str]:
        raise OSError(host)

    assert (
        check_destination(
            "https://allowed.example/hook", allowed_hosts=allowed, resolve=offline
        )
        == "unresolvable"
    )
    assert (
        check_destination(
            "https://allowed.example/hook",
            allowed_hosts=allowed,
            resolve=lambda host: [],
        )
        == "unresolvable"
    )

    assert load_allowed_hosts(tmp_path) == frozenset()
    (tmp_path / "push-destinations.json").write_text("{")
    assert load_allowed_hosts(tmp_path) == frozenset()
    (tmp_path / "push-destinations.json").write_text(
        json.dumps({"allowed_hosts": ["Hooks.Example", "API.Example.", 1]})
    )
    assert load_allowed_hosts(tmp_path) == frozenset({"hooks.example", "api.example"})
    (tmp_path / "push-destinations.json").write_text(
        json.dumps({"allowed_hosts": "hooks.example"})
    )
    assert load_allowed_hosts(tmp_path) == frozenset()


def test_refused_push_url_is_400_until_check_destination_is_patched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config.config_dir.mkdir(parents=True)
    destination = config.config_dir / "push-destinations.json"
    destination.write_text(json.dumps({"allowed_hosts": ["Hooks.Example"]}))
    directory = tmp_path / "capabilities"
    _write_capability(directory, "a", CLIENT_A, ["work.read"])
    store = _Store()
    client = TestClient(
        create_app(config, capabilities_dir=directory, store=store)  # type: ignore[arg-type]
    )
    seen: list[frozenset[str]] = []

    def refuse(url: str, *, allowed_hosts, resolve=None) -> str:
        seen.append(frozenset(allowed_hosts))
        return "host_not_allowed"

    monkeypatch.setattr(event_push, "check_destination", refuse)
    refused = _post(client, "a", _put(SUB_A, push_url="https://hooks.example/h"))
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "invalid_request"
    assert refused.json()["error"]["diagnostics"] == [
        "push_destination_refused",
        "host_not_allowed",
    ]
    assert store.executed == []

    destination.write_text(json.dumps({"allowed_hosts": ["other.example"]}))

    def allow(url: str, *, allowed_hosts, resolve=None) -> None:
        seen.append(frozenset(allowed_hosts))
        return None

    monkeypatch.setattr(event_push, "check_destination", allow)
    accepted = _post(client, "a", _put(SUB_A, push_url="https://hooks.example/h"))
    assert accepted.status_code == 200
    assert len(store.executed) == 1
    assert seen == [frozenset({"hooks.example"}), frozenset({"hooks.example"})]


def test_push_url_without_a_checker_is_400() -> None:
    store = _Store()
    service = WorkService(store)  # type: ignore[arg-type]
    principal = Principal(
        actor_id=CLIENT_A,
        actor_kind="task-agent",
        workspaces=frozenset({WORKSPACE}),
        scopes=frozenset({"work.read"}),
    )
    envelope = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORKSPACE),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": _put(SUB_A, push_url="https://hooks.example/h"),
        }
    )
    with pytest.raises(WorkError) as refused:
        service.execute(principal, envelope)
    assert refused.value.status == 400
    assert refused.value.code == "invalid_request"
    assert refused.value.diagnostics == ("push_destination_refused", "no_check")
    assert store.executed == []


def test_malformed_push_url_is_refused_without_reaching_store(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.config_dir.mkdir(parents=True)
    destination = config.config_dir / "push-destinations.json"
    destination.write_text(json.dumps({"allowed_hosts": ["hooks.example"]}))
    directory = tmp_path / "capabilities"
    _write_capability(directory, "a", CLIENT_A, ["work.read"])
    store = _Store()
    client = TestClient(
        create_app(config, capabilities_dir=directory, store=store)  # type: ignore[arg-type]
    )

    r_bracket = _post(client, "a", _put(SUB_A, push_url="https://["))
    assert r_bracket.status_code == 400
    assert r_bracket.json()["error"]["code"] == "invalid_request"
    assert r_bracket.json()["error"]["diagnostics"] == [
        "push_destination_refused",
        "host_not_allowed",
    ]

    r_http_bracket = _post(client, "a", _put(SUB_A, push_url="http://["))
    assert r_http_bracket.status_code == 400
    assert r_http_bracket.json()["error"]["code"] == "invalid_request"
    assert r_http_bracket.json()["error"]["diagnostics"] == [
        "push_destination_refused",
        "scheme",
    ]

    r_user_bracket = _post(client, "a", _put(SUB_A, push_url="https://user@["))
    assert r_user_bracket.status_code == 400
    assert r_user_bracket.json()["error"]["code"] == "invalid_request"
    assert r_user_bracket.json()["error"]["diagnostics"] == [
        "push_destination_refused",
        "userinfo",
    ]

    assert store.executed == []

