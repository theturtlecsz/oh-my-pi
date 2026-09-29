"""Live expired-grant fence: new effects refuse until the owner reconciles."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.v1.canonical import sha256, text_sha256
from test_workflow_service import (
    OWNER,
    _command,
    _create,
    _grant,
    _owner_headers,
    _receipt,
    _tcb_manifest,
    service,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def test_expired_execution_grant_fences_writes_and_requires_owner_reconciliation(
    service, monkeypatch
) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Expired grant item", description="desc")
    judge_sha, judge_manifest = _tcb_manifest()
    grant_id = str(uuid4())
    begin = {
        "type": "begin_execution",
        "payload": {
            "grant_id": grant_id,
            "provenance": {
                "owner_input_id": str(uuid4()),
                "owner_session_id": "session-1",
                "normalized_command": "/execute OMP-1",
                "workspace_id": str(workspace_id),
                "repository": "oh-my-pi",
                "nonce": str(uuid4()),
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
            "remote_ref": "refs/heads/main",
            "mode": "single",
            "items": [
                {
                    "work_id": item["work_id"],
                    "revision_id": item["revision_id"],
                    "position": 0,
                    "original_request": "desc",
                    "original_request_sha256": text_sha256("desc"),
                    "initial_git_baseline": "0" * 40,
                }
            ],
            "expected_focus_version": 0,
            "judge_sha256": judge_sha,
            "judge_manifest": judge_manifest,
        },
    }

    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class OldAdmissionClock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(days=8)

    # Only the isolated fixture's admission clock moves; no immutable DB history is edited.
    operation_id = uuid4()
    with monkeypatch.context() as patch:
        patch.setattr("omp_work.v1.store.datetime", OldAdmissionClock)
        status, original = _command(
            service, workspace_id, begin, operation_id=operation_id
        )
    assert status == 200, original
    status, replay = _command(service, workspace_id, begin, operation_id=operation_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed", replay
    state = {
        "grant_id": grant_id,
        "expected_grant_version": 1,
        "judge_sha256": judge_sha,
    }
    for reason in (None, "service_refresh"):
        payload = {**state, "target_state": "active"}
        if reason:
            payload["reason"] = reason
        status, body = _command(
            service, workspace_id, {"type": "set_execution_state", "payload": payload}
        )
        assert status == 409 and body["error"]["code"] == "execution_grant_stale", body
    receipt = _receipt(item["work_id"], item["revision_id"], uuid4(), "verification")
    status, body = _command(
        service, workspace_id, {"type": "append_evidence", "payload": {"receipt": receipt}}
    )
    assert status == 409 and body["error"]["code"] == "execution_grant_stale", body
    replacement = {
        "type": "begin_execution",
        "payload": {**begin["payload"], "grant_id": str(uuid4())},
    }
    status, body = _command(service, workspace_id, replacement)
    assert status == 409 and body["error"]["code"] == "execution_grant_stale", body
    view = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    ).json()
    assert view["grant"]["state"] == "active"
    assert view["grant"]["grant_version"] == 1
    assert view["items"][0]["phase"] == "criteria_pending"
    status, body = _command(
        service,
        workspace_id,
        {"type": "set_execution_state", "payload": {**state, "target_state": "stopped"}},
    )
    assert status == 200 and body["result"]["grant"]["state"] == "stopped", body
    # Admission changed focus once; explicit stop clears it and increments again.
    replacement["payload"]["expected_focus_version"] = 2
    status, body = _command(service, workspace_id, replacement)
    assert status == 200 and body["result"]["grant"]["state"] == "active", body


def _expired_clock():
    """A datetime whose now() is eight days behind wall time.

    expires_at is computed by the service at admission from its own clock and the
    immutability triggers forbid changing it afterwards, so moving the service's
    admission clock back is the only way to obtain a genuinely expired grant
    without editing immutable history.
    """

    class ClockType(type):
        def __instancecheck__(cls, value):
            return isinstance(value, datetime)

    class OldAdmissionClock(datetime, metaclass=ClockType):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) - timedelta(days=8)

    return OldAdmissionClock


def _begin_grant(workspace_id, items, *, description: str) -> dict:
    """Admit a grant covering every already-created entry in `items`.

    begin_execution binds each claim's original_request to the work revision's
    description, so every listed item must have been filed with `description`.
    """
    judge_sha, judge_manifest = _tcb_manifest()
    return {
        "type": "begin_execution",
        "payload": {
            "grant_id": str(uuid4()),
            "provenance": {
                "owner_input_id": str(uuid4()),
                "owner_session_id": "session-1",
                "normalized_command": "/execute OMP-1",
                "workspace_id": str(workspace_id),
                "repository": "oh-my-pi",
                "nonce": str(uuid4()),
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
            "remote_ref": "refs/heads/main",
            "mode": "single" if len(items) == 1 else "queue",
            "items": [
                {
                    "work_id": item["work_id"],
                    "revision_id": item["revision_id"],
                    "position": position,
                    "original_request": description,
                    "original_request_sha256": text_sha256(description),
                    "initial_git_baseline": "0" * 40,
                }
                for position, item in enumerate(items)
            ],
            "expected_focus_version": 0,
            "judge_sha256": judge_sha,
            "judge_manifest": judge_manifest,
        },
    }


def _admit_expired_grant(
    service, workspace_id, items, monkeypatch, *, description: str, operation_id=None
) -> tuple[dict, object]:
    """Admit a grant under the old clock so its expires_at is already past."""
    begin = _begin_grant(workspace_id, items, description=description)
    operation_id = operation_id or uuid4()
    with monkeypatch.context() as patch:
        patch.setattr("omp_work.v1.store.datetime", _expired_clock())
        status, body = _command(
            service, workspace_id, begin, operation_id=operation_id
        )
    assert status == 200, body
    return begin, operation_id


def _seed_grant_close_attempt(
    service,
    workspace_id,
    item: dict,
    begin: dict,
    *,
    position: int,
) -> dict:
    """Freeze a close attempt (candidate, receipts, manifest, launch) onto an expired grant.

    begin_close_attempt refuses an expired grant before it ever creates a
    delivery-bearing event, so the only reachable expiry scenario for the
    delivery, settle and cancel exemptions is a close attempt frozen into the
    grant before expiry. The attempt carries its own candidate so the fence-less
    settle/cancel handlers execute their real logic instead of seeing an item
    whose current candidate drifted away from the attempt.
    """
    work_id = UUID(str(item["work_id"]))
    revision_id = UUID(str(item["revision_id"]))
    attempt_id = uuid4()
    candidate_id = uuid4()
    candidate_sha256 = "d" * 64
    candidate_commit = "f" * 40
    plan_receipt_id = uuid4()
    verification_receipt_id = uuid4()
    manifest_id = uuid4()
    launch_id = uuid4()
    task_sha256 = sha256("seeded auditor task")
    authorization_ref = f"execution:{begin['payload']['grant_id']}:{position}:1"
    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as connection:
        connection.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        connection.execute(
            "INSERT INTO omp_work.candidates(candidate_id, workspace_id, work_id, revision_id, candidate_sha256, commit_sha, kind, allocated_at) VALUES (%s, %s, %s, %s, %s, %s, 'final', clock_timestamp())",
            (candidate_id, workspace_id, work_id, revision_id, candidate_sha256, candidate_commit),
        )
        connection.execute(
            "UPDATE omp_work.work_items SET current_candidate_id=%s WHERE workspace_id=%s AND work_id=%s",
            (candidate_id, workspace_id, work_id),
        )
        for receipt_id, kind in (
            (plan_receipt_id, "plan"),
            (verification_receipt_id, "verification"),
        ):
            connection.execute(
                "INSERT INTO omp_evidence.receipts(receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, payload, payload_sha256, issued_at) VALUES (%s, %s, %s, %s, %s, %s, '{}'::jsonb, %s, clock_timestamp())",
                (receipt_id, workspace_id, work_id, revision_id, candidate_id, kind, "a" * 64),
            )
        connection.execute(
            "INSERT INTO omp_work.close_attempts(attempt_id, workspace_id, work_id, revision_id, candidate_id, plan_receipt_id, candidate_sha256, candidate_commit, owner_session_id, owner_session_started_at, owner_session_start_commit, repository, diff_sha256, starting_dirty_paths, authorization_kind, authorization_ref, state, in_flight_launch_id, launch_count, execution_grant_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 's1', clock_timestamp(), %s, 'repo', %s, ARRAY[]::text[], 'execution', %s, 'auditor_in_flight', %s, 1, %s)",
            (
                attempt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                plan_receipt_id,
                candidate_sha256,
                candidate_commit,
                "b" * 40,
                "e" * 64,
                authorization_ref,
                launch_id,
                begin["payload"]["grant_id"],
            ),
        )
        connection.execute(
            "INSERT INTO omp_work.audit_manifests(manifest_id, workspace_id, work_id, attempt_id, manifest_version, plan_receipt_id, verification_receipt_id, candidate_id, candidate_sha256, candidate_commit, task_body, task_sha256, section_hashes) VALUES (%s, %s, %s, %s, 1, %s, %s, %s, %s, %s, 'seeded auditor task', %s, '{}'::jsonb)",
            (
                manifest_id,
                workspace_id,
                work_id,
                attempt_id,
                plan_receipt_id,
                verification_receipt_id,
                candidate_id,
                candidate_sha256,
                candidate_commit,
                task_sha256,
            ),
        )
        connection.execute(
            "INSERT INTO omp_work.auditor_launches(launch_id, workspace_id, attempt_id, manifest_id, launch_number, task_sha256, tool_call_id) VALUES (%s, %s, %s, %s, 1, %s, 'tc-seeded')",
            (launch_id, workspace_id, attempt_id, manifest_id, task_sha256),
        )
    return {"attempt_id": str(attempt_id), "launch_id": str(launch_id)}


def test_expired_grant_fence_sees_relation_and_focus_payloads(service, monkeypatch) -> None:
    """put_relation/remove_relation/set_focus name their item under `relation`/`slot`.

    A fence that only reads top-level work_id fields lets every one of these
    through, so an expired grant keeps receiving relation edits and focus moves.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Expired grant item", description="desc")
    begin, _ = _admit_expired_grant(
        service, workspace_id, [item], monkeypatch, description="desc"
    )
    grant_id = begin["payload"]["grant_id"]

    # A relation edge touching the grant's item (as source) is fenced.
    for remove in (False, True):
        relation_command = {
            "type": "remove_relation" if remove else "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": item["work_id"],
                    "target_work_id": str(uuid4()),
                    "kind": "related",
                    "active": True,
                }
            },
        }
        status, body = _command(service, workspace_id, relation_command)
        assert status == 409, body
        assert body["error"]["code"] == "execution_grant_stale", body

    # ... and as target, the other nesting the fence used to miss.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": str(uuid4()),
                    "target_work_id": item["work_id"],
                    "kind": "related",
                    "active": True,
                }
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_grant_stale", body

    # set_focus carries its item under `slot`, one level down.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_focus",
            "payload": {
                "slot": {
                    "workspace_id": str(workspace_id),
                    "owner_id": str(OWNER),
                    "work_id": item["work_id"],
                    "version": 0,
                },
                "expected_version": 1,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_grant_stale", body

    # The grant's item is untouched by the refusals.
    view = service.client.get(
        f"/v1/workspaces/{workspace_id}/execution/{grant_id}",
        headers=_owner_headers(workspace_id),
    ).json()
    assert view["items"][0]["phase"] == "criteria_pending"


def test_expired_grant_fence_allows_unrelated_relation_and_focus(service, monkeypatch) -> None:
    """The fence keys on grant membership, not command type: other items still move."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Expired grant item", description="desc")
    _admit_expired_grant(service, workspace_id, [item], monkeypatch, description="desc")

    outside = _create(service, workspace_id, "outside item", description="outside desc")
    other = _create(service, workspace_id, "other item", description="other desc")
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": outside["work_id"],
                    "target_work_id": other["work_id"],
                    "kind": "related",
                    "active": True,
                }
            },
        },
    )
    assert status == 200 and body["result"]["active"] is True, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_focus",
            "payload": {
                "slot": {
                    "workspace_id": str(workspace_id),
                    "owner_id": str(OWNER),
                    "work_id": outside["work_id"],
                    "version": 0,
                },
                "expected_version": 1,
            },
        },
    )
    assert status == 200 and body["result"]["work_id"] == outside["work_id"], body


def test_unexpired_grant_fence_allows_relation_and_focus_on_its_items(service) -> None:
    """An unexpired grant is no reason to refuse its own item's edits."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Grant item", description="desc")
    other = _create(service, workspace_id, "other item", description="other desc")
    begin = _begin_grant(workspace_id, [item], description="desc")
    status, body = _command(service, workspace_id, begin)
    assert status == 200, body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_relation",
            "payload": {
                "relation": {
                    "workspace_id": str(workspace_id),
                    "source_work_id": item["work_id"],
                    "target_work_id": other["work_id"],
                    "kind": "related",
                    "active": True,
                }
            },
        },
    )
    assert status == 200 and body["result"]["active"] is True, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_focus",
            "payload": {
                "slot": {
                    "workspace_id": str(workspace_id),
                    "owner_id": str(OWNER),
                    "work_id": item["work_id"],
                    "version": 0,
                },
                "expected_version": 1,
            },
        },
    )
    assert status == 200 and body["result"]["work_id"] == item["work_id"], body


