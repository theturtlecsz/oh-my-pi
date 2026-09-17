"""Disposable PostgreSQL contract tests for PR3 budget authority."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
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


def _reserve(service, workspace_id, account_id, scope_id, *, operation_id=None, amount="1.00", logical_call_id=None, transport_attempt_id=None):
    attempt = transport_attempt_id or uuid4()
    return _command(service, workspace_id, {"type": "reserve_budget", "payload": {"scope_id": str(scope_id), "account_id": str(account_id), "logical_call_id": str(logical_call_id or uuid4()), "transport_attempt_id": str(attempt), "provider": "fixture", "model": "cheap", "effort": "low", "resource": "included_credit", "worst_case_drawdown": amount, "context_limit": 1000, "output_limit": 100, "expires_at": "2030-01-01T00:00:00+00:00"}}, operation_id=operation_id)


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
