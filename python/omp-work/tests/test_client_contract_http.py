"""OMP-416-s06: all seventeen client contract operations on HTTP.

No database. A fake store is passed to ``create_app``; the client capability
holds ``CLIENT_SCOPES`` and its actor is the designated controller, so an
unsigned relayed intent reaches the store. Each operation is exercised as a
non-owner ``client``, which proves the contract serves every client_contract
row without an owner principal.

Diagnostic keys (token/worker/worktree/operation_id/request_id/correlation_id)
never appear in a default body; ``?detail=true`` returns them under ``detail``
without leaking them elsewhere. The relay bodies the owner-client contract
replaces — reprioritise, cancel — and the intake decision output carry none of
the retired operation names.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from omp_work import contract_sha256
from omp_work.operations.capabilities import CLIENT_SCOPES
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import text_sha256
from omp_work.v1.models import OperationReceipt, OperationState
from omp_work.v1.owner_controller import designation_message, write_designation
from omp_work.v1.owner_signature import NAMESPACE
from omp_work.v1.server import create_app

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")
PROJECT = UUID("00000000-0000-7000-8000-000000000020")
MISSION = UUID("00000000-0000-7000-8000-000000000030")
RECEIPT = UUID("00000000-0000-7000-8000-000000000040")
DECISION = UUID("00000000-0000-7000-8000-000000000041")
# The client capability's actor, and therefore the designated controller.
CONTROLLER = UUID("00000000-0000-7000-8000-0000000000c1")

_BASE = f"/v1/workspaces/{WORKSPACE}/client"
_REQUEST_ID = UUID("00000000-0000-7000-8000-0000000000ff")
_WORKER = "worker-7"
_MAX_TOKENS = 4096
_WORKTREE = "/tmp/client-wt"
_DIAGNOSTIC_KEYS = frozenset(
    {"worker_id", "max_tokens", "worktree", "operation_id", "request_id", "correlation_id"}
)
_FORBIDDEN_SUBSTRINGS = ("queue_work", "set_now", "cancel_work")
_PREFIX = [
    "outcome",
    "state",
    "evidence",
    "blockers",
    "decisions",
    "artifacts",
    "operation",
    "contract",
]

_READ_ROUTES = (
    ("project.list", f"{_BASE}/projects"),
    ("project.context", f"{_BASE}/projects/{PROJECT}/context"),
    ("project.status", f"{_BASE}/projects/{PROJECT}/status"),
    ("project.decisions", f"{_BASE}/projects/{PROJECT}/decisions"),
    ("mission.status", f"{_BASE}/missions/{MISSION}"),
    ("evidence.inspect", f"{_BASE}/evidence/{RECEIPT}"),
    ("stop.status", f"{_BASE}/stop"),
)


def _instruction() -> dict[str, object]:
    return {
        "text": "Owner intent.",
        "source_message_ref": "msg-1",
        "owner_authored": True,
        "received_at": "2026-09-30T12:00:00+00:00",
    }


def _draft() -> dict[str, object]:
    return {
        "project_id": str(PROJECT),
        "objective": "Deliver the client contract",
        "risk_policy": "risk-policy",
        "approval_policy": "approval-policy",
        "effort_policy": "effort-policy",
    }


def _intake() -> dict[str, object]:
    text = "Confirm the mission scope."
    return {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {"id": "goal-1", "statement": "Confirm scope", "source_span_ids": []},
    }


def _intake_scope() -> dict[str, object]:
    return {
        "project_id": str(PROJECT),
        "risk_policy": "risk-policy",
        "approval_policy": "approval-policy",
        "effort_policy": "effort-policy",
    }


def _mutation_routes() -> tuple[tuple[str, str, dict[str, object], UUID | None], ...]:
    instruction = _instruction()
    return (
        (
            "mission.submit",
            f"{_BASE}/missions",
            {"mission_id": str(MISSION), "draft": _draft()},
            None,
        ),
        (
            "mission.intake",
            f"{_BASE}/mission-intake",
            {"mission_id": str(MISSION), "intake": _intake(), "scope": _intake_scope()},
            None,
        ),
        (
            "stop.engage",
            f"{_BASE}/stop",
            {"reason": "owner asked to stop"},
            None,
        ),
        (
            "mission.pause",
            f"{_BASE}/missions/{MISSION}/pause",
            {"intent": "pause", "instruction": instruction, "mission_id": str(MISSION)},
            MISSION,
        ),
        (
            "mission.resume",
            f"{_BASE}/missions/{MISSION}/resume",
            {"intent": "resume", "instruction": instruction, "mission_id": str(MISSION)},
            MISSION,
        ),
        (
            "mission.cancel",
            f"{_BASE}/missions/{MISSION}/cancel",
            {
                "intent": "request_cancellation",
                "instruction": instruction,
                "mission_id": str(MISSION),
            },
            MISSION,
        ),
        (
            "mission.reprioritise",
            f"{_BASE}/missions/{MISSION}/priority",
            {
                "intent": "change_priority",
                "instruction": instruction,
                "mission_id": str(MISSION),
                "revision": 1,
                "priority": 1,
            },
            MISSION,
        ),
        (
            "mission.scope.confirm",
            f"{_BASE}/missions/{MISSION}/scope/confirm",
            {
                "intent": "confirm_scope",
                "instruction": instruction,
                "mission_id": str(MISSION),
                "decision_id": str(DECISION),
                "revision": 1,
            },
            MISSION,
        ),
        (
            "mission.scope.edit",
            f"{_BASE}/missions/{MISSION}/scope/edit",
            {
                "intent": "edit_scope",
                "instruction": instruction,
                "mission_id": str(MISSION),
                "decision_id": str(DECISION),
                "revision": 1,
                "draft": _draft(),
            },
            MISSION,
        ),
        (
            "decision.answer",
            f"{_BASE}/decisions/{DECISION}/answer",
            {
                "intent": "answer_decision",
                "instruction": instruction,
                "decision_id": str(DECISION),
                "answer": "approved",
            },
            DECISION,
        ),
    )


class _RecordingStore:
    """Fake WorkStore: reads return a leaky view, execute echoes the envelope."""

    def __init__(self) -> None:
        self.executes: list[dict[str, object]] = []

    def list_projects(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        return _leaky("project.list", workspace_id=str(workspace_id))

    def project_context(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        return _leaky("project.context", project_id=str(project_id))

    def read_project(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        return _leaky("project.status", project_id=str(project_id))

    def decisions(
        self, workspace_id: UUID, actor_id: UUID, **kwargs: object
    ) -> dict[str, object]:
        return _leaky(
            "project.decisions",
            decisions=[
                {"decision_id": str(DECISION), "question": "ship?"},
            ],
        )

    def read(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        kind: str,
        value: str,
        **kwargs: object,
    ) -> dict[str, object]:
        return _leaky("mission.status", mission_id=value, status="paused")

    def receipt(
        self, workspace_id: UUID, actor_id: UUID, receipt_id: object
    ) -> dict[str, object]:
        return _leaky("evidence.inspect", receipt_id=str(receipt_id))

    def stop_status(self, workspace_id: UUID, actor_id: UUID) -> dict[str, object]:
        return {
            "workspace_id": workspace_id,
            "stopped": False,
            "reason": None,
            "changed_at": None,
            "changed_by_actor_kind": None,
        }

    def execute(self, envelope, *, actor_id, actor_kind, required_scope):
        command = envelope.command
        self.executes.append(
            {
                "type": command.type,
                "actor_id": actor_id,
                "actor_kind": actor_kind,
                "required_scope": required_scope,
                "workspace_id": envelope.workspace_id,
            }
        )
        receipt = OperationReceipt(
            operation_id=envelope.operation_id,
            request_id=envelope.request_id,
            state=OperationState.APPLIED,
            request_sha256="0" * 64,
            result_sha256="1" * 64,
        )
        result = _leaky(
            command.type,
            operation_id=str(envelope.operation_id),
        )
        if command.type == "relay_owner_intent":
            result["intent"] = command.payload.intent
        if command.type == "draft_mission_intake":
            result["intake_decision"] = {
                "question": "Confirm mission revision 1?",
                "options": ["confirm", "reject"],
                "state": "pending",
                "answered_at": None,
            }
        return receipt, result


def _leaky(operation: str, **extra: object) -> dict[str, object]:
    view: dict[str, object] = {
        "marker": operation,
        "worker_id": _WORKER,
        "budget": {"max_tokens": _MAX_TOKENS, "currency": "USD"},
        "items": [{"worktree": _WORKTREE, "name": "keep"}],
    }
    view.update(extra)
    return view


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _designate(config_dir: Path, key_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    key = _generate_key(key_dir, "owner")
    signers = config_dir / "owner_allowed_signers"
    signers.write_text(
        f"owner {Path(f'{key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    write_designation(
        config_dir,
        WORKSPACE,
        CONTROLLER,
        _sign(key, designation_message(WORKSPACE, CONTROLLER)),
    )


def _capabilities(tmp_path: Path) -> Path:
    directory = tmp_path / "capabilities"
    directory.mkdir(mode=0o700)
    for name, token, actor_id, scopes in (
        ("client", "client-token", CONTROLLER, list(CLIENT_SCOPES)),
        ("unscoped", "unscoped-token", uuid4(), ["work.execute"]),
    ):
        path = directory / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "token": token,
                    "actor_id": str(actor_id),
                    "actor_kind": "client",
                    "workspaces": [str(WORKSPACE)],
                    "scopes": scopes,
                }
            )
        )
        path.chmod(0o600)
    return directory


def _http(tmp_path: Path, store: _RecordingStore) -> TestClient:
    config_dir = tmp_path / "config"
    _designate(config_dir, tmp_path)
    config = OperationsConfig(
        config_dir=config_dir,
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    return TestClient(
        create_app(
            config,
            capabilities_dir=_capabilities(tmp_path),
            store=store,  # type: ignore[arg-type]
        )
    )


def _headers(token: str, *, contract: bool = True) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if contract:
        headers["X-OMP-Contract-SHA256"] = contract_sha256()
    return headers


def _walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(str(key))
            keys.extend(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


def _assert_no_diagnostic_keys(body: dict[str, Any]) -> None:
    for section, value in body.items():
        if section == "detail":
            continue
        assert not (_DIAGNOSTIC_KEYS & set(_walk_keys(value))), value


def _assert_prefix(body: dict[str, Any]) -> None:
    assert list(body.keys())[:8] == _PREFIX
    assert body["contract"] == "client.omp.dev/v1"


@pytest.mark.parametrize("operation,path", _READ_ROUTES)
def test_each_read_succeeds_for_a_non_owner_client(
    tmp_path: Path, operation: str, path: str
) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)

    response = client.get(path, headers=_headers("client-token"))

    assert response.status_code == 200, (operation, response.text)
    body = response.json()
    _assert_prefix(body)
    assert body["outcome"] == "read"
    assert body["operation"] == operation
    assert body["detail"] is None
    _assert_no_diagnostic_keys(body)


@pytest.mark.parametrize(
    "operation,path,payload,path_ident",
    _mutation_routes(),
    ids=[row[0] for row in _mutation_routes()],
)
def test_each_mutation_succeeds_for_a_non_owner_client(
    tmp_path: Path,
    operation: str,
    path: str,
    payload: dict[str, object],
    path_ident: UUID | None,
) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    body_in = {"request_id": str(_REQUEST_ID), "payload": payload}

    response = client.post(path, headers=_headers("client-token"), json=body_in)

    assert response.status_code == 200, (operation, response.text)
    body = response.json()
    _assert_prefix(body)
    assert body["outcome"] == "applied"
    assert body["operation"] == operation
    assert body["detail"] is None
    _assert_no_diagnostic_keys(body)
    assert len(store.executes) == 1
    call = store.executes[0]
    assert call["actor_kind"] == "client"
    assert call["actor_id"] == CONTROLLER
    expected_scope = "work.stop" if operation == "stop.engage" else "work.client"
    assert call["required_scope"] == expected_scope

    expected_id = uuid5(
        NAMESPACE_URL,
        f"client:{WORKSPACE}:{CONTROLLER}:{operation}:{_REQUEST_ID}",
    )
    detailed = client.post(
        path, headers=_headers("client-token"), json=body_in, params={"detail": "true"}
    )
    assert detailed.status_code == 200, (operation, detailed.text)
    detail_body = detailed.json()
    _assert_prefix(detail_body)
    _assert_no_diagnostic_keys(detail_body)
    assert detail_body["detail"]["operation_id"] == str(expected_id)
    assert detail_body["detail"]["worker_id"] == _WORKER
    assert detail_body["detail"]["budget.max_tokens"] == _MAX_TOKENS
    assert detail_body["detail"]["items.0.worktree"] == _WORKTREE


def test_reads_detail_returns_the_store_diagnostics(tmp_path: Path) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)

    detailed = client.get(
        f"{_BASE}/projects",
        headers=_headers("client-token"),
        params={"detail": "true"},
    )

    assert detailed.status_code == 200
    detail_body = detailed.json()
    _assert_prefix(detail_body)
    _assert_no_diagnostic_keys(detail_body)
    assert detail_body["detail"] == {
        "worker_id": _WORKER,
        "budget.max_tokens": _MAX_TOKENS,
        "items.0.worktree": _WORKTREE,
    }


def test_reprioritise_and_cancel_bodies_carry_no_retired_operation_names(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    routes = {row[0]: row for row in _mutation_routes()}

    for operation in ("mission.reprioritise", "mission.cancel"):
        _, path, payload, _ = routes[operation]
        response = client.post(
            path,
            headers=_headers("client-token"),
            json={"request_id": str(uuid4()), "payload": payload},
        )
        assert response.status_code == 200, (operation, response.text)
        text = json.dumps(response.json())
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            assert forbidden not in text, (operation, forbidden)


def test_intake_decision_output_carries_no_retired_operation_names(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)
    _, path, payload, _ = next(
        row for row in _mutation_routes() if row[0] == "mission.intake"
    )

    response = client.post(
        path,
        headers=_headers("client-token"),
        json={"request_id": str(uuid4()), "payload": payload},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["intake_decision"]["state"] == "pending"
    text = json.dumps(body)
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in text, forbidden


def test_principal_without_client_or_read_scope_is_forbidden(
    tmp_path: Path,
) -> None:
    store = _RecordingStore()
    client = _http(tmp_path, store)

    for operation, path in _READ_ROUTES:
        response = client.get(path, headers=_headers("unscoped-token"))
        assert response.status_code == 403, (operation, response.text)
        assert response.json()["error"]["code"] == "forbidden"

    for operation, path, payload, _ in _mutation_routes():
        response = client.post(
            path,
            headers=_headers("unscoped-token"),
            json={"request_id": str(uuid4()), "payload": payload},
        )
        assert response.status_code == 403, (operation, response.text)
        assert response.json()["error"]["code"] == "forbidden"

    assert store.executes == []
