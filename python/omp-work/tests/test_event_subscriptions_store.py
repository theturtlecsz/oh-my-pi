"""OMP-415-s05: PostgreSQL integration tests for event subscriptions store."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest

from test_workflow_service import OWNER, _command, _grant, _owner_headers

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _init_workspace(service) -> UUID:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    return workspace_id


def _get_watermark(service, workspace_id: UUID) -> int:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS wm FROM omp_audit.domain_events WHERE workspace_id = %s",
                (workspace_id,),
            )
            return int(cur.fetchone()[0])


def test_event_subscriptions_lifecycle_and_read_api(service) -> None:
    workspace_id = _init_workspace(service)
    sub_id = uuid4()

    # Domain watermark before subscription creation
    wm_before = _get_watermark(service, workspace_id)

    # 1. put_event_subscription: new id -> client_id = actor_id (OWNER), cursor = watermark
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
                "push_url": "https://hooks.example.com/events-v1",
                "event_types": ["mission.started", "important_finding"],
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "put_event_subscription"
    sub_1 = body["result"]["subscription"]
    assert sub_1["subscription_id"] == str(sub_id)
    assert sub_1["client_id"] == str(OWNER)
    assert sub_1["push_url"] == "https://hooks.example.com/events-v1"
    assert sub_1["event_types"] == ["mission.started", "important_finding"]
    assert sub_1["cursor_sequence"] == wm_before
    assert sub_1["deleted"] is False

    # Read back via GET: subscription is returned matching put result
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/event-subscriptions",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200
    subs = resp.json()["subscriptions"]
    assert len(subs) == 1
    assert subs[0] == sub_1

    # 2. re-put: replace push_url and event_types, keep cursor
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
                "push_url": "https://hooks.example.com/events-v2",
                "event_types": ["mission.completed", "mission.failed"],
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "put_event_subscription"
    sub_2 = body["result"]["subscription"]
    assert sub_2["subscription_id"] == str(sub_id)
    assert sub_2["client_id"] == str(OWNER)
    assert sub_2["push_url"] == "https://hooks.example.com/events-v2"
    assert sub_2["event_types"] == ["mission.completed", "mission.failed"]
    assert sub_2["cursor_sequence"] == wm_before  # cursor kept!
    assert sub_2["deleted"] is False

    # Read back via GET: updated push_url and event_types, preserved cursor
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/event-subscriptions",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200
    subs = resp.json()["subscriptions"]
    assert len(subs) == 1
    assert subs[0] == sub_2

    # 3. advance_event_cursor: equal -> no-op success
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(sub_id),
                "after_sequence": wm_before,
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "advance_event_cursor"
    assert body["result"]["subscription"]["cursor_sequence"] == wm_before

    # Advance to current watermark (watermark moved forward due to the put commands)
    current_wm = _get_watermark(service, workspace_id)
    assert current_wm > wm_before
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(sub_id),
                "after_sequence": current_wm,
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "advance_event_cursor"
    sub_adv = body["result"]["subscription"]
    assert sub_adv["cursor_sequence"] == current_wm
    assert sub_adv["deleted"] is False

    # Read back via GET: cursor advanced
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/event-subscriptions",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200
    subs = resp.json()["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["cursor_sequence"] == current_wm

    # 4. delete_event_subscription: result carries subscription with deleted=true
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "delete_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
            },
        },
    )
    assert status == 200
    assert body["result"]["type"] == "delete_event_subscription"
    del_sub = body["result"]["subscription"]
    assert del_sub["subscription_id"] == str(sub_id)
    assert del_sub["deleted"] is True
    assert del_sub["cursor_sequence"] == current_wm

    # Read back via GET: deleted subscription is absent
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/event-subscriptions",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200
    assert len(resp.json()["subscriptions"]) == 0


def test_event_subscription_cursor_equals_watermark(service) -> None:
    workspace_id = _init_workspace(service)

    # First subscription created at initial watermark
    wm_0 = _get_watermark(service, workspace_id)
    sub_a_id = uuid4()
    status_a, body_a = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_a_id),
                "event_types": ["mission.started"],
            },
        },
    )
    assert status_a == 200
    assert body_a["result"]["subscription"]["cursor_sequence"] == wm_0

    # Second subscription created at updated watermark (wm_0 + 1 from first command)
    wm_1 = _get_watermark(service, workspace_id)
    assert wm_1 > wm_0
    sub_b_id = uuid4()
    status_b, body_b = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_b_id),
                "event_types": ["mission.completed"],
            },
        },
    )
    assert status_b == 200
    assert body_b["result"]["subscription"]["cursor_sequence"] == wm_1


def test_ops_streams_subscriptions_store_like_mission_ones(service) -> None:
    workspace_id = _init_workspace(service)

    # ops.alarm subscription
    alarm_id = uuid4()
    status_alarm, body_alarm = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(alarm_id),
                "push_url": "https://ops.example.com/alarm",
                "event_types": ["ops.alarm"],
            },
        },
    )
    assert status_alarm == 200
    assert body_alarm["result"]["type"] == "put_event_subscription"
    assert body_alarm["result"]["subscription"]["event_types"] == ["ops.alarm"]

    # ops.digest subscription
    digest_id = uuid4()
    status_digest, body_digest = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(digest_id),
                "push_url": "https://ops.example.com/digest",
                "event_types": ["ops.digest"],
            },
        },
    )
    assert status_digest == 200
    assert body_digest["result"]["type"] == "put_event_subscription"
    assert body_digest["result"]["subscription"]["event_types"] == ["ops.digest"]

    # Read back via GET: both ops subscriptions stored and readable
    resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/event-subscriptions",
        headers=_owner_headers(workspace_id),
    )
    assert resp.status_code == 200
    sub_ids = {s["subscription_id"] for s in resp.json()["subscriptions"]}
    assert str(alarm_id) in sub_ids
    assert str(digest_id) in sub_ids


def test_event_subscriptions_refusals(service) -> None:
    workspace_id = _init_workspace(service)
    sub_id = uuid4()

    # Put initial subscription
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
                "client_id": str(OWNER),
                "event_types": ["mission.started"],
            },
        },
    )
    assert status == 200
    cursor = body["result"]["subscription"]["cursor_sequence"]

    # 1. put_event_subscription: payload client_id differing from stored -> invalid_request "client_id_immutable"
    other_client = uuid4()
    status_bad_client, body_bad_client = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
                "client_id": str(other_client),
                "event_types": ["mission.started"],
            },
        },
    )
    assert status_bad_client == 400
    assert body_bad_client["error"]["code"] == "invalid_request"
    assert "client_id_immutable" in body_bad_client["error"]["diagnostics"]

    # 2. advance_event_cursor: unknown -> invalid_request "subscription_not_found"
    unknown_id = uuid4()
    status_adv_unknown, body_adv_unknown = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(unknown_id),
                "after_sequence": 0,
            },
        },
    )
    assert status_adv_unknown == 400
    assert body_adv_unknown["error"]["code"] == "invalid_request"
    assert "subscription_not_found" in body_adv_unknown["error"]["diagnostics"]

    # 3. advance_event_cursor: above watermark -> invalid_request "cursor_ahead"
    watermark = _get_watermark(service, workspace_id)
    status_adv_ahead, body_adv_ahead = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(sub_id),
                "after_sequence": watermark + 100,
            },
        },
    )
    assert status_adv_ahead == 400
    assert body_adv_ahead["error"]["code"] == "invalid_request"
    assert "cursor_ahead" in body_adv_ahead["error"]["diagnostics"]

    # Advance cursor to current watermark
    status_adv_ok, _ = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(sub_id),
                "after_sequence": watermark,
            },
        },
    )
    assert status_adv_ok == 200

    # 4. advance_event_cursor: after_sequence below cursor -> revision_conflict "cursor_regression"
    if watermark > 0:
        status_regress, body_regress = _command(
            service,
            workspace_id,
            {
                "type": "advance_event_cursor",
                "payload": {
                    "subscription_id": str(sub_id),
                    "after_sequence": watermark - 1,
                },
            },
        )
        assert status_regress == 409
        assert body_regress["error"]["code"] == "revision_conflict"
        assert "cursor_regression" in body_regress["error"]["diagnostics"]

    # 5. delete_event_subscription: unknown -> invalid_request "subscription_not_found"
    status_del_unknown, body_del_unknown = _command(
        service,
        workspace_id,
        {
            "type": "delete_event_subscription",
            "payload": {
                "subscription_id": str(unknown_id),
            },
        },
    )
    assert status_del_unknown == 400
    assert body_del_unknown["error"]["code"] == "invalid_request"
    assert "subscription_not_found" in body_del_unknown["error"]["diagnostics"]

    # Delete the subscription
    status_del, _ = _command(
        service,
        workspace_id,
        {
            "type": "delete_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
            },
        },
    )
    assert status_del == 200

    # 6. later put -> revision_conflict "subscription_deleted"
    status_put_del, body_put_del = _command(
        service,
        workspace_id,
        {
            "type": "put_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
                "event_types": ["mission.started"],
            },
        },
    )
    assert status_put_del == 409
    assert body_put_del["error"]["code"] == "revision_conflict"
    assert "subscription_deleted" in body_put_del["error"]["diagnostics"]

    # 7. later delete -> revision_conflict "subscription_deleted"
    status_del_del, body_del_del = _command(
        service,
        workspace_id,
        {
            "type": "delete_event_subscription",
            "payload": {
                "subscription_id": str(sub_id),
            },
        },
    )
    assert status_del_del == 409
    assert body_del_del["error"]["code"] == "revision_conflict"
    assert "subscription_deleted" in body_del_del["error"]["diagnostics"]

    # 8. later advance -> revision_conflict "subscription_deleted"
    status_adv_del, body_adv_del = _command(
        service,
        workspace_id,
        {
            "type": "advance_event_cursor",
            "payload": {
                "subscription_id": str(sub_id),
                "after_sequence": 0,
            },
        },
    )
    assert status_adv_del == 409
    assert body_adv_del["error"]["code"] == "revision_conflict"
    assert "subscription_deleted" in body_adv_del["error"]["diagnostics"]
