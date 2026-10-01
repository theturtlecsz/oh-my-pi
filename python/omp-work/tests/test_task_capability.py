"""OMP-417: a task capability cannot mutate, and it expires with the lease.

The capability is a candidate reader (actor_kind task-agent, scope
work.candidate.read) plus expires_at. While the lease is live, every command
type in WorkService._scopes and every client POST answers 403. After
expires_at the same token is unknown and every route answers 401. A
capability file with no expires_at still authenticates.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError
from pydantic.fields import FieldInfo

from omp_work import contract_sha256
from omp_work.operations.capabilities import (
    provision_candidate_reader,
    provision_task_capability,
)
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import Command, CommandEnvelope
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkService

WORKSPACE = UUID("00000000-0000-7000-8000-000000000041")
HEX = "ab" * 32
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _RecordingStore:
    """Fake WorkStore. Authorization must refuse before execute."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        self.calls.append(envelope.command.type)
        raise AssertionError(f"task capability reached the store: {envelope.command.type}")

    def record_refused_attempt(self, envelope, *, actor_id, actor_kind, code, diagnostics):
        return None


def _min_len(field: FieldInfo) -> int:
    found = 0
    for meta in field.metadata:
        value = getattr(meta, "min_length", None)
        if isinstance(value, int):
            found = max(found, value)
    return found


def _pattern(field: FieldInfo) -> str | None:
    for meta in field.metadata:
        value = getattr(meta, "pattern", None)
        if isinstance(value, str):
            return value
    return None


def _model_union(annotation: Any) -> list[type[BaseModel]] | None:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _model_union(get_args(annotation)[0])
    if origin in (Union, UnionType):
        args = [item for item in get_args(annotation) if item is not type(None)]
        if args and all(isinstance(item, type) and issubclass(item, BaseModel) for item in args):
            return args
        return None
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return None


def _string(name: str, field: FieldInfo) -> str:
    if name == "remote_ref":
        return "refs/heads/main"
    if name == "usd":
        return "1"
    if name == "media_type":
        return "text/plain"
    pattern = _pattern(field) or ""
    if "0-9a-f" in pattern or name.endswith("sha256") or "sha" in name:
        return HEX
    if name == "client_ref":
        return "item1"
    if name == "state":
        return "BACKLOG"
    return "x"


def _sample(annotation: Any, field: FieldInfo | None, name: str) -> Any:
    models = _model_union(annotation)
    if models:
        chosen = min(models, key=lambda item: sum(1 for f in item.model_fields.values() if f.is_required()))
        return _sample_model(chosen)
    origin = get_origin(annotation)
    if origin is Annotated:
        return _sample(get_args(annotation)[0], field, name)
    if origin in (Union, UnionType):
        args = [item for item in get_args(annotation) if item is not type(None)]
        return _sample(args[0], field, name)
    if origin is Literal:
        return get_args(annotation)[0]
    if annotation is str or annotation is Any:
        return _string(name, field or FieldInfo(annotation=str))
    if annotation is int:
        return 1
    if annotation is bool:
        return True
    if annotation is UUID:
        return str(uuid4())
    type_name = getattr(annotation, "__name__", "")
    if (
        annotation is datetime
        or type_name in {"datetime", "AwareDatetime", "NaiveDatetime"}
        or (isinstance(annotation, type) and issubclass(annotation, datetime))
    ):
        return _NOW.isoformat()
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return next(iter(annotation)).value
    if origin in (tuple, list):
        inner = get_args(annotation)[0]
        if name == "event_types":
            return ["mission.started"]
        if name == "options":
            return ["a", "b"]
        count = _min_len(field) if field is not None else 0
        return [_sample(inner, None, name) for _ in range(count)]
    if origin is dict:
        if name == "risk_of_each_choice":
            return {"a": "ra", "b": "rb"}
        return {}
    if annotation is object:
        return {}
    raise TypeError(f"cannot sample {name}: {annotation!r}")


def _sample_model(model: type[BaseModel]) -> dict[str, Any]:
    hints = get_type_hints(model)
    if model.__name__ == "RelayOwnerIntentPayload":
        instruction = _model_union(hints["instruction"])
        assert instruction is not None
        return {
            "intent": "pause",
            "mission_id": str(uuid4()),
            "instruction": _sample_model(instruction[0]),
        }
    data: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        data[name] = _sample(hints[name], field, name)
    if model.__name__ == "IntakeSource":
        data["sha256"] = text_sha256(data["text"])
    if model.__name__ == "EvidenceReceipt":
        data["kind"] = "external_delivery"
    if model.__name__ == "SetResearchCampaignStatePayload":
        data["expected_state"] = "admitted"
        data["target_state"] = "running"
    if model.__name__ == "CreateDecisionPayload":
        data["options"] = ["a", "b"]
        data["risk_of_each_choice"] = {"a": "ra", "b": "rb"}
    if model.__name__ == "ResearchSourceAccess":
        data["access_class"] = "workspace"
    if model.__name__ == "CompletionEvidence":
        data["artifacts"] = [
            {"receipt_id": str(uuid4()), "kind": kind, "payload_sha256": HEX}
            for kind in ("verification", "audit", "push")
        ]
    return data


