"""Disposable PostgreSQL contract tests for stage-budget binding."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
import pytest

from omp_work.v1.canonical import sha256
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.server import create_app
from test_provider_account_authority import _put_account, _setup_operator
from test_rate_card_authority import _register_card
from test_workflow_service import _command, _grant, _execution_grant_audited_attempt, _owner_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _setup_binding(
    service,
    *,
    provider: str = "kimi-code",
    model: str = "k3",
    effort: str = "high",
    concurrency_limit: int = 5,
    scope_limit: str = "10.00",
):
    workspace_id = uuid4()
    _grant(service, workspace_id)
    account_id = uuid4()
    scope_id = uuid4()

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
        conn.execute("GRANT SELECT, REFERENCES ON omp_work.provider_accounts TO omp_work_app")
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(uuid4())),
        )
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                balance_provenance, concurrency_limit
            ) VALUES (
                %s, %s, %s, %s,
                'fixture-evidence', clock_timestamp(), 'subscription',
                'provider_observed', %s
            )
            """,
            (
                account_id,
                workspace_id,
                provider,
                f"{provider}-account-{secrets.token_hex(4)}",
                concurrency_limit,
            ),
        )

    status, result = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(scope_id),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": scope_limit},
            },
        },
    )
    assert status == 200, result

    grant_id, work_id, revision_id, candidate_id, attempt_id, _push_id, _judge, item = (
        _execution_grant_audited_attempt(service, workspace_id, "stage-budget-binding")
    )

    return {
        "workspace_id": workspace_id,
        "account_id": account_id,
        "scope_id": scope_id,
        "grant_id": grant_id,
        "work_id": work_id,
        "revision_id": revision_id,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "provider": provider,
        "model": model,
        "effort": effort,
    }


def _reserve_launch(
    ctx, service, *, provider=None, model=None, effort=None, tool_call_id=None, task_sha=None, role="audit"
):
    prov = provider or ctx["provider"]
    mod = model or ctx["model"]
    eff = effort or ctx["effort"]
    t_sha = task_sha or ("b" * 64)
    tc_id = tool_call_id or f"launch-{uuid4()}"
    payload = {
        "work_id": ctx["work_id"],
        "revision_id": ctx["revision_id"],
        "candidate_id": ctx["candidate_id"],
        "attempt_id": ctx["attempt_id"],
        "grant_id": ctx["grant_id"],
        "role": role,
        "request_sha256": secrets.token_hex(32),
        "tool_call_id": tc_id,
        "task_sha256": t_sha,
        "prepared_context_sha256": "c" * 64,
        "requested_selector": f"{prov}/{mod}:{eff}",
        "requested_provider": prov,
        "requested_model": mod,
        "requested_api": "openai-completions",
        "requested_effort": eff,
        "requested_wire_model": mod,
    }
    status, body = _command(
        service, ctx["workspace_id"], {"type": "reserve_stage_launch", "payload": payload}
    )
    assert status == 200 and body["result"]["status"] == "applied", body
    return UUID(body["result"]["launch"]["launch_id"]), t_sha


def _reserve_budget_bound(
    ctx,
    service,
    *,
    launch_id=None,
    amount="1.00",
    logical_call_id=None,
    transport_attempt_id=None,
    provider=None,
    model=None,
    effort=None,
    account_id=None,
    scope_id=None,
    expires_at="2030-01-01T00:00:00+00:00",
    operation_id=None,
    workspace_id=None,
):
    ws_id = workspace_id or ctx["workspace_id"]
    payload = {
        "scope_id": str(scope_id or ctx["scope_id"]),
        "account_id": str(account_id or ctx["account_id"]),
        "logical_call_id": str(logical_call_id or uuid4()),
        "transport_attempt_id": str(transport_attempt_id or uuid4()),
        "provider": provider or ctx["provider"],
        "model": model or ctx["model"],
        "effort": effort or ctx["effort"],
        "resource": "included_credit",
        "worst_case_drawdown": amount,
        "context_limit": 1000,
        "output_limit": 100,
        "expires_at": expires_at,
    }
    if launch_id is not None:
        payload["launch_id"] = str(launch_id)
    return _command(
        service,
        ws_id,
        {"type": "reserve_budget", "payload": payload},
        operation_id=operation_id,
    )


def _inspect_reservation(service, reservation_id):
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    ) as conn:
        return conn.execute(
            "SELECT * FROM omp_work.budget_reservations WHERE reservation_id = %s",
            (reservation_id,),
        ).fetchone()


def _inspect_scope(service, scope_id):
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    ) as conn:
        return conn.execute(
            "SELECT held, spent, unresolved FROM omp_work.budget_scopes WHERE scope_id = %s",
            (scope_id,),
        ).fetchone()


