"""Disposable PostgreSQL contract tests for PR3 budget authority."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest

from omp_work import contract_sha256
from omp_work.v1.models import CommandEnvelope
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
