"""Close-stage checkpoint delivery transport (OMP-594-s03).

``_deliver_checkpoints`` owes one push per unresolved ``requires_delivery``
event: it sends the event's exact rendered text to the frozen subscription
destination with Idempotency-Key ``checkpoint:{event_id}`` and attests
``delivered`` only after ``event_push.send_signed`` confirms. A missing route
or a raising sender attests ``failed`` and leaves the debt pending; a retry
after a lost response reuses the open intent's URL and key, and a changed
subscription does not retarget it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work import event_push
from omp_work.action_classify import ResolvedTarget, classify, parse_submission
from omp_work.orchestrator.service import OrchestratorHandler, load_config, submit
from omp_work.push_http import PushDeliveryError
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.service import Principal, WorkService
from omp_work.v1.store import PostgresWorkStore
from orchestrator_e2e_support import (
    PUSH_URL,
    E2EWorld,
    SimulatedCrash,
    answer_intake,
    open_e2e_world,
    open_worker,
    seed_published_budget,
    sign_merge_answer,
    tick_until,
)
from test_workflow_service import OWNER, _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_FUTURE = datetime(2099, 1, 1, tzinfo=timezone.utc)
_CHANGED_URL = "https://checkpoints.example.test/omp/changed"
_OPS_URL = "https://checkpoints.example.test/omp/ops"
_SCOPES = ("work.read", "work.mutate", "work.approve", "work.close", "work.execute")


def _open(service: Any, root: Path, monkeypatch: Any) -> E2EWorld:
    """Build one E2EWorld with default operations."""
    root.mkdir(parents=True, exist_ok=True)
    world = open_e2e_world(
        service,
        root,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability="egress",
        operations=(),
    )
    document = json.loads(world.config_path.read_text(encoding="utf-8"))
    document.pop("operations", None)
    world.config_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    world.config = load_config(world.config_path, service.config)
    return world


def _steps(world: E2EWorld) -> list[dict[str, Any]]:
    handler = OrchestratorHandler(world.config)
    handler.mission_id = world.mission_id
    return handler.steps()


def _close_failed(world: E2EWorld, code: str) -> bool:
    return any(
        step.get("stage") == "close"
        and step.get("kind") == "outcome"
        and step.get("outcome") == "failed"
        and (step.get("data") or {}).get("code") == code
        for step in _steps(world)
    )


def _submit(world: E2EWorld, service: Any) -> UUID:
    submitted = submit(world.config, world.request)
    work_id = UUID(str(submitted["work_id"]))
    tree = world.store.read(world.workspace_id, OWNER, "tree", "")
    item = next(row for row in tree["items"] if str(row.get("work_id")) == str(work_id))
    seed_published_budget(
        service, world.workspace_id, work_id, UUID(str(item["revision"]["revision_id"]))
    )
    return work_id


def _answer_intake(world: E2EWorld) -> None:
    intake_step = next(
        s for s in _steps(world) if s.get("stage") == "intake" and s.get("kind") == "outcome"
    )
    world.intake_decision_id = UUID(str(intake_step["data"]["decision_id"]))
    world.mission_revision = int(intake_step["data"]["mission_revision"])
    answered = answer_intake(world, "edited_draft")
    assert answered["outcome"] == "approved"


def _sign_merge(world: E2EWorld, service: Any) -> None:
    steps = _steps(world)
    pause = next(
        s
        for s in reversed(steps)
        if s.get("kind") == "pause" and s.get("rule_id") == "d41-merge-approval"
    )
    push_outcome = next(
        s for s in steps if s.get("stage") == "push" and s.get("kind") == "outcome"
    )
    commit = str(push_outcome["data"]["candidate_commit"])
    operation = {
        "kind": "git_push",
        "repository": world.repo_key,
        "branch": "main",
        "commit": commit,
    }
    digest = classify(
        parse_submission(operation),
        ResolvedTarget(repository=world.repository_record(), commit=commit, changed_paths=()),
    ).target_sha256
    signature = sign_merge_answer(
        world.owner_key_path,
        workspace_id=world.workspace_id,
        decision_id=UUID(str(pause["decision_id"])),
        action_class="merge_protected_branch",
        answer="approve",
        target_sha256=digest,
        expires_at=_FUTURE,
    )
    code, body = _command(
        service,
        world.workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(pause["decision_id"]),
                "answer": "approve",
                "owner_signature": signature,
                "expires_at": _FUTURE.isoformat(),
            },
        },
    )
    assert code == 200, body


def _to_merge_pause(world: E2EWorld, service: Any, worker: Any) -> UUID:
    """Drive the world to the answered merge pause; close is all that remains."""
    work_id = _submit(world, service)
    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d23-owner-confirm" for s in v.get("steps", [])),
    )
    _answer_intake(world)
    tick_until(
        worker,
        world,
        lambda v: any(s.get("rule_id") == "d41-merge-approval" for s in v.get("steps", [])),
    )
    _sign_merge(world, service)
    return work_id


def _owed_events(world: E2EWorld, work_id: UUID) -> list[dict[str, Any]]:
    """The work item's requires_delivery events, in sequence order."""
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, event_type, rendered_text, rendered_sha256"
            " FROM omp_work.close_attempt_events"
            " WHERE workspace_id=%s AND work_id=%s AND requires_delivery ORDER BY sequence",
            (world.workspace_id, work_id),
        )
        return [dict(row) for row in cur.fetchall()]