def _inspect_launch(service, launch_id):
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    ) as conn:
        return conn.execute(
            "SELECT * FROM omp_work.stage_launches WHERE launch_id = %s",
            (launch_id,),
        ).fetchone()


def test_bound_handoff_claims_reservation_atomically_and_replays(service):
    ctx = _setup_binding(service)
    launch_id, task_sha = _reserve_launch(ctx, service)

    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        amount="1.50",
        logical_call_id=call_id,
        transport_attempt_id=attempt_id,
    )
    assert status == 200, res
    res_id = UUID(res["result"]["reservation_id"])

    # Pre-handoff: reservation is reserved_unsent
    row_before = _inspect_reservation(service, res_id)
    assert row_before["state"] == "reserved_unsent"
    assert row_before["claimed_at"] is None
    assert row_before["launch_id"] == launch_id

    # Atomic handoff claims the bound reservation
    handoff_op_id = uuid4()
    status, handoff = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
        operation_id=handoff_op_id,
    )
    assert status == 200 and handoff["result"]["status"] == "applied", handoff
    assert handoff["result"]["launch"]["status"] == "handed_off"

    row_after = _inspect_reservation(service, res_id)
    assert row_after["state"] == "potentially_sent"
    assert row_after["claimed_at"] is not None

    scope = _inspect_scope(service, ctx["scope_id"])
    assert scope["held"].get("included_credit") == "1.50"

    # Same-operation replay returns stored receipt
    status, replay_same = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
        operation_id=handoff_op_id,
    )
    assert status == 200 and replay_same["receipt"]["state"] == "replayed", replay_same
    assert replay_same["result"]["launch"]["status"] == "handed_off"

    # Fresh-operation replay returns replayed without duplicate claim
    status, replay_fresh = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
        operation_id=uuid4(),
    )
    assert status == 200 and replay_fresh["result"]["status"] == "replayed", replay_fresh

    # Lost-response replay through a fresh TestClient
    fresh_client = TestClient(create_app(service.config, capabilities_dir=service.capabilities))
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(ctx["workspace_id"]),
        "operation_id": str(handoff_op_id),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": {
            "type": "handoff_stage_launch",
            "payload": {"launch_id": str(launch_id), "task_sha256": task_sha},
        },
    }
    response = fresh_client.post(
        "/v1/commands",
        headers=_owner_headers(ctx["workspace_id"]) | {"Authorization": "Bearer owner-token"},
        json=envelope,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["receipt"]["state"] == "replayed"
    assert body["result"]["launch"]["status"] == "handed_off"


def test_handoff_without_reservation_refuses_only_when_account_exists(service):
    ctx = _setup_binding(service, provider="kimi-code")
    launch_id, task_sha = _reserve_launch(ctx, service, provider="kimi-code")

    # Provider account exists for kimi-code, but no bound reservation -> budget_exhausted
    status, refused = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert status == 409 and refused["error"]["code"] == "budget_exhausted", refused
    assert "stage handoff requires an active budget reservation" in refused["error"]["diagnostics"]

    # Verify launch remains reserved
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), row_factory=dict_row, autocommit=True
    ) as conn:
        launch = conn.execute(
            "SELECT status FROM omp_work.stage_launches WHERE launch_id = %s", (launch_id,)
        ).fetchone()
        assert launch["status"] == "reserved"

    # Unrelated provider without any provider_accounts row retains historical behavior
    ctx_unrelated = _setup_binding(service, provider="other-prov")
    unrelated_launch_id, unrelated_task_sha = _reserve_launch(
        ctx_unrelated, service, provider="unrelated-legacy-provider", model="legacy-model", effort="low"
    )
    status, allowed = _command(
        service,
        ctx_unrelated["workspace_id"],
        {
            "type": "handoff_stage_launch",
            "payload": {"launch_id": str(unrelated_launch_id), "task_sha256": unrelated_task_sha},
        },
    )
    assert status == 200 and allowed["result"]["status"] == "applied", allowed
    assert allowed["result"]["launch"]["status"] == "handed_off"