def test_expired_grant_fence_keeps_delivery_and_auditor_settlement_usable(
    service, monkeypatch
) -> None:
    """Delivery attestation and auditor settle/cancel must survive expiry.

    An expired grant still holds close attempts with in-flight auditor launches;
    the fence must not strand their delivery, settle and cancel behind
    execution_grant_stale, or the owner can never drain the debt that
    reconciliation requires.
    """
    workspace_id = uuid4()
    _grant(service, workspace_id)
    settle_item = _create(service, workspace_id, "settle item", description="desc")
    cancel_item = _create(service, workspace_id, "cancel item", description="desc")
    begin, operation_id = _admit_expired_grant(
        service, workspace_id, [settle_item, cancel_item], monkeypatch, description="desc"
    )

    # Replay of the already-applied admission stays idempotent, not stale.
    status, body = _command(service, workspace_id, begin, operation_id=operation_id)
    assert status == 200 and body["receipt"]["state"] == "replayed", body

    # A close attempt frozen into the grant before expiry: settle stays usable,
    # so the launch burns and the attempt returns to audit_ready instead of the
    # fence stranding the in-flight debt behind execution_grant_stale.
    settle_attempt = _seed_grant_close_attempt(
        service, workspace_id, settle_item, begin, position=0
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "settle_auditor_launch",
            "payload": {
                "attempt_id": settle_attempt["attempt_id"],
                "launch_id": settle_attempt["launch_id"],
                "transport_failed": True,
            },
        },
    )
    assert status == 200 and body["result"]["event"]["reason_code"] == "transport_failed", body
    assert body["result"]["attempt"]["state"] == "audit_ready", body

    # A second item's in-flight attempt: cancel stays usable, delivery too.
    cancel_attempt = _seed_grant_close_attempt(
        service, workspace_id, cancel_item, begin, position=1
    )
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "cancel_auditor_launch",
            "payload": {
                "attempt_id": cancel_attempt["attempt_id"],
                "launch_id": cancel_attempt["launch_id"],
            },
        },
    )
    assert status == 200 and body["result"]["status"] == "applied", body

    view = service.client.get(
        f"/v1/work-items/{settle_item['key']}/workflow",
        headers=_owner_headers(workspace_id),
    ).json()
    deliverable = [
        event for event in view["close_attempt_events"] if event["requires_delivery"]
    ]
    assert deliverable, view
    for event in deliverable:
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "attest_checkpoint_delivery",
                "payload": {
                    "event_id": event["event_id"],
                    "owner_session_id": "session-test",
                    "rendered_sha256": event["rendered_sha256"],
                    "status": "delivered",
                },
            },
        )
        assert status == 200 and body["result"]["status"] == "applied", body


