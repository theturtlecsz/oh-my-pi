"""Live expired-grant fence: new effects refuse until the owner reconciles."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from omp_work.v1.canonical import text_sha256
from test_workflow_service import (
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
