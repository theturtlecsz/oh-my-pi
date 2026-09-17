"""Disposable PostgreSQL contract tests for PR3 budget authority."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
import threading
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
import pytest

from omp_work import contract_sha256
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.server import create_app
from test_workflow_service import _command, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _setup(service):
    workspace_id = uuid4()
    _grant(service, workspace_id)
    account_id, scope_id = uuid4(), uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (workspace_id,))
        conn.execute("GRANT SELECT ON omp_work.provider_accounts TO omp_work_app")
        conn.execute("GRANT REFERENCES ON omp_work.provider_accounts TO omp_work_app")
        assert conn.execute("SELECT has_table_privilege('omp_work_app','omp_work.provider_accounts','SELECT')").fetchone()[0] is True
        conn.execute("SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)", (str(workspace_id), str(uuid4())))
        conn.execute("INSERT INTO omp_work.provider_accounts(account_id,workspace_id,provider,account_identity,entitlement_evidence,evidence_observed_at,billing_mode,balance_provenance,concurrency_limit) VALUES(%s,%s,'fixture','account','fixture-evidence',clock_timestamp(),'subscription','provider_observed',1)", (account_id, workspace_id))
    status, result = _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(scope_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "1.00"}}})
    assert status == 200, result
    return workspace_id, account_id, scope_id


def _reserve(service, workspace_id, account_id, scope_id, *, operation_id=None, amount="1.00", logical_call_id=None, transport_attempt_id=None, expires_at="2030-01-01T00:00:00+00:00"):
    attempt = transport_attempt_id or uuid4()
    return _command(service, workspace_id, {"type": "reserve_budget", "payload": {"scope_id": str(scope_id), "account_id": str(account_id), "logical_call_id": str(logical_call_id or uuid4()), "transport_attempt_id": str(attempt), "provider": "fixture", "model": "cheap", "effort": "low", "resource": "included_credit", "worst_case_drawdown": amount, "context_limit": 1000, "output_limit": 100, "expires_at": expires_at}}, operation_id=operation_id)


def _expire(service, workspace_id, reservation_id, logical_call_id, transport_attempt_id, fence, expected_state, *, operation_id=None):
    return _command(
        service,
        workspace_id,
        {
            "type": "expire_budget",
            "payload": {
                "reservation_id": str(reservation_id),
                "logical_call_id": str(logical_call_id),
                "transport_attempt_id": str(transport_attempt_id),
                "fence": fence,
                "expected_state": expected_state,
            },
        },
        operation_id=operation_id,
    )


def test_budget_reservation_claim_settlement_and_unknown_evidence_are_durable(service):
    workspace_id, account_id, scope_id = _setup(service)
    status, result = _reserve(service, workspace_id, account_id, scope_id)
    assert status == 200, result
    reservation_id, fence = result["result"]["reservation_id"], result["result"]["fence"]
    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": reservation_id, "fence": fence}})
    assert status == 200 and claimed["result"]["state"] == "potentially_sent"
    status, settled = _command(service, workspace_id, {"type": "settle_budget", "payload": {"reservation_id": reservation_id, "transport_attempt_id": result["result"]["transport_attempt_id"], "fence": fence, "state": "unresolved", "actual_drawdown": "0.25", "provenance": "unknown", "outcome": "timeout"}})
    assert status == 200 and settled["result"]["state"] == "unresolved", settled
    status, denied = _reserve(service, workspace_id, account_id, scope_id, amount="0.01")
    assert status == 409 and denied["error"]["code"] in {"budget_exhausted", "invalid_request"}


def test_budget_duplicate_operation_replays_and_unsent_cancel_requires_fence(service):
    workspace_id, account_id, scope_id = _setup(service)
    operation_id = uuid4()
    logical_call_id, transport_attempt_id = uuid4(), uuid4()
    status, first = _reserve(service, workspace_id, account_id, scope_id, operation_id=operation_id, logical_call_id=logical_call_id, transport_attempt_id=transport_attempt_id)
    assert status == 200, first
    status, replay = _reserve(service, workspace_id, account_id, scope_id, operation_id=operation_id, logical_call_id=logical_call_id, transport_attempt_id=transport_attempt_id)
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    reservation_id, fence = first["result"]["reservation_id"], first["result"]["fence"]
    status, refused = _command(service, workspace_id, {"type": "cancel_budget", "payload": {"reservation_id": reservation_id, "fence": fence + 1, "verified_unsent": True}})
    assert status == 400
    status, cancelled = _command(service, workspace_id, {"type": "cancel_budget", "payload": {"reservation_id": reservation_id, "fence": fence, "verified_unsent": True}})
    assert status == 200 and cancelled["result"]["state"] == "cancelled_unsent"


def test_budget_account_slot_and_trusted_frontier_boundary(service):
    workspace_id, account_id, scope_id = _setup(service)
    status, first = _reserve(service, workspace_id, account_id, scope_id, amount="0.50")
    assert status == 200, first
    status, denied = _reserve(service, workspace_id, account_id, scope_id, amount="0.10")
    assert status == 409 and denied["error"]["code"] == "budget_exhausted"
    question = {"type": "issue_frontier_exception", "payload": {"scope_id": str(scope_id), "question": "why?", "route": "frontier", "effort": "medium", "context_limit": 100, "output_limit": 20, "max_attempts": 1, "resource": "included_credit", "resource_limit": "0.10", "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}}
    status, denied_exception = _command(service, workspace_id, question)
    assert status == 403


def _settle(
    service,
    workspace_id,
    reservation_id,
    transport_attempt_id,
    fence,
    *,
    state="settled",
    amount="0.40",
    outcome="success",
    provenance="provider_observed",
    usage=None,
    provider_request_id=None,
    operation_id=None,
):
    return _command(
        service,
        workspace_id,
        {
            "type": "settle_budget",
            "payload": {
                "reservation_id": str(reservation_id),
                "transport_attempt_id": str(transport_attempt_id),
                "fence": fence,
                "state": state,
                "actual_drawdown": amount,
                "usage": usage or {},
                "provenance": provenance,
                "provider_request_id": provider_request_id,
                "outcome": outcome,
            },
        },
        operation_id=operation_id,
    )


def _inspect_accounting(service, scope_id, reservation_id=None):
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    ) as conn:
        scope = conn.execute(
            "SELECT held, spent, unresolved FROM omp_work.budget_scopes WHERE scope_id = %s",
            (scope_id,),
        ).fetchone()
        reservation = (
            conn.execute(
                "SELECT state, actual_drawdown FROM omp_work.budget_reservations WHERE reservation_id = %s",
                (reservation_id,),
            ).fetchone()
            if reservation_id
            else None
        )
        return scope, reservation


def test_unresolved_budget_reservation_reconciles_and_releases_allowance(service):
    workspace_id, account_id, scope_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.provider_accounts SET concurrency_limit = 5 WHERE account_id = %s", (account_id,))

    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.60")
    assert status == 200, res
    res_id, attempt_id, fence = res["result"]["reservation_id"], res["result"]["transport_attempt_id"], res["result"]["fence"]

    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200 and claimed["result"]["state"] == "potentially_sent"

    status, unresolved = _settle(service, workspace_id, res_id, attempt_id, fence, state="unresolved", amount="0.60", outcome="timeout", provenance="unknown")
    assert status == 200 and unresolved["result"]["state"] == "unresolved"

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["unresolved"].get("included_credit") == "0.60" and reservation["state"] == "unresolved"

    status, blocked = _reserve(service, workspace_id, account_id, scope_id, amount="0.50")
    assert status == 409 and blocked["error"]["code"] == "budget_exhausted"

    status, cancel_refused = _command(service, workspace_id, {"type": "cancel_budget", "payload": {"reservation_id": res_id, "fence": fence, "verified_unsent": True}})
    assert status == 400 and cancel_refused["error"]["code"] == "invalid_request"

    status, stale = _settle(service, workspace_id, res_id, attempt_id, fence + 1, amount="0.40")
    assert status == 400 and stale["error"]["code"] == "invalid_request"

    status, wrong_attempt = _settle(service, workspace_id, res_id, uuid4(), fence, amount="0.40")
    assert status == 400 and wrong_attempt["error"]["code"] == "invalid_request"

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["unresolved"].get("included_credit") == "0.60" and scope["spent"].get("included_credit", "0") == "0" and reservation["state"] == "unresolved"

    reconcile_op = uuid4()
    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="0.40", operation_id=reconcile_op)
    assert status == 200 and settled["result"]["state"] == "settled"

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["unresolved"].get("included_credit") == "0" and scope["spent"].get("included_credit") == "0.40"
    assert reservation["state"] == "settled" and str(reservation["actual_drawdown"]) == "0.40"

    status, replay = _settle(service, workspace_id, res_id, attempt_id, fence, amount="0.40", operation_id=reconcile_op)
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    scope, _ = _inspect_accounting(service, scope_id)
    assert scope["unresolved"].get("included_credit") == "0" and scope["spent"].get("included_credit") == "0.40"

    status, permitted = _reserve(service, workspace_id, account_id, scope_id, amount="0.50")
    assert status == 200, permitted

    status, safe_replay = _settle(service, workspace_id, res_id, attempt_id, fence, amount="0.40")
    assert status == 200 and safe_replay["result"]["replayed"] is True

    status, conflict = _settle(service, workspace_id, res_id, attempt_id, fence, amount="0.99")
    assert status == 409 and conflict["error"]["code"] == "idempotency_conflict"
    scope, _ = _inspect_accounting(service, scope_id)
    assert scope["spent"].get("included_credit") == "0.40"


def test_unresolved_reconciliation_survives_restart_boundary(service):
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()

    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200 and claimed["result"]["state"] == "potentially_sent"

    status, unresolved = _settle(service, workspace_id, res_id, attempt_id, fence, state="unresolved", amount="0.35", outcome="timeout", provenance="unknown")
    assert status == 200 and unresolved["result"]["state"] == "unresolved"

    fresh_service = SimpleNamespace(
        client=TestClient(create_app(service.config, capabilities_dir=service.capabilities)),
        capabilities=service.capabilities,
        config=service.config,
    )

    status, settled = _settle(fresh_service, workspace_id, res_id, attempt_id, fence, amount="0.30")
    assert status == 200 and settled["result"]["state"] == "settled"

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["unresolved"].get("included_credit") == "0" and scope["spent"].get("included_credit") == "0.30"
    assert reservation["state"] == "settled"

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM omp_work.budget_reservations WHERE workspace_id = %s AND logical_call_id = %s", (workspace_id, call_id)).fetchone()[0] == 1


def test_parent_limit_rejects_when_child_permits_counters_unchanged(service):
    workspace_id, account_id, parent_scope_id = _setup(service)
    child_scope_id = uuid4()
    status, res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(child_scope_id),
                "parent_scope_id": str(parent_scope_id),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "5.00"},
            },
        },
    )
    assert status == 200, res

    status, denied = _reserve(service, workspace_id, account_id, child_scope_id, amount="2.00")
    assert status == 409, denied
    assert denied["error"]["code"] == "budget_exhausted"

    child_scope, _ = _inspect_accounting(service, child_scope_id)
    parent_scope, _ = _inspect_accounting(service, parent_scope_id)
    assert child_scope["held"].get("included_credit", "0") == "0"
    assert child_scope["spent"].get("included_credit", "0") == "0"
    assert child_scope["unresolved"].get("included_credit", "0") == "0"
    assert parent_scope["held"].get("included_credit", "0") == "0"
    assert parent_scope["spent"].get("included_credit", "0") == "0"
    assert parent_scope["unresolved"].get("included_credit", "0") == "0"


def test_concurrent_sibling_reservations_share_parent_limit(service):
    workspace_id, account_id, parent_scope_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.provider_accounts SET concurrency_limit = 5 WHERE account_id = %s", (account_id,))
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"1.50\"}' WHERE scope_id = %s", (parent_scope_id,))

    sib1_id, sib2_id = uuid4(), uuid4()
    for sid in (sib1_id, sib2_id):
        status, res = _command(
            service,
            workspace_id,
            {
                "type": "create_budget_scope",
                "payload": {
                    "scope_id": str(sid),
                    "parent_scope_id": str(parent_scope_id),
                    "kind": "session",
                    "policy_version": "economy-v1",
                    "limits": {"included_credit": "2.00"},
                },
            },
        )
        assert status == 200, res

    barrier = threading.Barrier(2)
    results = {}

    def worker(sib_id):
        barrier.wait()
        status, res = _reserve(service, workspace_id, account_id, sib_id, amount="1.00")
        results[sib_id] = (status, res)

    t1 = threading.Thread(target=worker, args=(sib1_id,))
    t2 = threading.Thread(target=worker, args=(sib2_id,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [results[sib1_id][0], results[sib2_id][0]]
    assert sorted(statuses) == [200, 409]

    parent_scope, _ = _inspect_accounting(service, parent_scope_id)
    assert parent_scope["held"].get("included_credit") == "1.00"
    assert parent_scope["spent"].get("included_credit", "0") == "0"
    assert parent_scope["unresolved"].get("included_credit", "0") == "0"

    winner_id = sib1_id if results[sib1_id][0] == 200 else sib2_id
    loser_id = sib2_id if winner_id == sib1_id else sib1_id
    win_scope, _ = _inspect_accounting(service, winner_id)
    lose_scope, _ = _inspect_accounting(service, loser_id)
    assert win_scope["held"].get("included_credit") == "1.00"
    assert lose_scope["held"].get("included_credit", "0") == "0"


def test_three_level_reserve_then_settle_updates_all_levels_once(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    mid_id, leaf_id = uuid4(), uuid4()
    status, res = _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(mid_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    assert status == 200, res
    status, res = _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(leaf_id), "parent_scope_id": str(mid_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    assert status == 200, res

    status, res = _reserve(service, workspace_id, account_id, leaf_id, amount="3.00")
    assert status == 200, res
    res_id, attempt_id, fence = res["result"]["reservation_id"], res["result"]["transport_attempt_id"], res["result"]["fence"]

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit") == "3.00"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit", "0") == "0"

    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200

    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="2.50")
    assert status == 200 and settled["result"]["state"] == "settled"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit") == "2.50"
        assert scope["unresolved"].get("included_credit", "0") == "0"


def test_unresolved_then_reconciled_settlement_updates_all_levels_once(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    mid_id, leaf_id = uuid4(), uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(mid_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(leaf_id), "parent_scope_id": str(mid_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})

    status, res = _reserve(service, workspace_id, account_id, leaf_id, amount="2.00")
    assert status == 200, res
    res_id, attempt_id, fence = res["result"]["reservation_id"], res["result"]["transport_attempt_id"], res["result"]["fence"]

    _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})

    status, unresolved = _settle(service, workspace_id, res_id, attempt_id, fence, state="unresolved", amount="2.00", outcome="timeout", provenance="unknown")
    assert status == 200 and unresolved["result"]["state"] == "unresolved"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit") == "2.00"

    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.75")
    assert status == 200 and settled["result"]["state"] == "settled"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit") == "1.75"
        assert scope["unresolved"].get("included_credit", "0") == "0"


def test_cancellation_releases_held_on_all_levels(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    mid_id, leaf_id = uuid4(), uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(mid_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(leaf_id), "parent_scope_id": str(mid_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})

    status, res = _reserve(service, workspace_id, account_id, leaf_id, amount="4.00")
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit") == "4.00"

    status, cancelled = _command(service, workspace_id, {"type": "cancel_budget", "payload": {"reservation_id": res_id, "fence": fence, "verified_unsent": True}})
    assert status == 200 and cancelled["result"]["state"] == "cancelled_unsent"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit", "0") == "0"


def test_same_operation_replay_never_double_applies_chain_counters(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    child_id = uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(child_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})

    op_reserve = uuid4()
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, child_id, amount="1.50", operation_id=op_reserve, logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    status, replay_res = _reserve(service, workspace_id, account_id, child_id, amount="1.50", operation_id=op_reserve, logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200 and replay_res["receipt"]["state"] == "replayed"

    for sid in (root_id, child_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit") == "1.50"
        assert scope["spent"].get("included_credit", "0") == "0"

    op_settle = uuid4()
    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.20", operation_id=op_settle)
    assert status == 200

    for sid in (root_id, child_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit") == "1.20"

    status, replay_settle = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.20", operation_id=op_settle)
    assert status == 200 and replay_settle["receipt"]["state"] == "replayed"

    for sid in (root_id, child_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit") == "1.20"


def test_cross_workspace_parent_remains_rejected(service):
    ws1, account1, scope1 = _setup(service)
    ws2 = uuid4()
    _grant(service, ws2)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (ws2,))

    child_scope_id = uuid4()
    status, res = _command(
        service,
        ws2,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(child_scope_id),
                "parent_scope_id": str(scope1),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "5.00"},
            },
        },
    )
    assert status == 400
    assert res["error"]["code"] == "invalid_request"
    assert "parent_scope_not_found" in res["error"]["diagnostics"]


def test_injected_aggregate_drift_below_zero_aborts_transaction(service):
    workspace_id, account_id, root_id = _setup(service)
    child_id = uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(child_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "1.00"}}})

    status, res = _reserve(service, workspace_id, account_id, child_id, amount="1.00")
    assert status == 200
    res_id, attempt_id, fence = res["result"]["reservation_id"], res["result"]["transport_attempt_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET held = '{\"included_credit\": \"0\"}' WHERE scope_id = %s", (root_id,))

    status, err = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.00")
    assert status == 409
    assert err["error"]["code"] == "cutover_invariant"

    child_scope, _ = _inspect_accounting(service, child_id)
    root_scope, reservation = _inspect_accounting(service, root_id, res_id)
    assert child_scope["held"].get("included_credit") == "1.00"
    assert child_scope["spent"].get("included_credit", "0") == "0"
    assert root_scope["held"].get("included_credit", "0") == "0"
    assert root_scope["spent"].get("included_credit", "0") == "0"
    assert reservation["state"] == "reserved_unsent"


def test_reserve_rejects_already_expired_reservation(service):
    workspace_id, account_id, scope_id = _setup(service)
    status, denied = _reserve(
        service,
        workspace_id,
        account_id,
        scope_id,
        expires_at="2020-01-01T00:00:00+00:00",
    )
    assert status == 400
    assert denied["error"]["code"] == "invalid_request"
    assert "reservation_already_expired" in denied["error"]["diagnostics"]

    scope, _ = _inspect_accounting(service, scope_id)
    assert scope["held"].get("included_credit", "0") == "0"
    assert scope["spent"].get("included_credit", "0") == "0"
    assert scope["unresolved"].get("included_credit", "0") == "0"
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM omp_work.budget_reservations WHERE workspace_id = %s", (workspace_id,)).fetchone()[0] == 0


def test_claim_refuses_after_expiry_without_mutation(service):
    workspace_id, account_id, scope_id = _setup(service)
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50")
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    status, denied = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 400
    assert denied["error"]["code"] == "invalid_request"
    assert "reservation_expired" in denied["error"]["diagnostics"]

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit") == "0.50"
    assert scope["spent"].get("included_credit", "0") == "0"
    assert scope["unresolved"].get("included_credit", "0") == "0"
    assert reservation["state"] == "reserved_unsent"


def test_expire_reserved_unsent_releases_all_levels_exactly_once(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    mid_id, leaf_id = uuid4(), uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(mid_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(leaf_id), "parent_scope_id": str(mid_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})

    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, leaf_id, amount="3.00", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    expire_op = uuid4()
    status, expired = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent", operation_id=expire_op)
    assert status == 200, expired
    assert expired["result"]["state"] == "cancelled_unsent"
    assert expired["result"]["actual_drawdown"] is None

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit", "0") == "0"

    with psycopg.connect(**service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute("SELECT state, actual_drawdown, outcome, provenance, settled_at FROM omp_work.budget_reservations WHERE reservation_id = %s", (res_id,)).fetchone()
        assert row["state"] == "cancelled_unsent"
        assert row["actual_drawdown"] is None
        assert row["outcome"] == "timeout"
        assert row["provenance"] == "unknown"
        assert row["settled_at"] is not None

    # Same operation replays
    status, replay = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent", operation_id=expire_op)
    assert status == 200 and replay["receipt"]["state"] == "replayed"
    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"

    # Fresh operation against terminal state refuses without double accounting
    status, fresh_refusal = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent")
    assert status == 400
    assert fresh_refusal["error"]["code"] == "invalid_request"
    assert "reservation_state_mismatch" in fresh_refusal["error"]["diagnostics"]
    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"

    # Cancel and settle on the row refuse
    status, cancel_refused = _command(service, workspace_id, {"type": "cancel_budget", "payload": {"reservation_id": res_id, "fence": fence, "verified_unsent": True}})
    assert status == 400
    status, settle_refused = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.00")
    assert status == 400


def test_expire_potentially_sent_preserves_charge_then_settlement_reconciles(service):
    workspace_id, account_id, root_id = _setup(service)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"10.00\"}' WHERE scope_id = %s", (root_id,))

    mid_id, leaf_id = uuid4(), uuid4()
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(mid_id), "parent_scope_id": str(root_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    _command(service, workspace_id, {"type": "create_budget_scope", "payload": {"scope_id": str(leaf_id), "parent_scope_id": str(mid_id), "kind": "session", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})

    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, leaf_id, amount="2.00", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200 and claimed["result"]["state"] == "potentially_sent"

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    status, expired = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "potentially_sent")
    assert status == 200, expired
    assert expired["result"]["state"] == "unresolved"
    assert expired["result"]["actual_drawdown"] == "2.00"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit") == "2.00"

    with psycopg.connect(**service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute("SELECT state, actual_drawdown, outcome, provenance FROM omp_work.budget_reservations WHERE reservation_id = %s", (res_id,)).fetchone()
        assert row["state"] == "unresolved"
        assert str(row["actual_drawdown"]) == "2.00"
        assert row["outcome"] == "timeout"
        assert row["provenance"] == "unknown"

    # Set parent limit so that adding another reservation exceeding remaining fails
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_scopes SET limits = '{\"included_credit\": \"2.50\"}' WHERE scope_id = %s", (root_id,))

    status, denied = _reserve(service, workspace_id, account_id, leaf_id, amount="1.00")
    assert status == 409
    assert denied["error"]["code"] == "budget_exhausted"

    # Later settlement reconciles: subtracts stored prior (2.00) and adds final actual (1.75)
    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="1.75")
    assert status == 200 and settled["result"]["state"] == "settled"

    for sid in (root_id, mid_id, leaf_id):
        scope, _ = _inspect_accounting(service, sid)
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit", "0") == "0"
        assert scope["spent"].get("included_credit") == "1.75"


def test_expire_not_yet_expired_refuses_without_counter_change(service):
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    # Expiry before expiration time refuses
    status, denied = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent")
    assert status == 400
    assert denied["error"]["code"] == "invalid_request"
    assert "reservation_not_expired" in denied["error"]["diagnostics"]

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit") == "0.50"
    assert reservation["state"] == "reserved_unsent"

    # Claim, then attempt expire on potentially_sent while still nonexpired
    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200

    status, denied_sent = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "potentially_sent")
    assert status == 400
    assert denied_sent["error"]["code"] == "invalid_request"
    assert "reservation_not_expired" in denied_sent["error"]["diagnostics"]

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit") == "0.50"
    assert scope["unresolved"].get("included_credit", "0") == "0"
    assert reservation["state"] == "potentially_sent"


def test_expire_binds_identity_and_workspace(service):
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    # Wrong fence
    status, denied = _expire(service, workspace_id, res_id, call_id, attempt_id, fence + 1, "reserved_unsent")
    assert status == 400 and "reservation_identity_invalid" in denied["error"]["diagnostics"]

    # Wrong transport_attempt_id
    status, denied = _expire(service, workspace_id, res_id, call_id, uuid4(), fence, "reserved_unsent")
    assert status == 400 and "reservation_identity_invalid" in denied["error"]["diagnostics"]

    # Wrong logical_call_id
    status, denied = _expire(service, workspace_id, res_id, uuid4(), attempt_id, fence, "reserved_unsent")
    assert status == 400 and "reservation_identity_invalid" in denied["error"]["diagnostics"]

    # Wrong expected_state
    status, denied = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "potentially_sent")
    assert status == 400 and "reservation_state_mismatch" in denied["error"]["diagnostics"]

    # Wrong workspace
    ws2 = uuid4()
    _grant(service, ws2)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (ws2,))
    status, denied = _expire(service, ws2, res_id, call_id, attempt_id, fence, "reserved_unsent")
    assert status == 400 and "reservation_identity_invalid" in denied["error"]["diagnostics"]

    # Stored state unchanged
    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit") == "0.50"
    assert scope["spent"].get("included_credit", "0") == "0"
    assert reservation["state"] == "reserved_unsent"


def test_expire_committed_replay_through_fresh_client_and_conflict(service):
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    op = uuid4()
    status, expired = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent", operation_id=op)
    assert status == 200, expired
    stored_result = expired["result"]

    fresh_service = SimpleNamespace(
        client=TestClient(create_app(service.config, capabilities_dir=service.capabilities)),
        capabilities=service.capabilities,
        config=service.config,
    )
    status, replay = _expire(fresh_service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent", operation_id=op)
    assert status == 200
    assert replay["receipt"]["state"] == "replayed"
    assert replay["result"] == stored_result

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit", "0") == "0"
    assert reservation["state"] == "cancelled_unsent"

    # Changed payload under same operation id conflicts
    status, conflict = _expire(fresh_service, workspace_id, res_id, uuid4(), attempt_id, fence, "reserved_unsent", operation_id=op)
    assert status == 409
    assert conflict["error"]["code"] == "idempotency_conflict"

    scope, _ = _inspect_accounting(service, scope_id)
    assert scope["held"].get("included_credit", "0") == "0"


def test_concurrent_claim_and_expire_have_one_winner(service):
    # (a) Backdated reservation: claim vs expire -> expire wins
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.50", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    barrier_a = threading.Barrier(2)
    results_a = {}

    def worker_claim_a():
        barrier_a.wait()
        results_a["claim"] = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})

    def worker_expire_a():
        barrier_a.wait()
        results_a["expire"] = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "reserved_unsent")

    t1 = threading.Thread(target=worker_claim_a)
    t2 = threading.Thread(target=worker_expire_a)
    t1.start(); t2.start()
    t1.join(); t2.join()

    statuses_a = [results_a["claim"][0], results_a["expire"][0]]
    assert sorted(statuses_a) == [200, 400]
    assert results_a["expire"][0] == 200
    assert results_a["claim"][0] == 400

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit", "0") == "0"
    assert reservation["state"] == "cancelled_unsent"

    # (b) Far-future reservation: claim vs expire -> claim wins
    status, res2 = _reserve(service, workspace_id, account_id, scope_id, amount="0.50")
    assert status == 200, res2
    res2_id, fence2 = res2["result"]["reservation_id"], res2["result"]["fence"]
    call2_id, attempt2_id = uuid4(), uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET logical_call_id = %s, transport_attempt_id = %s WHERE reservation_id = %s", (call2_id, attempt2_id, res2_id))

    barrier_b = threading.Barrier(2)
    results_b = {}

    def worker_claim_b():
        barrier_b.wait()
        results_b["claim"] = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res2_id, "fence": fence2}})

    def worker_expire_b():
        barrier_b.wait()
        results_b["expire"] = _expire(service, workspace_id, res2_id, call2_id, attempt2_id, fence2, "reserved_unsent")

    t3 = threading.Thread(target=worker_claim_b)
    t4 = threading.Thread(target=worker_expire_b)
    t3.start(); t4.start()
    t3.join(); t4.join()

    statuses_b = [results_b["claim"][0], results_b["expire"][0]]
    assert sorted(statuses_b) == [200, 400]
    assert results_b["claim"][0] == 200
    assert results_b["expire"][0] == 400

    scope2, reservation2 = _inspect_accounting(service, scope_id, res2_id)
    assert scope2["held"].get("included_credit") == "0.50"
    assert reservation2["state"] == "potentially_sent"


def test_late_provider_settlement_on_expired_row_wins_then_expire_refuses(service):
    workspace_id, account_id, scope_id = _setup(service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve(service, workspace_id, account_id, scope_id, amount="0.80", logical_call_id=call_id, transport_attempt_id=attempt_id)
    assert status == 200, res
    res_id, fence = res["result"]["reservation_id"], res["result"]["fence"]

    status, claimed = _command(service, workspace_id, {"type": "claim_budget", "payload": {"reservation_id": res_id, "fence": fence}})
    assert status == 200

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '1 second' WHERE reservation_id = %s", (res_id,))

    # Late provider settlement arrives before expiry command is issued
    status, settled = _settle(service, workspace_id, res_id, attempt_id, fence, amount="0.75", provenance="provider_observed")
    assert status == 200 and settled["result"]["state"] == "settled"

    # Subsequent expiry command refuses due to state mismatch
    status, denied = _expire(service, workspace_id, res_id, call_id, attempt_id, fence, "potentially_sent")
    assert status == 400
    assert denied["error"]["code"] == "invalid_request"
    assert "reservation_state_mismatch" in denied["error"]["diagnostics"]

    scope, reservation = _inspect_accounting(service, scope_id, res_id)
    assert scope["held"].get("included_credit", "0") == "0"
    assert scope["unresolved"].get("included_credit", "0") == "0"
    assert scope["spent"].get("included_credit") == "0.75"
    assert reservation["state"] == "settled"