def test_expired_grant_fence_allows_owner_reconciliation_and_detects_paused(
    service, monkeypatch
) -> None:
    """A paused expired grant still fences, and pausing/stopping it stays legal."""
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "Expired grant item", description="desc")
    begin, _ = _admit_expired_grant(
        service, workspace_id, [item], monkeypatch, description="desc"
    )
    grant_id = begin["payload"]["grant_id"]
    judge_sha = begin["payload"]["judge_sha256"]

    # Pause under the fence, then prove the paused expired grant still fences.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 1,
                "target_state": "paused",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200 and body["result"]["grant"]["state"] == "paused", body

    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_focus",
            "payload": {
                "slot": {
                    "workspace_id": str(workspace_id),
                    "owner_id": str(OWNER),
                    "work_id": item["work_id"],
                    "version": 0,
                },
                "expected_version": 1,
            },
        },
    )
    assert status == 409 and body["error"]["code"] == "execution_grant_stale", body

    # clear_focus carries no item id: it stays allowed even while stale.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "clear_focus",
            "payload": {
                "workspace_id": str(workspace_id),
                "owner_id": str(OWNER),
                "expected_version": 1,
            },
        },
    )
    assert status == 200 and body["result"]["work_id"] is None, body

    # Owner reconciliation: stop the paused expired grant.
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "set_execution_state",
            "payload": {
                "grant_id": grant_id,
                "expected_grant_version": 2,
                "target_state": "stopped",
                "judge_sha256": judge_sha,
            },
        },
    )
    assert status == 200 and body["result"]["grant"]["state"] == "stopped", body