def _deliveries(world: E2EWorld, event_id: Any) -> list[dict[str, Any]]:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), row_factory=dict_row
    ) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_id, delivery_sequence, rendered_sha256, status"
            " FROM omp_work.checkpoint_deliveries"
            " WHERE workspace_id=%s AND event_id=%s ORDER BY delivery_sequence",
            (world.workspace_id, event_id),
        )
        return [dict(row) for row in cur.fetchall()]


def _subscription_command(world: E2EWorld, command: dict[str, Any]) -> None:
    service = WorkService(
        PostgresWorkStore(world.service.config),
        push_destination_check=lambda url: None,
    )
    automation = Principal(
        actor_id=OWNER,
        actor_kind="automation",
        workspaces=frozenset({world.workspace_id}),
        scopes=frozenset(_SCOPES),
    )
    service.execute(
        automation,
        CommandEnvelope.model_validate(
            {
                "api_version": "work.omp.dev/v1",
                "workspace_id": str(world.workspace_id),
                "operation_id": str(uuid4()),
                "request_id": str(uuid4()),
                "correlation_id": str(uuid4()),
                "command": command,
            }
        ),
    )


def _put_subscription(world: E2EWorld, push_url: str, event_types: tuple[str, ...], *, subscription_id: UUID | None = None) -> None:
    _subscription_command(
        world,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(subscription_id or uuid4()),
                "push_url": push_url,
                "event_types": list(event_types),
            },
        },
    )


def _delete_subscription(world: E2EWorld, subscription_id: UUID) -> None:
    _subscription_command(
        world,
        {
            "type": "delete_event_subscription",
            "payload": {"subscription_id": str(subscription_id)},
        },
    )


def _recorder(world: E2EWorld):
    def record(
        url: str,
        token: str | None,
        idempotency_key: str,
        body: Any,
        *,
        timeout: float = 10,
        headers: dict[str, str] | None = None,
    ) -> None:
        del timeout
        world.sent_pushes.append(
            (str(url), token, str(idempotency_key), body, dict(headers or {}))
        )

    return record


def _expire_lease(world: E2EWorld) -> None:
    with psycopg.connect(
        **world.config.ops.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "UPDATE omp_jobs.jobs SET lease_expires_at = clock_timestamp() - interval '1 second' "
            "WHERE workspace_id=%s AND lease_expires_at IS NOT NULL",
            (world.workspace_id,),
        )


def _crash_after_first_send(world: E2EWorld, monkeypatch: Any) -> dict[str, bool]:
    """Record the send, then crash before the delivered attestation, once."""
    state = {"crashed": False}

    def crash_after_send(
        url: str,
        token: str | None,
        idempotency_key: str,
        body: Any,
        *,
        timeout: float = 10,
        headers: dict[str, str] | None = None,
    ) -> None:
        del timeout
        world.sent_pushes.append(
            (str(url), token, str(idempotency_key), body, dict(headers or {}))
        )
        if not state["crashed"]:
            state["crashed"] = True
            raise SimulatedCrash("lost-response")

    monkeypatch.setattr(event_push, "push_send", crash_after_send)
    return state