def test_bind_refuses_stale_or_mismatched_launch_without_mutation(service):
    ctx = _setup_binding(service, provider="kimi-code", model="k3", effort="high")
    launch_id, task_sha = _reserve_launch(ctx, service)

    # 1. Cross-provider account mismatch
    openai_account_id = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                balance_provenance, concurrency_limit
            ) VALUES (%s, %s, 'openai', 'openai-acct', 'fixture-evidence', clock_timestamp(), 'subscription', 'provider_observed', 2)
            """,
            (openai_account_id, ctx["workspace_id"]),
        )

    # Provider on payload ("openai") differs from launch provider ("kimi-code") -> 409 stale_evidence
    status, refused_route = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        account_id=openai_account_id,
        provider="openai",
    )
    assert status == 409 and refused_route["error"]["code"] == "stale_evidence", refused_route

    # Provider on payload matches launch ("kimi-code") but account is "openai" -> 400 invalid_request
    status, refused_account = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        account_id=openai_account_id,
        provider="kimi-code",
    )
    assert status == 400 and refused_account["error"]["code"] == "invalid_request", refused_account
    assert "account_provider_mismatch" in refused_account["error"]["diagnostics"]

    # 2. Wrong model -> 409 stale_evidence
    status, refused_model = _reserve_budget_bound(
        ctx, service, launch_id=launch_id, model="wrong-model"
    )
    assert status == 409 and refused_model["error"]["code"] == "stale_evidence", refused_model

    # 3. Wrong effort -> 409 stale_evidence
    status, refused_effort = _reserve_budget_bound(
        ctx, service, launch_id=launch_id, effort="low"
    )
    assert status == 409 and refused_effort["error"]["code"] == "stale_evidence", refused_effort

    # 4. Wrong workspace -> 409 stale_evidence
    other_ws = uuid4()
    _grant(service, other_ws)
    status, refused_ws = _reserve_budget_bound(
        ctx, service, launch_id=launch_id, workspace_id=other_ws
    )
    assert status == 409 and refused_ws["error"]["code"] == "stale_evidence", refused_ws

    # 5. Handed-off launch cannot accept a reservation
    status_bind, res1 = _reserve_budget_bound(ctx, service, launch_id=launch_id)
    assert status_bind == 200, res1
    status_handoff, _ = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert status_handoff == 200

    status, refused_handed_off = _reserve_budget_bound(
        ctx, service, launch_id=launch_id
    )
    assert status == 409 and refused_handed_off["error"]["code"] == "stale_evidence", refused_handed_off

    # 6. Second active binding on a fresh reserved launch -> 400 invalid_request
    ctx2 = _setup_binding(service, provider="kimi-code", model="k3", effort="high")
    launch2_id, _ = _reserve_launch(ctx2, service)
    status, res2_first = _reserve_budget_bound(ctx2, service, launch_id=launch2_id)
    assert status == 200, res2_first

    status, res2_second = _reserve_budget_bound(ctx2, service, launch_id=launch2_id)
    assert status == 400 and res2_second["error"]["code"] == "invalid_request", res2_second
    assert "launch_reservation_active" in res2_second["error"]["diagnostics"]


def test_unbound_logical_call_does_not_satisfy_handoff(service):
    ctx = _setup_binding(service)
    launch_id, task_sha = _reserve_launch(ctx, service)

    # Reserve budget WITHOUT launch_id, but with logical_call_id == launch_id
    status, unbound_res = _reserve_budget_bound(
        ctx, service, launch_id=None, logical_call_id=launch_id
    )
    assert status == 200, unbound_res

    # Stage handoff must refuse because no active bound reservation exists
    status, refused = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert status == 409 and refused["error"]["code"] == "budget_exhausted", refused
    assert "stage handoff requires an active budget reservation" in refused["error"]["diagnostics"]


def test_cancel_before_handoff_releases_every_ancestor_exactly_once(service):
    ctx = _setup_binding(service)
    # Create parent scope and child scope
    parent_scope_id = uuid4()
    child_scope_id = uuid4()
    status, parent = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(parent_scope_id),
                "kind": "tournament",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "10.00"},
            },
        },
    )
    assert status == 200, parent
    status, child = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(child_scope_id),
                "parent_scope_id": str(parent_scope_id),
                "kind": "work",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "5.00"},
            },
        },
    )
    assert status == 200, child

    launch_id, _ = _reserve_launch(ctx, service)
    status, res = _reserve_budget_bound(
        ctx, service, launch_id=launch_id, scope_id=child_scope_id, amount="2.50"
    )
    assert status == 200, res
    res_id = UUID(res["result"]["reservation_id"])
    fence = res["result"]["fence"]

    # Both parent and child hold 2.50
    parent_scope = _inspect_scope(service, parent_scope_id)
    child_scope = _inspect_scope(service, child_scope_id)
    assert parent_scope["held"].get("included_credit") == "2.50"
    assert child_scope["held"].get("included_credit") == "2.50"

    # Cancel stage launch releases bound unsent reservation across all ancestors
    status, cancelled = _command(
        service,
        ctx["workspace_id"],
        {"type": "cancel_stage_launch", "payload": {"launch_id": str(launch_id), "reason": "worker canceled"}},
    )
    assert status == 200 and cancelled["result"]["status"] == "applied", cancelled
    assert cancelled["result"]["launch"]["status"] == "cancelled"

    # Reservation state is cancelled_unsent
    row = _inspect_reservation(service, res_id)
    assert row["state"] == "cancelled_unsent"

    # Held is released on both parent and child
    parent_scope_after = _inspect_scope(service, parent_scope_id)
    child_scope_after = _inspect_scope(service, child_scope_id)
    assert parent_scope_after["held"].get("included_credit", "0") == "0"
    assert child_scope_after["held"].get("included_credit", "0") == "0"

    # Replay cancel does not double release
    status, replay_cancel = _command(
        service,
        ctx["workspace_id"],
        {"type": "cancel_stage_launch", "payload": {"launch_id": str(launch_id), "reason": "worker canceled"}},
    )
    assert status == 200 and replay_cancel["result"]["status"] == "replayed", replay_cancel
    parent_scope_replay = _inspect_scope(service, parent_scope_id)
    assert parent_scope_replay["held"].get("included_credit", "0") == "0"

    # Direct cancel_budget and claim_budget refuse bound row
    status, direct_cancel = _command(
        service,
        ctx["workspace_id"],
        {"type": "cancel_budget", "payload": {"reservation_id": str(res_id), "fence": fence, "verified_unsent": True}},
    )
    assert status == 400 and direct_cancel["error"]["code"] == "invalid_request"

    status, direct_claim = _command(
        service,
        ctx["workspace_id"],
        {"type": "claim_budget", "payload": {"reservation_id": str(res_id), "fence": fence}},
    )
    assert status == 400 and direct_claim["error"]["code"] == "invalid_request"


def test_direct_claim_and_cancel_side_doors_refuse_bound_reservation(service):
    ctx = _setup_binding(service)
    launch_id, _ = _reserve_launch(ctx, service)
    status, res = _reserve_budget_bound(ctx, service, launch_id=launch_id, amount="1.00")
    assert status == 200, res
    res_id = UUID(res["result"]["reservation_id"])
    fence = res["result"]["fence"]

    # Direct claim refuses bound reservation
    status, refused_claim = _command(
        service,
        ctx["workspace_id"],
        {"type": "claim_budget", "payload": {"reservation_id": str(res_id), "fence": fence}},
    )
    assert status == 400 and refused_claim["error"]["code"] == "invalid_request", refused_claim
    assert "bound_reservation_uses_stage_commands" in refused_claim["error"]["diagnostics"]

    # Direct cancel refuses bound reservation
    status, refused_cancel = _command(
        service,
        ctx["workspace_id"],
        {"type": "cancel_budget", "payload": {"reservation_id": str(res_id), "fence": fence, "verified_unsent": True}},
    )
    assert status == 400 and refused_cancel["error"]["code"] == "invalid_request", refused_cancel
    assert "bound_reservation_uses_stage_commands" in refused_cancel["error"]["diagnostics"]

    # Reservation state is unchanged
    row = _inspect_reservation(service, res_id)
    assert row["state"] == "reserved_unsent"


def test_interruption_after_handoff_preserves_worst_case_and_reconciles(service):
    ctx = _setup_binding(service)
    launch_id, task_sha = _reserve_launch(ctx, service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        amount="3.00",
        logical_call_id=call_id,
        transport_attempt_id=attempt_id,
    )
    assert status == 200, res
    res_id = UUID(res["result"]["reservation_id"])
    fence = res["result"]["fence"]

    # Handoff launch -> reservation becomes potentially_sent
    status, handoff = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert status == 200, handoff

    # Reconcile stage launch to interrupted
    status, reconcile = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "reconcile_stage_launch",
            "payload": {"launch_id": str(launch_id), "reason": "host disconnected"},
        },
    )
    assert status == 200 and reconcile["result"]["launch"]["status"] == "interrupted", reconcile

    # Held accounting remains preserved
    scope = _inspect_scope(service, ctx["scope_id"])
    assert scope["held"].get("included_credit") == "3.00"
    row = _inspect_reservation(service, res_id)
    assert row["state"] == "potentially_sent"

    # Backdate reservation and expire it -> state moves to unresolved
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '10 seconds' WHERE reservation_id = %s",
            (res_id,),
        )

    status, expired = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "expire_budget",
            "payload": {
                "reservation_id": str(res_id),
                "logical_call_id": str(call_id),
                "transport_attempt_id": str(attempt_id),
                "fence": fence,
                "expected_state": "potentially_sent",
            },
        },
    )
    assert status == 200 and expired["result"]["state"] == "unresolved", expired

    scope_unresolved = _inspect_scope(service, ctx["scope_id"])
    assert scope_unresolved["held"].get("included_credit", "0") == "0"
    assert scope_unresolved["unresolved"].get("included_credit") == "3.00"

    # Provider-observed settlement arrives later and reconciles actual drawdown
    status, settled = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "settle_budget",
            "payload": {
                "reservation_id": str(res_id),
                "transport_attempt_id": str(attempt_id),
                "fence": fence,
                "state": "settled",
                "actual_drawdown": "0.85",
                "provenance": "provider_observed",
                "outcome": "success",
            },
        },
    )
    assert status == 200 and settled["result"]["state"] == "settled", settled

    scope_settled = _inspect_scope(service, ctx["scope_id"])
    assert scope_settled["unresolved"].get("included_credit", "0") == "0"
    assert scope_settled["spent"].get("included_credit") == "0.85"


def test_expired_bound_reservation_refuses_handoff_and_does_not_double_release(service):
    ctx = _setup_binding(service)
    launch_id, task_sha = _reserve_launch(ctx, service)
    call_id, attempt_id = uuid4(), uuid4()
    status, res = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        amount="1.20",
        logical_call_id=call_id,
        transport_attempt_id=attempt_id,
    )
    assert status == 200, res
    res_id = UUID(res["result"]["reservation_id"])
    fence = res["result"]["fence"]

    # Backdate expires_at
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '5 seconds' WHERE reservation_id = %s",
            (res_id,),
        )

    # Handoff refuses because bound reservation is expired
    status, refused = _command(
        service,
        ctx["workspace_id"],
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert status == 400 and refused["error"]["code"] == "invalid_request", refused
    assert "reservation_expired" in refused["error"]["diagnostics"]

    # Launch remains reserved; reservation remains reserved_unsent; held remains 1.20
    launch = _inspect_launch(service, launch_id)
    assert launch["status"] == "reserved"
    row = _inspect_reservation(service, res_id)
    assert row["state"] == "reserved_unsent"
    scope = _inspect_scope(service, ctx["scope_id"])
    assert scope["held"].get("included_credit") == "1.20"

    # Expire the reservation -> state becomes cancelled_unsent, held becomes 0
    status, expired = _command(
        service,
        ctx["workspace_id"],
        {
            "type": "expire_budget",
            "payload": {
                "reservation_id": str(res_id),
                "logical_call_id": str(call_id),
                "transport_attempt_id": str(attempt_id),
                "fence": fence,
                "expected_state": "reserved_unsent",
            },
        },
    )
    assert status == 200 and expired["result"]["state"] == "cancelled_unsent", expired
    scope_after_expire = _inspect_scope(service, ctx["scope_id"])
    assert scope_after_expire["held"].get("included_credit", "0") == "0"

    # Cancel stage launch afterwards -> launch cancelled, no double-release
    status, cancelled = _command(
        service,
        ctx["workspace_id"],
        {"type": "cancel_stage_launch", "payload": {"launch_id": str(launch_id), "reason": "expired cancel"}},
    )
    assert status == 200 and cancelled["result"]["status"] == "applied", cancelled
    assert cancelled["result"]["launch"]["status"] == "cancelled"

    scope_after_cancel = _inspect_scope(service, ctx["scope_id"])
    assert scope_after_cancel["held"].get("included_credit", "0") == "0"


def test_concurrent_handoff_and_expire_have_one_winner(service):
    # (a) Backdated reservation: handoff vs expire -> expire wins
    ctx_a = _setup_binding(service)
    launch_a, task_sha_a = _reserve_launch(ctx_a, service)
    call_a, attempt_a = uuid4(), uuid4()
    status, res_a = _reserve_budget_bound(
        ctx_a,
        service,
        launch_id=launch_a,
        amount="1.00",
        logical_call_id=call_a,
        transport_attempt_id=attempt_a,
    )
    assert status == 200, res_a
    res_a_id = UUID(res_a["result"]["reservation_id"])
    fence_a = res_a["result"]["fence"]

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "UPDATE omp_work.budget_reservations SET expires_at = clock_timestamp() - interval '2 seconds' WHERE reservation_id = %s",
            (res_a_id,),
        )

    barrier_a = threading.Barrier(2)
    results_a = {}
    errors_a = []

    def worker_handoff_a():
        try:
            barrier_a.wait()
            results_a["handoff"] = _command(
                service,
                ctx_a["workspace_id"],
                {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_a), "task_sha256": task_sha_a}},
            )
        except Exception as e:
            errors_a.append(("handoff", e))

    def worker_expire_a():
        try:
            barrier_a.wait()
            results_a["expire"] = _command(
                service,
                ctx_a["workspace_id"],
                {
                    "type": "expire_budget",
                    "payload": {
                        "reservation_id": str(res_a_id),
                        "logical_call_id": str(call_a),
                        "transport_attempt_id": str(attempt_a),
                        "fence": fence_a,
                        "expected_state": "reserved_unsent",
                    },
                },
            )
        except Exception as e:
            errors_a.append(("expire", e))

    t1 = threading.Thread(target=worker_handoff_a)
    t2 = threading.Thread(target=worker_expire_a)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors_a, f"Thread errors occurred: {errors_a}"
    assert "handoff" in results_a and "expire" in results_a
    assert results_a["expire"][0] == 200
    assert results_a["handoff"][0] in {400, 409}

    row_a = _inspect_reservation(service, res_a_id)
    assert row_a["state"] == "cancelled_unsent"
    launch_a_row = _inspect_launch(service, launch_a)
    assert launch_a_row["status"] == "reserved"
    scope_a = _inspect_scope(service, ctx_a["scope_id"])
    assert scope_a["held"].get("included_credit", "0") == "0"

    # (b) Far-future reservation: handoff vs expire -> handoff wins
    ctx_b = _setup_binding(service)
    launch_b, task_sha_b = _reserve_launch(ctx_b, service)
    call_b, attempt_b = uuid4(), uuid4()
    status, res_b = _reserve_budget_bound(
        ctx_b,
        service,
        launch_id=launch_b,
        amount="1.00",
        logical_call_id=call_b,
        transport_attempt_id=attempt_b,
        expires_at="2035-01-01T00:00:00+00:00",
    )
    assert status == 200, res_b
    res_b_id = UUID(res_b["result"]["reservation_id"])
    fence_b = res_b["result"]["fence"]

    barrier_b = threading.Barrier(2)
    results_b = {}
    errors_b = []

    def worker_handoff_b():
        try:
            barrier_b.wait()
            results_b["handoff"] = _command(
                service,
                ctx_b["workspace_id"],
                {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_b), "task_sha256": task_sha_b}},
            )
        except Exception as e:
            errors_b.append(("handoff", e))

    def worker_expire_b():
        try:
            barrier_b.wait()
            results_b["expire"] = _command(
                service,
                ctx_b["workspace_id"],
                {
                    "type": "expire_budget",
                    "payload": {
                        "reservation_id": str(res_b_id),
                        "logical_call_id": str(call_b),
                        "transport_attempt_id": str(attempt_b),
                        "fence": fence_b,
                        "expected_state": "reserved_unsent",
                    },
                },
            )
        except Exception as e:
            errors_b.append(("expire", e))

    t3 = threading.Thread(target=worker_handoff_b)
    t4 = threading.Thread(target=worker_expire_b)
    t3.start()
    t4.start()
    t3.join()
    t4.join()

    assert not errors_b, f"Thread errors occurred: {errors_b}"
    assert "handoff" in results_b and "expire" in results_b
    assert results_b["handoff"][0] == 200
    assert results_b["expire"][0] == 400

    row_b = _inspect_reservation(service, res_b_id)
    assert row_b["state"] == "potentially_sent"
    launch_b_row = _inspect_launch(service, launch_b)
    assert launch_b_row["status"] == "handed_off"
    scope_b = _inspect_scope(service, ctx_b["scope_id"])
    assert scope_b["held"].get("included_credit") == "1.00"


def test_quoted_stage_reservation_atomic_settlement(service):
    """Quoted reservation settlement: exact fixed-point sum, replay idempotency, unresolved transitions."""
    ws = uuid4()
    _grant(service, ws)
    _setup_operator(service, ws)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (ws,),
        )

    # 1. Rate card and account setup
    card_id = uuid4()
    _register_card(
        service,
        ws,
        card_id,
        provider="anthropic",
        version="2026-q3",
        billing_modes=["subscription"],
        effective_from=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        currency="USD",
        unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"},
    )
    acct_id = uuid4()
    _put_account(
        service,
        ws,
        acct_id,
        provider="anthropic",
        account_identity="settle-acct",
        rate_card_version="2026-q3",
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )

    grant_id, work_id, revision_id, candidate_id, attempt_id, _p, _j, item = (
        _execution_grant_audited_attempt(service, ws, "settle-quoted-test")
    )
    scope_id = uuid4()
    _command(
        service,
        ws,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(scope_id),
                "work_id": str(work_id),
                "kind": "work",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "100.00"},
            },
        },
    )

    ctx = {
        "workspace_id": ws,
        "account_id": acct_id,
        "scope_id": scope_id,
        "grant_id": grant_id,
        "work_id": work_id,
        "revision_id": revision_id,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "provider": "anthropic",
        "model": "claude-3-7-sonnet",
        "effort": "high",
    }

    # Case A: Exact fixed-point settlement on qualified usage
    launch_id, task_sha = _reserve_launch(ctx, service, role="audit")
    st_q, res_q = _command(
        service,
        ws,
        {
            "type": "quote_budget",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "candidate_id": str(candidate_id),
                "attempt_id": str(attempt_id),
                "grant_id": str(grant_id),
                "role": "audit",
                "launch_id": str(launch_id),
                "account_id": str(acct_id),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "currency": "USD",
                "usage_ceiling": {"input": 100000, "output": 4000, "cacheRead": 100},
            },
        },
    )
    assert st_q == 200, res_q
    quote = res_q["result"]["quote"]
    # 100000 * 3e-6 = 0.30
    # 4000 * 1.5e-5 = 0.06
    # 100 * 3e-7 = 0.00003
    # worst_case = 0.36003
    worst_case = quote["worst_case_amount"]
    assert worst_case == "0.3600300"
    quote_id = UUID(quote["quote_id"])

    st_res, res_res = _command(
        service,
        ws,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(acct_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(uuid4()),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "resource": "included_credit",
                "worst_case_drawdown": worst_case,
                "context_limit": 100000,
                "output_limit": 4000,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "launch_id": str(launch_id),
                "quote_id": str(quote_id),
            },
        },
    )
    assert st_res == 200, res_res
    res_id = UUID(res_res["result"]["reservation_id"])

    # Handoff
    st_h, res_h = _command(
        service,
        ws,
        {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id), "task_sha256": task_sha}},
    )
    assert st_h == 200, res_h

    # Measured usage: input: 10000, output: 500, cacheRead: 50, cacheWrite: 0, totalTokens: 10550
    # Expected actual: 10000*3e-6 + 500*1.5e-5 + 50*3e-7 = 0.03 + 0.0075 + 0.000015 = 0.037515
    outcome_a = {
        "started": True,
        "usage": {"input": 10000, "output": 500, "cacheRead": 50, "cacheWrite": 0, "totalTokens": 10550},
        "payload": "{\"verdict\":\"PASS\"}",
    }
    settle_payload_a = {
        "launch_id": str(launch_id),
        "outcome_sha256": sha256(outcome_a),
        "outcome": outcome_a,
        "served_selector": "anthropic/claude-3-7-sonnet:high",
        "served_model": "claude-3-7-sonnet",
    }
    st_settle, res_settle = _command(
        service,
        ws,
        {"type": "settle_stage_launch", "payload": settle_payload_a},
    )
    assert st_settle == 200 and res_settle["result"]["status"] == "applied", res_settle

    # Reservation row check
    res_row = _inspect_reservation(service, res_id)
    assert res_row["state"] == "settled"
    assert Decimal(str(res_row["actual_drawdown"])) == Decimal("0.037515")
    assert res_row["provenance"] == "provider_observed"
    assert res_row["outcome"] == "success"

    # Scope check: held released (0), spent = 0.037515, unresolved = 0
    sc = _inspect_scope(service, scope_id)
    assert sc["held"].get("included_credit", "0") == "0"
    assert Decimal(sc["spent"]["included_credit"]) == Decimal("0.037515")
    assert sc["unresolved"].get("included_credit", "0") == "0"

    # Settle replay: receipt is replayed, counters untouched
    st_rep, res_rep = _command(
        service,
        ws,
        {"type": "settle_stage_launch", "payload": settle_payload_a},
    )
    assert st_rep == 200 and res_rep["result"]["status"] == "replayed", res_rep
    sc_rep = _inspect_scope(service, scope_id)
    assert Decimal(sc_rep["spent"]["included_credit"]) == Decimal("0.037515")

    # Case B: Missing/malformed usage transitions to unresolved worst case
    launch_id_b, task_sha_b = _reserve_launch(ctx, service, role="audit")
    st_qb, res_qb = _command(
        service,
        ws,
        {
            "type": "quote_budget",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "candidate_id": str(candidate_id),
                "attempt_id": str(attempt_id),
                "grant_id": str(grant_id),
                "role": "audit",
                "launch_id": str(launch_id_b),
                "account_id": str(acct_id),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "currency": "USD",
                "usage_ceiling": {"input": 10000, "output": 1000, "cacheRead": 10},
            },
        },
    )
    assert st_qb == 200, res_qb
    quote_b = res_qb["result"]["quote"]
    worst_b = quote_b["worst_case_amount"]
    quote_id_b = UUID(quote_b["quote_id"])

    st_rb, res_rb = _command(
        service,
        ws,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(acct_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(uuid4()),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "resource": "included_credit",
                "worst_case_drawdown": worst_b,
                "context_limit": 10000,
                "output_limit": 1000,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "launch_id": str(launch_id_b),
                "quote_id": str(quote_id_b),
            },
        },
    )
    assert st_rb == 200, res_rb
    res_b_id = UUID(res_rb["result"]["reservation_id"])

    _command(service, ws, {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id_b), "task_sha256": task_sha_b}})

    # Missing usage in outcome
    outcome_b = {"started": True}
    settle_b = {
        "launch_id": str(launch_id_b),
        "outcome_sha256": sha256(outcome_b),
        "outcome": outcome_b,
        "served_selector": "anthropic/claude-3-7-sonnet:high",
        "served_model": "claude-3-7-sonnet",
    }
    st_sb, res_sb = _command(service, ws, {"type": "settle_stage_launch", "payload": settle_b})
    assert st_sb == 200 and res_sb["result"]["status"] == "applied", res_sb

    res_b_row = _inspect_reservation(service, res_b_id)
    assert res_b_row["state"] == "unresolved"
    assert str(res_b_row["actual_drawdown"]) == worst_b
    assert res_b_row["provenance"] == "unknown"

    sc_b = _inspect_scope(service, scope_id)
    assert sc_b["unresolved"]["included_credit"] == worst_b

    # Case C: Positive nested usage transitions to unresolved worst case
    launch_id_c, task_sha_c = _reserve_launch(ctx, service, role="audit")
    st_qc, res_qc = _command(
        service,
        ws,
        {
            "type": "quote_budget",
            "payload": {
                "work_id": str(work_id),
                "revision_id": str(revision_id),
                "candidate_id": str(candidate_id),
                "attempt_id": str(attempt_id),
                "grant_id": str(grant_id),
                "role": "audit",
                "launch_id": str(launch_id_c),
                "account_id": str(acct_id),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "currency": "USD",
                "usage_ceiling": {"input": 10000, "output": 1000, "cacheRead": 10},
            },
        },
    )
    assert st_qc == 200, res_qc
    quote_c = res_qc["result"]["quote"]
    worst_c = quote_c["worst_case_amount"]
    quote_id_c = UUID(quote_c["quote_id"])

    st_rc, res_rc = _command(
        service,
        ws,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(acct_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(uuid4()),
                "provider": "anthropic",
                "model": "claude-3-7-sonnet",
                "effort": "high",
                "resource": "included_credit",
                "worst_case_drawdown": worst_c,
                "context_limit": 10000,
                "output_limit": 1000,
                "expires_at": "2030-01-01T00:00:00+00:00",
                "launch_id": str(launch_id_c),
                "quote_id": str(quote_id_c),
            },
        },
    )
    assert st_rc == 200, res_rc
    res_c_id = UUID(res_rc["result"]["reservation_id"])

    _command(service, ws, {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id_c), "task_sha256": task_sha_c}})

    # Nested orchestration usage > 0 forces unresolved
    outcome_c = {
        "started": True,
        "usage": {
            "input": 1000,
            "output": 100,
            "cacheRead": 5,
            "cacheWrite": 0,
            "totalTokens": 1105,
            "orchestration": {"input": 10, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 10},
        },
    }
    settle_c = {
        "launch_id": str(launch_id_c),
        "outcome_sha256": sha256(outcome_c),
        "outcome": outcome_c,
        "served_selector": "anthropic/claude-3-7-sonnet:high",
        "served_model": "claude-3-7-sonnet",
    }
    st_sc, res_sc = _command(service, ws, {"type": "settle_stage_launch", "payload": settle_c})
    assert st_sc == 200 and res_sc["result"]["status"] == "applied", res_sc

    res_c_row = _inspect_reservation(service, res_c_id)
    assert res_c_row["state"] == "unresolved"
    assert str(res_c_row["actual_drawdown"]) == worst_c

    # Case D: Quote-less legacy reservation is untouched by stage settlement
    launch_id_d, task_sha_d = _reserve_launch(ctx, service, role="audit")
    st_rd, res_rd = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id_d,
        amount="1.00",
    )
    assert st_rd == 200, res_rd
    res_d_id = UUID(res_rd["result"]["reservation_id"])
    _command(service, ws, {"type": "handoff_stage_launch", "payload": {"launch_id": str(launch_id_d), "task_sha256": task_sha_d}})

    outcome_d = {"started": True}
    settle_d = {
        "launch_id": str(launch_id_d),
        "outcome_sha256": sha256(outcome_d),
        "outcome": outcome_d,
        "served_selector": "anthropic/claude-3-7-sonnet:high",
        "served_model": "claude-3-7-sonnet",
    }
    st_sd, res_sd = _command(service, ws, {"type": "settle_stage_launch", "payload": settle_d})
    assert st_sd == 200 and res_sd["result"]["status"] == "applied", res_sd

    # Unquoted reservation remains potentially_sent (legacy behavior)
    res_d_row = _inspect_reservation(service, res_d_id)
    assert res_d_row["state"] == "potentially_sent"
    assert res_d_row["quote_id"] is None