def _commands() -> dict[str, dict[str, Any]]:
    """One schema-valid command body per WorkService._scopes type."""
    built: dict[str, dict[str, Any]] = {}
    for model in get_args(get_args(Command)[0]):
        hints = get_type_hints(model)
        command_type = get_args(hints["type"])[0]
        body = {"type": command_type, "payload": _sample_model(hints["payload"])}
        model.model_validate(body)
        built[command_type] = body
    missing = set(WorkService._scopes) - set(built)
    if missing:
        raise AssertionError(f"no valid body for {sorted(missing)}")
    return built


def _config(tmp_path: Path) -> OperationsConfig:
    return OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "X-OMP-Contract-SHA256": contract_sha256(),
        "X-OMP-Workspace-ID": str(WORKSPACE),
    }


def _envelope(command: dict[str, Any]) -> dict[str, Any]:
    body = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORKSPACE),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    CommandEnvelope.model_validate(body)
    return body


def _client_posts(client: TestClient) -> list[str]:
    found: list[str] = []
    for route in client.app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        if "POST" in methods and "/client" in path:
            found.append(path)
    if len(found) < 10:
        raise AssertionError(f"expected the client POSTs, found {found}")
    return found


def _fill_path(path: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1) == "workspace_id":
            return str(WORKSPACE)
        return str(uuid4())

    return re.sub(r"\{([^}]+)\}", repl, path)


def _expire(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expires_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    path.chmod(0o600)


def test_task_capability_is_forbidden_until_it_expires(tmp_path: Path) -> None:
    config = _config(tmp_path)
    expires = datetime.now(UTC) + timedelta(hours=1)
    capability = provision_task_capability(
        config,
        workspace_id=WORKSPACE,
        candidate_ids=(uuid4(),),
        expires_at=expires,
        name="task",
    )
    record = json.loads(capability.read_text(encoding="utf-8"))
    assert record["actor_kind"] == "task-agent"
    assert record["scopes"] == ["work.candidate.read"]
    assert record["expires_at"] == expires.astimezone(UTC).isoformat()
    token = str(record["token"])

    timeless = provision_candidate_reader(
        config, workspace_id=WORKSPACE, candidate_ids=(uuid4(),), name="reader"
    )
    timeless_token = str(json.loads(timeless.read_text(encoding="utf-8"))["token"])
    assert "expires_at" not in json.loads(timeless.read_text(encoding="utf-8"))

    store = _RecordingStore()
    commands = _commands()
    with TestClient(
        create_app(config, capabilities_dir=capability.parent, store=store)  # type: ignore[arg-type]
    ) as client:
        posts = _client_posts(client)

        # No expires_at: still a principal, still forbidden to mutate.
        timeless_stop = client.post(
            "/v1/commands",
            headers=_headers(timeless_token),
            json=_envelope({"type": "engage_stop", "payload": {"reason": "stop"}}),
        )
        assert timeless_stop.status_code == 403, timeless_stop.text

        for command_type, command in commands.items():
            response = client.post(
                "/v1/commands",
                headers=_headers(token),
                json=_envelope(command),
            )
            assert response.status_code == 403, (command_type, response.status_code, response.text)
            assert response.json()["error"]["code"] == "forbidden"

        for path in posts:
            response = client.post(
                _fill_path(path),
                headers=_headers(token),
                json={"request_id": str(uuid4()), "payload": {}},
            )
            assert response.status_code == 403, (path, response.status_code, response.text)

        assert store.calls == []

        _expire(capability)
        for command_type, command in commands.items():
            response = client.post(
                "/v1/commands",
                headers=_headers(token),
                json=_envelope(command),
            )
            assert response.status_code == 401, (command_type, response.status_code, response.text)

        for path in posts:
            response = client.post(
                _fill_path(path),
                headers=_headers(token),
                json={"request_id": str(uuid4()), "payload": {}},
            )
            assert response.status_code == 401, (path, response.status_code, response.text)

        # The reader without expires_at is unaffected by the other file expiring.
        still = client.post(
            "/v1/commands",
            headers=_headers(timeless_token),
            json=_envelope({"type": "engage_stop", "payload": {"reason": "stop"}}),
        )
        assert still.status_code == 403, still.text
        assert store.calls == []


def test_command_samples_cover_scopes() -> None:
    """The sampler stays aligned with the command union. A ValidationError here
    means a command type would reach the server as 400 instead of 403."""
    try:
        built = _commands()
    except ValidationError as exc:
        raise AssertionError(str(exc)) from exc
    assert set(built) == set(WorkService._scopes)