def test_confirmed_send_attests_delivered_with_rendered_text(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A confirmed send stores one delivered row bound to the event's rendered hash."""
    world = _open(service, tmp_path / "delivered", monkeypatch)
    worker = open_worker(world)
    work_id = _to_merge_pause(world, service, worker)
    view = tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert view["status"] == "completed"

    events = _owed_events(world, work_id)
    assert [str(event["event_type"]) for event in events] == [
        "attempt_begun",
        "auditor_launch_settled",
    ]
    assert len(world.sent_pushes) == len(events)
    key = event_push.subscription_key(
        event_push.load_master_key(service.config.config_dir),
        world.push_subscription_id,
    )
    for event, (url, token, idempotency_key, body, headers) in zip(
        events, world.sent_pushes, strict=True
    ):
        deliveries = _deliveries(world, event["event_id"])
        assert [row["status"] for row in deliveries] == ["delivered"]
        assert str(deliveries[0]["rendered_sha256"]) == str(event["rendered_sha256"])
        assert url == PUSH_URL
        assert token is None
        assert idempotency_key == f"checkpoint:{event['event_id']}"
        assert json.loads(body) == {
            "event_id": str(event["event_id"]),
            "event_type": str(event["event_type"]),
            "rendered_sha256": str(event["rendered_sha256"]),
            "rendered_text": str(event["rendered_text"]),
        }
        assert event_push.verify(
            key, idempotency_key, body, headers.get("X-OMP-Signature")
        )


def test_missing_route_leaves_debt_pending(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """No subscription route: one failed attestation, no send, debt pending."""
    world = _open(service, tmp_path / "no-route", monkeypatch)
    _delete_subscription(world, world.push_subscription_id)
    worker = open_worker(world)
    work_id = _to_merge_pause(world, service, worker)
    tick_until(worker, world, lambda _v: _close_failed(world, "delivery_route_missing"))

    events = _owed_events(world, work_id)
    assert len(events) == 2
    # The first owed event fails the pass; the second is never reached.
    assert [row["status"] for row in _deliveries(world, events[0]["event_id"])] == ["failed"]
    assert str(_deliveries(world, events[0]["event_id"])[0]["rendered_sha256"]) == str(
        events[0]["rendered_sha256"]
    )
    assert _deliveries(world, events[1]["event_id"]) == []
    assert world.sent_pushes == []


def test_ops_only_subscription_is_delivery_route_missing(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """An ops.*-only subscription is not a checkpoint route."""
    world = _open(service, tmp_path / "ops-only", monkeypatch)
    _delete_subscription(world, world.push_subscription_id)
    _put_subscription(world, _OPS_URL, ("ops.alarm",))
    worker = open_worker(world)
    work_id = _to_merge_pause(world, service, worker)
    tick_until(worker, world, lambda _v: _close_failed(world, "delivery_route_missing"))

    events = _owed_events(world, work_id)
    assert len(events) == 2
    assert [row["status"] for row in _deliveries(world, events[0]["event_id"])] == ["failed"]
    assert _deliveries(world, events[1]["event_id"]) == []
    assert world.sent_pushes == []


def test_raising_sender_leaves_debt_pending(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A sender that keeps failing: one failed attestation, debt pending."""
    world = _open(service, tmp_path / "sender-down", monkeypatch)

    def always_fail(
        url: str,
        token: str | None,
        idempotency_key: str,
        body: Any,
        *,
        timeout: float = 10,
        headers: dict[str, str] | None = None,
    ) -> None:
        raise PushDeliveryError("connection refused")

    monkeypatch.setattr(event_push, "push_send", always_fail)
    worker = open_worker(world)
    work_id = _to_merge_pause(world, service, worker)
    tick_until(worker, world, lambda _v: _close_failed(world, "delivery_send_failed"))

    events = _owed_events(world, work_id)
    assert len(events) == 2
    assert [row["status"] for row in _deliveries(world, events[0]["event_id"])] == ["failed"]
    assert _deliveries(world, events[1]["event_id"]) == []
    assert world.sent_pushes == []


def test_retry_after_lost_response_reuses_destination(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A crash after the send: the retry reuses the URL and the Idempotency-Key."""
    world = _open(service, tmp_path / "lost-response", monkeypatch)
    crash = _crash_after_first_send(world, monkeypatch)
    worker = open_worker(world)
    work_id = _to_merge_pause(world, service, worker)

    with pytest.raises(SimulatedCrash):
        tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert crash["crashed"] is True
    assert len(world.sent_pushes) == 1

    _expire_lease(world)
    monkeypatch.setattr(event_push, "push_send", _recorder(world))
    view = tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert view["status"] == "completed"

    # The retried first event reuses the frozen URL and Idempotency-Key.
    assert len(world.sent_pushes) == 3
    first_url, _first_token, first_key, first_body, _first_headers = world.sent_pushes[0]
    second_url, _second_token, second_key, second_body, _second_headers = world.sent_pushes[1]
    assert first_url == second_url == PUSH_URL
    assert first_key == second_key
    assert first_body == second_body

    events = _owed_events(world, work_id)
    assert len(events) == 2
    assert first_key == f"checkpoint:{events[0]['event_id']}"
    for event in events:
        assert [row["status"] for row in _deliveries(world, event["event_id"])] == ["delivered"]


def test_changed_subscription_does_not_retarget_open_intent(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A subscription change after the intent is open does not move the send."""
    world = _open(service, tmp_path / "retarget", monkeypatch)
    crash = _crash_after_first_send(world, monkeypatch)
    worker = open_worker(world)
    _to_merge_pause(world, service, worker)

    with pytest.raises(SimulatedCrash):
        tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert crash["crashed"] is True

    _expire_lease(world)
    _put_subscription(world, _CHANGED_URL, ("mission.progressed",), subscription_id=world.push_subscription_id)
    monkeypatch.setattr(event_push, "push_send", _recorder(world))
    view = tick_until(worker, world, lambda v: v.get("status") == "completed")
    assert view["status"] == "completed"

    # The retried first event keeps the frozen destination; the second event,
    # whose debt was never attempted before the change, routes to the new URL.
    assert len(world.sent_pushes) == 3
    first_url, _first_token, first_key, first_body, _first_headers = world.sent_pushes[0]
    second_url, _second_token, second_key, second_body, _second_headers = world.sent_pushes[1]
    third_url, _third_token, third_key, _third_body, _third_headers = world.sent_pushes[2]
    assert first_url == second_url == PUSH_URL
    assert first_key == second_key
    assert first_body == second_body
    assert third_url == _CHANGED_URL
    assert third_key != first_key


def test_sitecustomize_keeps_the_installed_receiver(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """Executing the child seam does not replace a push_send the suite installed."""
    world = _open(service, tmp_path / "keep-receiver", monkeypatch)
    recorded: list[tuple] = []

    def receiver(
        url: str,
        token: str | None,
        idempotency_key: str,
        body: Any,
        *,
        timeout: float = 10,
        headers: dict[str, str] | None = None,
    ) -> None:
        del timeout
        recorded.append((url, token, idempotency_key, body, dict(headers or {})))

    monkeypatch.setattr(event_push, "push_send", receiver)
    stub = (world.config_path.parent / "push-stub" / "sitecustomize.py").read_text(
        encoding="utf-8"
    )
    # The child seam, run here after this test installed its own receiver.
    exec(stub, {})  # nosec B102 - loads the world's sitecustomize, no user input
    event_push.send_signed(
        PUSH_URL, "checkpoint:probe", {"rendered_text": "probe"}, key=b"x" * 32
    )
    assert len(recorded) == 1
    url, token, idempotency_key, body, headers = recorded[0]
    assert url == PUSH_URL
    assert token is None
    assert idempotency_key == "checkpoint:probe"
    assert json.loads(body) == {"rendered_text": "probe"}
    assert str(headers.get("X-OMP-Signature", "")).startswith("v1=")
    assert world.sent_pushes == []


def test_child_process_records_the_confirmed_push(
    service: Any, tmp_path: Path, monkeypatch: Any
) -> None:
    """A child interpreter's send_signed is visible on world.sent_pushes."""
    world = _open(service, tmp_path / "child-record", monkeypatch)
    probe = (
        "import os\n"
        "from omp_work import event_push\n"
        "event_push.send_signed(\n"
        "    os.environ['PUSH_URL'],\n"
        "    'checkpoint:probe',\n"
        "    {'rendered_text': 'probe'},\n"
        "    key=b'x' * 32,\n"
        ")\n"
    )
    env = os.environ.copy()
    env["PUSH_URL"] = PUSH_URL
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    assert len(world.sent_pushes) == 1
    url, token, idempotency_key, body, headers = world.sent_pushes[0]
    assert url == PUSH_URL
    assert token is None
    assert idempotency_key == "checkpoint:probe"
    assert json.loads(body) == {"rendered_text": "probe"}
    assert str(headers.get("X-OMP-Signature", "")).startswith("v1=")
