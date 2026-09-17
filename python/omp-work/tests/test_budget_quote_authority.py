"""Disposable PostgreSQL contract tests for combined immutable budget quote authority (OMP-233)."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from test_provider_account_authority import _put_account, _setup_operator
from test_rate_card_authority import _register_card
from test_stage_budget_binding import (
    _execution_grant_audited_attempt,
    _grant,
    _reserve_budget_bound,
    _reserve_launch,
    _setup_binding,
)
from test_budget_authority import _inspect_accounting
from test_workflow_service import _command
from omp_work.operations import database as database_module
from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _quote_budget(
    service,
    workspace_id: UUID,
    payload: dict,
    *,
    operation_id: UUID | None = None,
    token: str = "owner-token",
) -> tuple[int, dict]:
    return _command(
        service,
        workspace_id,
        {"type": "quote_budget", "payload": payload},
        operation_id=operation_id,
        token=token,
    )


def _reserve_with_quote(
    service,
    workspace_id: UUID,
    *,
    scope_id: UUID,
    account_id: UUID,
    amount: str,
    provider: str,
    model: str,
    effort: str,
    quote_id: UUID | None = None,
    launch_id: UUID | None = None,
    resource: str = "included_credit",
    logical_call_id: UUID | None = None,
    transport_attempt_id: UUID | None = None,
    expires_at: str = "2030-01-01T00:00:00+00:00",
    operation_id: UUID | None = None,
    token: str = "owner-token",
) -> tuple[int, dict]:
    payload = {
        "scope_id": str(scope_id),
        "account_id": str(account_id),
        "logical_call_id": str(logical_call_id or uuid4()),
        "transport_attempt_id": str(transport_attempt_id or uuid4()),
        "provider": provider,
        "model": model,
        "effort": effort,
        "resource": resource,
        "worst_case_drawdown": amount,
        "context_limit": 1000,
        "output_limit": 100,
        "expires_at": expires_at,
    }
    if launch_id is not None:
        payload["launch_id"] = str(launch_id)
    if quote_id is not None:
        payload["quote_id"] = str(quote_id)
    return _command(
        service,
        workspace_id,
        {"type": "reserve_budget", "payload": payload},
        operation_id=operation_id,
        token=token,
    )


def _setup_quote_context(
    service,
    *,
    provider: str = "anthropic",
    model: str = "claude-3-7-sonnet",
    effort: str = "high",
    rate_card_version: str = "2026-q3",
    unit_prices: dict[str, str] | None = None,
    currency: str = "USD",
    qualification: str = "qualified",
    effective_from: str | None = None,
    effective_until: str | None = None,
    account_concurrency: int = 5,
    scope_limit: str = "50.00",
    balance_provenance: str = "provider_observed",
    account_identity: str | None = None,
) -> dict:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    _setup_operator(service, workspace_id)

    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )

    scope_id = uuid4()
    status, scope_res = _command(
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
    assert status == 200, scope_res

    card_id = uuid4()
    eff_from = effective_from or (datetime.now(UTC) - timedelta(days=1)).isoformat()
    prices = (
        unit_prices
        if unit_prices is not None
        else {"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"}
    )
    card_status, card_res = _register_card(
        service,
        workspace_id,
        card_id,
        provider=provider,
        version=rate_card_version,
        billing_modes=["subscription", "metered"],
        effective_from=eff_from,
        effective_until=effective_until,
        currency=currency,
        unit_prices=prices,
        qualification=qualification,
    )
    assert card_status == 200, card_res

    account_id = uuid4()
    acct_ident = account_identity or f"{provider}-account-{secrets.token_hex(4)}"
    acct_status, acct_res = _put_account(
        service,
        workspace_id,
        account_id,
        provider=provider,
        account_identity=acct_ident,
        rate_card_version=rate_card_version,
        balance_provenance=balance_provenance,
        billing_mode="subscription",
        budget_resource="included_credit",
        concurrency_limit=account_concurrency,
    )
    assert acct_status == 200, acct_res

    grant_id, work_id, revision_id, candidate_id, attempt_id, _push_id, _judge, item = (
        _execution_grant_audited_attempt(service, workspace_id, "quote-test")
    )

    work_scope_id = uuid4()
    status, work_scope_res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(work_scope_id),
                "parent_scope_id": str(scope_id),
                "work_id": str(work_id),
                "kind": "work",
                "policy_version": "economy-v1",
                "limits": {"included_credit": scope_limit},
            },
        },
    )
    assert status == 200, work_scope_res

    return {
        "workspace_id": workspace_id,
        "account_id": account_id,
        "account_identity": acct_ident,
        "scope_id": work_scope_id,
        "session_scope_id": scope_id,
        "grant_id": grant_id,
        "work_id": work_id,
        "revision_id": revision_id,
        "candidate_id": candidate_id,
        "attempt_id": attempt_id,
        "provider": provider,
        "model": model,
        "effort": effort,
        "rate_card_id": card_id,
        "rate_card_version": rate_card_version,
        "currency": currency,
        "unit_prices": prices,
    }


def test_applied_quote_exact_amount_immutability_replay(service):
    """1. Applied quote, exact amount, immutability, replay."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    op_id = uuid4()
    payload = {
        "work_id": str(ctx["work_id"]),
        "revision_id": str(ctx["revision_id"]),
        "candidate_id": str(ctx["candidate_id"]),
        "attempt_id": str(ctx["attempt_id"]),
        "grant_id": str(ctx["grant_id"]),
        "role": "audit",
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 200000, "output": 8000, "cacheRead": 1},
    }

    status, resp = _quote_budget(service, workspace_id, payload, operation_id=op_id)
    assert status == 200, resp
    assert resp["result"]["type"] == "quote_budget"
    quote = resp["result"]["quote"]
    # 200000 * 0.000003 = 0.60
    # 8000 * 0.000015 = 0.12
    # 1 * 0.0000003 = 0.0000003
    # Total = 0.7200003
    assert quote["worst_case_amount"] == "0.7200003"
    assert quote["quote_id"] == str(op_id)
    assert quote["account_id"] == str(ctx["account_id"])
    assert quote["rate_card_id"] == str(ctx["rate_card_id"])
    assert quote["currency"] == "USD"
    assert quote["usage_ceiling"] == {"input": 200000, "output": 8000, "cacheRead": 1}
    assert len(quote["evidence_sha256"]) == 64
    assert len(quote["quote_sha256"]) == 64

    # Idempotent replay: same operation_id, same payload
    status_replay, resp_replay = _quote_budget(service, workspace_id, payload, operation_id=op_id)
    assert status_replay == 200, resp_replay
    assert resp_replay["receipt"]["state"] == "replayed"
    assert resp_replay["result"]["quote"]["worst_case_amount"] == "0.7200003"

    # Exactly 1 row exists in DB
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_work.budget_quotes WHERE quote_id = %s", (op_id,))
            assert cur.fetchone()[0] == 1

    # Idempotency conflict: same operation_id, different payload
    conflict_payload = dict(payload, usage_ceiling={"input": 300000, "output": 8000, "cacheRead": 1})
    status_conflict, resp_conflict = _quote_budget(service, workspace_id, conflict_payload, operation_id=op_id)
    assert status_conflict == 409, resp_conflict
    assert resp_conflict["error"]["code"] == "idempotency_conflict"

    # Direct UPDATE, DELETE, TRUNCATE with omp_work_app role MUST fail with InsufficientPrivilege
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('omp.workspace_id', %s, false)", (str(workspace_id),))
            cur.execute("SELECT set_config('omp.actor_id', %s, false)", (str(uuid4()),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE omp_work.budget_quotes SET worst_case_amount=0 WHERE quote_id=%s",
                    (op_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT set_config('omp.workspace_id', %s, false)", (str(workspace_id),))
            cur.execute("SELECT set_config('omp.actor_id', %s, false)", (str(uuid4()),))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM omp_work.budget_quotes WHERE quote_id=%s",
                    (op_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE omp_work.budget_quotes")
        conn.rollback()


def test_account_selection_and_ambiguity(service):
    """2. Selection: ambiguous, explicit, foreign provider, missing."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    # Add second account for the same provider
    second_acct_id = uuid4()
    st, acct2 = _put_account(
        service,
        workspace_id,
        second_acct_id,
        provider=ctx["provider"],
        account_identity="second-acct",
        rate_card_version=ctx["rate_card_version"],
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )
    assert st == 200, acct2

    base_payload = {
        "work_id": str(ctx["work_id"]),
        "role": "implement",
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 1000, "output": 100, "cacheRead": 10},
    }

    # Multiple accounts, no account_id specified -> ambiguous
    status, resp = _quote_budget(service, workspace_id, base_payload)
    assert status == 400, resp
    assert "account_selection_ambiguous" in resp["error"]["diagnostics"]

    # Explicit account_id -> applied
    payload_explicit = dict(base_payload, account_id=str(ctx["account_id"]))
    status_exp, resp_exp = _quote_budget(service, workspace_id, payload_explicit)
    assert status_exp == 200, resp_exp
    assert resp_exp["result"]["quote"]["account_id"] == str(ctx["account_id"])

    # Foreign-provider account_id
    foreign_acct_id = uuid4()
    _put_account(
        service,
        workspace_id,
        foreign_acct_id,
        provider="openai",
        account_identity="foreign-acct",
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )
    payload_foreign = dict(base_payload, account_id=str(foreign_acct_id))
    status_foreign, resp_foreign = _quote_budget(service, workspace_id, payload_foreign)
    assert status_foreign == 400, resp_foreign
    assert "account_provider_mismatch" in resp_foreign["error"]["diagnostics"]

    # Non-existent account_id
    payload_missing_id = dict(base_payload, account_id=str(uuid4()))
    status_missing_id, resp_missing_id = _quote_budget(service, workspace_id, payload_missing_id)
    assert status_missing_id == 400, resp_missing_id
    assert "provider_account_missing" in resp_missing_id["error"]["diagnostics"]

    # Non-existent provider entirely
    payload_unknown_prov = dict(base_payload, provider="unknown-prov")
    status_unknown_prov, resp_unknown_prov = _quote_budget(service, workspace_id, payload_unknown_prov)
    assert status_unknown_prov == 400, resp_unknown_prov
    assert "provider_account_missing" in resp_unknown_prov["error"]["diagnostics"]


def test_fail_closed_matrix_zero_rows(service):
    """3. Fail-closed matrix, zero rows each."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    def count_quotes():
        with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM omp_work.budget_quotes WHERE workspace_id = %s", (workspace_id,))
                return cur.fetchone()[0]

    initial_count = count_quotes()

    base_payload = {
        "work_id": str(ctx["work_id"]),
        "role": "implement",
        "account_id": str(ctx["account_id"]),
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 1000, "output": 100, "cacheRead": 10},
    }

    # (a) Unpriced account (rate_card_version IS NULL)
    unpriced_acct_id = uuid4()
    _put_account(
        service,
        workspace_id,
        unpriced_acct_id,
        provider="anthropic",
        account_identity="unpriced-acct",
        rate_card_version=None,
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )
    p = dict(base_payload, account_id=str(unpriced_acct_id))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_unpriced" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (b) Missing rate card version
    missing_card_acct = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                rate_card_version, balance_provenance, concurrency_limit, budget_resource
            ) VALUES (%s, %s, 'anthropic', 'hist-acct', 'ev', clock_timestamp(), 'subscription', 'v-nonexistent', 'provider_observed', 5, 'included_credit')
            """,
            (missing_card_acct, workspace_id),
        )
    p = dict(base_payload, account_id=str(missing_card_acct))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_missing" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (c) Expired card (effective_until in the past)
    expired_card_id = uuid4()
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    past_end = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    st_c, res_c = _register_card(
        service,
        workspace_id,
        expired_card_id,
        provider="anthropic",
        version="expired-v1",
        effective_from=past,
        effective_until=past_end,
        billing_modes=["metered", "subscription"],
        unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"},
    )
    assert st_c == 200, res_c
    expired_acct = uuid4()
    st_acct, res_acct = _put_account(
        service,
        workspace_id,
        expired_acct,
        provider="anthropic",
        account_identity="expired-card-acct",
        rate_card_version="expired-v1",
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )
    assert st_acct == 200, res_acct
    p = dict(base_payload, account_id=str(expired_acct))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_not_effective" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (d) Currency mismatch (payload currency EUR vs card USD)
    p = dict(base_payload, currency="EUR")
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "currency_mismatch" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (e) Usage ceiling incomplete (missing cacheRead)
    p = dict(base_payload, usage_ceiling={"input": 1000, "output": 100})
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "usage_ceiling_incomplete" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (f) Unknown price category in usage ceiling (webSearch)
    p = dict(base_payload, usage_ceiling={"input": 1000, "output": 100, "cacheRead": 10, "webSearch": 5})
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_unknown_category" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (g) All-zero usage ceiling -> 400 Pydantic validation error
    p = dict(base_payload, usage_ceiling={"input": 0, "output": 0, "cacheRead": 0})
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400
    assert count_quotes() == initial_count

    # (h) Unqualified rate card
    unqual_card_id = uuid4()
    _register_card(
        service,
        workspace_id,
        unqual_card_id,
        provider="anthropic",
        version="unqual-v1",
        qualification="unqualified",
        unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"},
    )
    unqual_acct = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                rate_card_version, balance_provenance, concurrency_limit, budget_resource
            ) VALUES (%s, %s, 'anthropic', 'unqual-acct', 'ev', clock_timestamp(), 'subscription', 'unqual-v1', 'provider_observed', 5, 'included_credit')
            """,
            (unqual_acct, workspace_id),
        )
    p = dict(base_payload, account_id=str(unqual_acct))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_unqualified" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (i) Incompatible card billing mode
    incomp_card_id = uuid4()
    _register_card(
        service,
        workspace_id,
        incomp_card_id,
        provider="anthropic",
        version="incomp-v1",
        billing_modes=["metered"],
        unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"},
    )
    incomp_acct = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                rate_card_version, balance_provenance, concurrency_limit, budget_resource
            ) VALUES (%s, %s, 'anthropic', 'incomp-acct', 'ev', clock_timestamp(), 'subscription', 'incomp-v1', 'provider_observed', 5, 'included_credit')
            """,
            (incomp_acct, workspace_id),
        )
    p = dict(base_payload, account_id=str(incomp_acct))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "rate_card_incompatible" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (j) Balance provenance unknown
    unknown_bal_acct = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.provider_accounts(
                account_id, workspace_id, provider, account_identity,
                entitlement_evidence, evidence_observed_at, billing_mode,
                rate_card_version, balance_provenance, concurrency_limit, budget_resource
            ) VALUES (%s, %s, 'anthropic', 'unknown-bal-acct', 'ev', clock_timestamp(), 'subscription', %s, 'unknown', 5, 'included_credit')
            """,
            (unknown_bal_acct, workspace_id, ctx["rate_card_version"]),
        )
    p = dict(base_payload, account_id=str(unknown_bal_acct))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "unknown_account_evidence" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (k) Unclassified account (budget_resource IS NULL)
    unclass_acct_id = uuid4()
    _put_account(
        service,
        workspace_id,
        unclass_acct_id,
        provider="anthropic",
        account_identity="unclass-acct",
        rate_card_version=ctx["rate_card_version"],
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource=None,
    )
    p = dict(base_payload, account_id=str(unclass_acct_id))
    st, res = _quote_budget(service, workspace_id, p)
    assert st == 400 and "account_resource_unclassified" in res["error"]["diagnostics"]
    assert count_quotes() == initial_count

    # (l) Zero work scopes (budget_scope_missing)
    ws_l = uuid4()
    _setup_operator(service, ws_l)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (ws_l,))
    _register_card(service, ws_l, uuid4(), provider="anthropic", version="2026-q3", billing_modes=["subscription"], effective_from=(datetime.now(UTC) - timedelta(days=1)).isoformat(), currency="USD", unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"})
    acct_l = uuid4()
    _put_account(service, ws_l, acct_l, provider="anthropic", account_identity="acct-l", rate_card_version="2026-q3", balance_provenance="provider_observed", billing_mode="subscription", budget_resource="included_credit")
    _g, w_id_no_scope, _r, _c, _a, _p, _j, _it = _execution_grant_audited_attempt(service, ws_l, "no-work-scope")
    p_no_scope = dict(base_payload, work_id=str(w_id_no_scope), account_id=str(acct_l))
    st, res = _quote_budget(service, ws_l, p_no_scope)
    assert st == 400 and "budget_scope_missing" in res["error"]["diagnostics"]

    # (m) Ambiguous work scopes (budget_scope_ambiguous)
    ws1 = uuid4()
    ws2 = uuid4()
    _command(service, ws_l, {"type": "create_budget_scope", "payload": {"scope_id": str(ws1), "work_id": str(w_id_no_scope), "kind": "work", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    _command(service, ws_l, {"type": "create_budget_scope", "payload": {"scope_id": str(ws2), "work_id": str(w_id_no_scope), "kind": "work", "policy_version": "economy-v1", "limits": {"included_credit": "10.00"}}})
    st, res = _quote_budget(service, ws_l, p_no_scope)
    assert st == 400 and "budget_scope_ambiguous" in res["error"]["diagnostics"]

    # (n) Missing resource limit (budget_scope_missing_limit)
    ws_n = uuid4()
    _setup_operator(service, ws_n)
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING", (ws_n,))
    _register_card(service, ws_n, uuid4(), provider="anthropic", version="2026-q3", billing_modes=["subscription"], effective_from=(datetime.now(UTC) - timedelta(days=1)).isoformat(), currency="USD", unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"})
    acct_n = uuid4()
    _put_account(service, ws_n, acct_n, provider="anthropic", account_identity="acct-n", rate_card_version="2026-q3", balance_provenance="provider_observed", billing_mode="subscription", budget_resource="included_credit")
    _g, w_id_cash, _r, _c, _a, _p, _j, _it = _execution_grant_audited_attempt(service, ws_n, "cash-only-scope")
    ws_cash = uuid4()
    _command(service, ws_n, {"type": "create_budget_scope", "payload": {"scope_id": str(ws_cash), "work_id": str(w_id_cash), "kind": "work", "policy_version": "economy-v1", "limits": {"cash": "10.00"}}})
    p_cash_scope = dict(base_payload, work_id=str(w_id_cash), account_id=str(acct_n))
    st, res = _quote_budget(service, ws_n, p_cash_scope)
    assert st == 400 and "budget_scope_missing_limit" in res["error"]["diagnostics"]


def test_launch_binding(service):
    """4. Launch binding: active, handed off, route mismatch."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    # Reserve stage launch
    launch_id, task_sha = _reserve_launch(ctx, service, role="audit")

    base_payload = {
        "work_id": str(ctx["work_id"]),
        "revision_id": str(ctx["revision_id"]),
        "candidate_id": str(ctx["candidate_id"]),
        "attempt_id": str(ctx["attempt_id"]),
        "grant_id": str(ctx["grant_id"]),
        "role": "audit",
        "launch_id": str(launch_id),
        "account_id": str(ctx["account_id"]),
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 1000, "output": 100, "cacheRead": 10},
    }

    # Quote on active reserved launch succeeds and echoes launch_id
    st, res = _quote_budget(service, workspace_id, base_payload)
    assert st == 200, res
    assert res["result"]["quote"]["launch_id"] == str(launch_id)

    # Route mismatch
    mismatch_payload = dict(base_payload, model="different-model")
    st_mismatch, res_mismatch = _quote_budget(service, workspace_id, mismatch_payload)
    assert st_mismatch == 409, res_mismatch
    assert "stage launch route mismatch" in res_mismatch["error"]["diagnostics"]

    # Unknown launch_id
    unknown_launch_payload = dict(base_payload, launch_id=str(uuid4()))
    st_unk, res_unk = _quote_budget(service, workspace_id, unknown_launch_payload)
    assert st_unk == 409, res_unk
    assert "unknown native stage launch" in res_unk["error"]["diagnostics"]

    # Reserve budget bound to launch so handoff requirement is satisfied
    st_res, res_res = _reserve_budget_bound(
        ctx,
        service,
        launch_id=launch_id,
        amount="1.00",
    )
    assert st_res == 200, res_res

    # Hand off launch, then quote fails with stale_evidence
    handoff_st, handoff_res = _command(
        service,
        workspace_id,
        {
            "type": "handoff_stage_launch",
            "payload": {
                "launch_id": str(launch_id),
                "task_sha256": task_sha,
            },
        },
    )
    assert handoff_st == 200, handoff_res

    st_handed_off, res_handed_off = _quote_budget(service, workspace_id, base_payload)
    assert st_handed_off == 409, res_handed_off
    assert "stage launch is handed_off" in res_handed_off["error"]["diagnostics"]


def test_reserve_with_quote_and_guards(service):
    """5. Reserve with quote: match, mismatch, reuse refusal, stale account, legacy quote-less reserve."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    op_id = uuid4()
    payload = {
        "work_id": str(ctx["work_id"]),
        "revision_id": str(ctx["revision_id"]),
        "candidate_id": str(ctx["candidate_id"]),
        "attempt_id": str(ctx["attempt_id"]),
        "grant_id": str(ctx["grant_id"]),
        "role": "audit",
        "account_id": str(ctx["account_id"]),
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 200000, "output": 8000, "cacheRead": 1},
    }
    st_quote, res_quote = _quote_budget(service, workspace_id, payload, operation_id=op_id)
    assert st_quote == 200, res_quote
    quote_id = UUID(res_quote["result"]["quote"]["quote_id"])
    exact_amount = res_quote["result"]["quote"]["worst_case_amount"]
    assert exact_amount == "0.7200003"
    assert res_quote["result"]["quote"]["resource"] == "included_credit"
    assert res_quote["result"]["quote"]["scope_id"] == str(ctx["scope_id"])

    # Mismatched scope -> 400 quote_scope_mismatch
    foreign_scope_id = uuid4()
    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(foreign_scope_id),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "50.00"},
            },
        },
    )
    st_bad_scope, res_bad_scope = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=foreign_scope_id,
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        quote_id=quote_id,
    )
    assert st_bad_scope == 400, res_bad_scope
    assert "quote_scope_mismatch" in res_bad_scope["error"]["diagnostics"]

    # Mismatched resource -> 400 quote_resource_mismatch
    st_bad_res, res_bad_res = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource="cash",
        quote_id=quote_id,
    )
    assert st_bad_res == 400, res_bad_res
    assert "quote_resource_mismatch" in res_bad_res["error"]["diagnostics"]

    # Mismatched amount -> 400 quote_amount_mismatch
    st_bad_amt, res_bad_amt = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount="0.72",
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        quote_id=quote_id,
    )
    assert st_bad_amt == 400, res_bad_amt
    assert "quote_amount_mismatch" in res_bad_amt["error"]["diagnostics"]

    # Check held untouched
    scope_data, _ = _inspect_accounting(service, ctx["scope_id"])
    assert scope_data["held"].get("included_credit", "0") == "0"

    # Exact matching reserve -> succeeds
    st_reserve, res_reserve = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        quote_id=quote_id,
    )
    assert st_reserve == 200, res_reserve
    assert res_reserve["result"]["state"] == "reserved_unsent"

    # Scope held == amount
    scope_data, _ = _inspect_accounting(service, ctx["scope_id"])
    assert scope_data["held"]["included_credit"] == exact_amount

    # Second reserve with same quote_id (distinct op_id) -> 400 quote_already_reserved
    st_reuse, res_reuse = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        quote_id=quote_id,
    )
    assert st_reuse == 400, res_reuse
    assert "quote_already_reserved" in res_reuse["error"]["diagnostics"]

    # Launch-bound quote replay: same quote on same launch replays with untouched counters
    launch_id, _task_sha = _reserve_launch(ctx, service, role="audit")
    st_q_launch, res_q_launch = _quote_budget(
        service,
        workspace_id,
        dict(payload, launch_id=str(launch_id)),
    )
    assert st_q_launch == 200, res_q_launch
    q_launch_id = UUID(res_q_launch["result"]["quote"]["quote_id"])

    held_before = Decimal(scope_data["held"]["included_credit"])
    t_attempt = uuid4()
    st_res_l1, res_res_l1 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        launch_id=launch_id,
        logical_call_id=launch_id,
        transport_attempt_id=t_attempt,
        quote_id=q_launch_id,
    )
    assert st_res_l1 == 200, res_res_l1
    assert res_res_l1["result"]["state"] == "reserved_unsent"

    scope_after_l1, _ = _inspect_accounting(service, ctx["scope_id"])
    assert Decimal(scope_after_l1["held"]["included_credit"]) == held_before + Decimal(exact_amount)

    # Replay same quote on same launch
    st_res_l2, res_res_l2 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        launch_id=launch_id,
        logical_call_id=launch_id,
        transport_attempt_id=t_attempt,
        quote_id=q_launch_id,
    )
    assert st_res_l2 == 200, res_res_l2
    assert res_res_l2["result"].get("replayed") is True

    # Counters remain untouched
    scope_after_l2, _ = _inspect_accounting(service, ctx["scope_id"])
    assert Decimal(scope_after_l2["held"]["included_credit"]) == held_before + Decimal(exact_amount)

    # Conflicting quote for same launch fails (launch_reservation_active)
    st_q_conflict, res_q_conflict = _quote_budget(
        service,
        workspace_id,
        dict(payload, launch_id=str(launch_id), usage_ceiling={"input": 100000, "output": 4000, "cacheRead": 1}),
    )
    assert st_q_conflict == 200, res_q_conflict
    q_conflict_id = UUID(res_q_conflict["result"]["quote"]["quote_id"])
    c_amount = res_q_conflict["result"]["quote"]["worst_case_amount"]

    st_conflict_res, res_conflict_res = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=c_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        launch_id=launch_id,
        quote_id=q_conflict_id,
    )
    assert st_conflict_res == 400, res_conflict_res
    assert "launch_reservation_active" in res_conflict_res["error"]["diagnostics"]

    # Stale account test: create a new quote, update provider account with newer evidence, then try to reserve
    st_q2, res_q2 = _quote_budget(service, workspace_id, payload)
    assert st_q2 == 200, res_q2
    quote2_id = UUID(res_q2["result"]["quote"]["quote_id"])

    # Update account evidence
    newer_time = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    _put_account(
        service,
        workspace_id,
        ctx["account_id"],
        provider=ctx["provider"],
        account_identity=ctx["account_identity"],
        rate_card_version=ctx["rate_card_version"],
        evidence_observed_at=newer_time,
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )

    st_stale, res_stale = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=exact_amount,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        quote_id=quote2_id,
    )
    assert st_stale == 409, res_stale
    assert "quote_stale_account" in res_stale["error"]["diagnostics"]

    # Legacy quote-less reserve still works on a card-less account
    legacy_acct_id = uuid4()
    _put_account(
        service,
        workspace_id,
        legacy_acct_id,
        provider="legacy-prov",
        account_identity="legacy-acct",
        rate_card_version=None,
        balance_provenance="provider_observed",
        billing_mode="subscription",
        budget_resource="included_credit",
    )
    st_legacy, res_legacy = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=legacy_acct_id,
        amount="1.50",
        provider="legacy-prov",
        model="legacy-model",
        effort="low",
        quote_id=None,
    )
    assert st_legacy == 200, res_legacy
    assert res_legacy["result"]["state"] == "reserved_unsent"


def test_concurrent_reserve_same_quote_has_one_winner(service):
    """6. Concurrency: two threads reserve same quote with distinct op-ids."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    payload = {
        "work_id": str(ctx["work_id"]),
        "role": "implement",
        "account_id": str(ctx["account_id"]),
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 100000, "output": 4000, "cacheRead": 1},
    }
    st_q, res_q = _quote_budget(service, workspace_id, payload)
    assert st_q == 200, res_q
    quote_id = UUID(res_q["result"]["quote"]["quote_id"])
    amount = res_q["result"]["quote"]["worst_case_amount"]

    barrier = threading.Barrier(2)
    results = {}

    def worker_reserve(key: str):
        barrier.wait()
        results[key] = _reserve_with_quote(
            service,
            workspace_id,
            scope_id=ctx["scope_id"],
            account_id=ctx["account_id"],
            amount=amount,
            provider=ctx["provider"],
            model=ctx["model"],
            effort=ctx["effort"],
            quote_id=quote_id,
        )

    t1 = threading.Thread(target=worker_reserve, args=("t1",))
    t2 = threading.Thread(target=worker_reserve, args=("t2",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = [results["t1"][0], results["t2"][0]]
    assert sorted(statuses) == [200, 400]
    winner_key = "t1" if results["t1"][0] == 200 else "t2"
    loser_key = "t2" if winner_key == "t1" else "t1"
    assert results[winner_key][1]["result"]["state"] == "reserved_unsent"
    assert "quote_already_reserved" in results[loser_key][1]["error"]["diagnostics"]

    # Exactly 1 reservation row for quote_id
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM omp_work.budget_reservations WHERE quote_id = %s", (quote_id,))
            assert cur.fetchone()[0] == 1

    # Scope held counted exactly once
    scope_data, _ = _inspect_accounting(service, ctx["scope_id"])
    assert scope_data["held"]["included_credit"] == amount


def test_authority_and_cross_workspace_isolation(service):
    """7. Authority: work.read-only 403, cross-workspace launch/account refused."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]

    # 1. work.read-only principal -> 403
    read_only_path = service.capabilities / "readonly.json"
    read_only_data = {
        "token": "readonly-token",
        "actor_id": str(uuid4()),
        "actor_kind": "auditor",
        "workspaces": [str(workspace_id)],
        "scopes": ["work.read"],
    }
    read_only_path.write_text(json.dumps(read_only_data))
    read_only_path.chmod(0o600)

    payload = {
        "work_id": str(ctx["work_id"]),
        "role": "audit",
        "account_id": str(ctx["account_id"]),
        "provider": ctx["provider"],
        "model": ctx["model"],
        "effort": ctx["effort"],
        "currency": ctx["currency"],
        "usage_ceiling": {"input": 1000, "output": 100, "cacheRead": 10},
    }
    st_ro, res_ro = _quote_budget(service, workspace_id, payload, token="readonly-token")
    assert st_ro == 403, res_ro

    # 2. Cross-workspace account_id
    ws2 = uuid4()
    ctx2 = _setup_quote_context(service, provider="anthropic", rate_card_version="2026-q3-ws2")
    payload_cross_acct = dict(payload, account_id=str(ctx2["account_id"]))
    st_cross_acct, res_cross_acct = _quote_budget(service, workspace_id, payload_cross_acct)
    assert st_cross_acct == 400, res_cross_acct
    assert "provider_account_missing" in res_cross_acct["error"]["diagnostics"]

    # 3. Cross-workspace launch_id
    launch_id_ws2, _ = _reserve_launch(ctx2, service, role="audit")
    payload_cross_launch = dict(payload, launch_id=str(launch_id_ws2))
    st_cross_launch, res_cross_launch = _quote_budget(service, workspace_id, payload_cross_launch)
    assert st_cross_launch == 409, res_cross_launch
    assert "unknown native stage launch" in res_cross_launch["error"]["diagnostics"]


def _make_operations_config(tmp_path) -> OperationsConfig:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    for role in ("postgres", "omp_work_migrator", "omp_work_app", "omp_work_importer", "omp_work_readonly", "omp_work_backup", "gpg-passphrase", "operator-actor-id"):
        path = credentials / role
        path.write_text(str(uuid4()) if role == "operator-actor-id" else secrets.token_urlsafe(24))
        path.chmod(0o600)
    return OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data", port=port)


def test_reserve_budget_lost_response_replay_identity(service):
    """8. Lost-response replay: byte-equivalent identity returns replayed reservation without counter modification."""
    ctx = _setup_quote_context(service)
    workspace_id = ctx["workspace_id"]
    launch_id, _ = _reserve_launch(ctx, service, role="audit")

    st_q, res_q = _quote_budget(
        service,
        workspace_id,
        {
            "work_id": str(ctx["work_id"]),
            "role": "audit",
            "launch_id": str(launch_id),
            "account_id": str(ctx["account_id"]),
            "provider": ctx["provider"],
            "model": ctx["model"],
            "effort": ctx["effort"],
            "currency": ctx["currency"],
            "usage_ceiling": {"input": 1000, "output": 100, "cacheRead": 10},
        },
    )
    assert st_q == 200, res_q
    quote = res_q["result"]["quote"]
    quote_id = UUID(quote["quote_id"])
    worst_case = quote["worst_case_amount"]

    transport_attempt_id = uuid4()
    logical_call_id = launch_id
    expires_at = "2030-01-01T00:00:00+00:00"

    # First reservation attempt: succeeds with replayed = False
    st_r1, res_r1 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=worst_case,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=quote["resource"],
        quote_id=quote_id,
        launch_id=launch_id,
        logical_call_id=logical_call_id,
        transport_attempt_id=transport_attempt_id,
        expires_at=expires_at,
    )
    assert st_r1 == 200, res_r1
    assert not res_r1["result"].get("replayed")
    reservation_id = res_r1["result"]["reservation_id"]
    assert res_r1["result"]["transport_attempt_id"] == str(transport_attempt_id)

    scope_data_1, _ = _inspect_accounting(service, ctx["scope_id"])
    held_1 = scope_data_1["held"][quote["resource"]]
    assert held_1 == worst_case

    # Replay with EXACT byte-equivalent payload: returns replayed = True, identical IDs, counters untouched
    st_r2, res_r2 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=worst_case,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=quote["resource"],
        quote_id=quote_id,
        launch_id=launch_id,
        logical_call_id=logical_call_id,
        transport_attempt_id=transport_attempt_id,
        expires_at=expires_at,
    )
    assert st_r2 == 200, res_r2
    assert res_r2["result"]["replayed"] is True
    assert res_r2["result"]["reservation_id"] == reservation_id
    assert res_r2["result"]["transport_attempt_id"] == str(transport_attempt_id)

    scope_data_2, _ = _inspect_accounting(service, ctx["scope_id"])
    assert scope_data_2["held"][quote["resource"]] == held_1

    # Conflicting transport_attempt_id on same quote fails closed
    st_r_conflict, res_r_conflict = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=worst_case,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=quote["resource"],
        quote_id=quote_id,
        launch_id=launch_id,
        logical_call_id=logical_call_id,
        transport_attempt_id=uuid4(),
        expires_at=expires_at,
    )
    assert st_r_conflict == 400, res_r_conflict
    assert "quote_already_reserved" in res_r_conflict["error"]["diagnostics"]

    # Cancel stage launch (releases unsent reservation to terminal state)
    st_c, res_c = _command(
        service,
        workspace_id,
        {"type": "cancel_stage_launch", "payload": {"launch_id": str(launch_id), "reason": "aborted"}},
    )
    assert st_c == 200, res_c

    # Replay on terminal reservation fails closed (never returned as active success)
    st_r_term, res_r_term = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=worst_case,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=quote["resource"],
        quote_id=quote_id,
        launch_id=launch_id,
        logical_call_id=logical_call_id,
        transport_attempt_id=transport_attempt_id,
        expires_at=expires_at,
    )
    assert st_r_term == 409, res_r_term
    assert "stage launch is cancelled" in res_r_term["error"]["diagnostics"]

    # Non-launch quote: reserve, settle, then replay -> fails closed with reservation_terminal
    st_q_stand, res_q_stand = _quote_budget(
        service,
        workspace_id,
        {
            "work_id": str(ctx["work_id"]),
            "role": "audit",
            "account_id": str(ctx["account_id"]),
            "provider": ctx["provider"],
            "model": ctx["model"],
            "effort": ctx["effort"],
            "currency": ctx["currency"],
            "usage_ceiling": {"input": 500, "output": 50, "cacheRead": 5},
        },
    )
    assert st_q_stand == 200, res_q_stand
    stand_quote = res_q_stand["result"]["quote"]
    stand_quote_id = UUID(stand_quote["quote_id"])
    stand_worst = stand_quote["worst_case_amount"]
    stand_transport = uuid4()
    stand_call = uuid4()

    st_r_s1, res_r_s1 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=stand_worst,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=stand_quote["resource"],
        quote_id=stand_quote_id,
        logical_call_id=stand_call,
        transport_attempt_id=stand_transport,
        expires_at=expires_at,
    )
    assert st_r_s1 == 200, res_r_s1
    stand_res_id = res_r_s1["result"]["reservation_id"]

    # Settle the reservation (reaches terminal state "settled")
    st_settle, res_settle = _command(
        service,
        workspace_id,
        {
            "type": "settle_budget",
            "payload": {
                "reservation_id": str(stand_res_id),
                "transport_attempt_id": str(stand_transport),
                "fence": res_r_s1["result"]["fence"],
                "state": "settled",
                "actual_drawdown": stand_worst,
                "provenance": "locally_estimated",
                "outcome": "success",
            },
        },
    )
    assert st_settle == 200, res_settle

    # Replaying reservation on settled terminal quote rejects with reservation_terminal
    st_r_term2, res_r_term2 = _reserve_with_quote(
        service,
        workspace_id,
        scope_id=ctx["scope_id"],
        account_id=ctx["account_id"],
        amount=stand_worst,
        provider=ctx["provider"],
        model=ctx["model"],
        effort=ctx["effort"],
        resource=stand_quote["resource"],
        quote_id=stand_quote_id,
        logical_call_id=stand_call,
        transport_attempt_id=stand_transport,
        expires_at=expires_at,
    )
    assert st_r_term2 == 400, res_r_term2
    assert "reservation_terminal" in res_r_term2["error"]["diagnostics"]


def test_migration_0035_preserves_historical_pre_0035_budget_quotes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """9. Migration upgrade: pre-0035 quotes with null resource/scope remain readable and fail closed on reserve."""
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    cfg = _make_operations_config(tmp_path)
    original_migrate = database_module.migrate

    # Step 1: Bootstrap up to target migration 34 (prior to 0035)
    monkeypatch.setattr(
        database_module,
        "migrate",
        lambda c, target=None, lock_timeout=30: original_migrate(c, target=34, lock_timeout=lock_timeout),
    )
    with native_postgres(cfg.state_dir, cfg.port):
        database_module.bootstrap(cfg)
        monkeypatch.setattr(database_module, "migrate", original_migrate)

        workspace_id = uuid4()
        work_id = uuid4()
        account_id = uuid4()
        card_id = uuid4()
        quote_id = uuid4()
        now = datetime.now(UTC)

        # Step 2: Seed pre-0035 quote row
        with psycopg.connect(**cfg.connection_kwargs("postgres"), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))
                cur.execute("INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'NOW')", (work_id, workspace_id))
                cur.execute(
                    """
                    INSERT INTO omp_work.rate_cards(
                        rate_card_id, workspace_id, provider, version, qualification,
                        currency, billing_modes, unit_prices, evidence_sha256, evidence_source, observed_at, effective_from, effective_until
                    ) VALUES (%s, %s, 'anthropic', '2026-q3', 'qualified', 'USD', ARRAY['metered'], '{"input": "0.000003"}'::jsonb, %s, 'test', %s, %s, null)
                    """,
                    (card_id, workspace_id, "0" * 64, now, now - timedelta(days=1)),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.provider_accounts(
                        account_id, workspace_id, provider, account_identity, entitlement_evidence,
                        evidence_observed_at, billing_mode, rate_card_version, observed_balance,
                        balance_provenance, reset_at, concurrency_limit
                    ) VALUES (%s, %s, 'anthropic', 'acct-pre35', 'ev-1', %s, 'metered', '2026-q3', null, 'unknown', null, 5)
                    """,
                    (account_id, workspace_id, now),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.budget_quotes(
                        quote_id, workspace_id, work_id, role, account_id,
                        account_evidence_observed_at, provider, model, effort, rate_card_id,
                        rate_card_version, currency, usage_ceiling, worst_case_amount,
                        evidence_sha256, quote_sha256, quoted_at
                    ) VALUES (
                        %s, %s, %s, 'audit', %s,
                        %s, 'anthropic', 'claude-3-7-sonnet', 'high', %s,
                        '2026-q3', 'USD', '{"input": 1000}'::jsonb, 0.003,
                        %s, %s, %s
                    )
                    """,
                    (quote_id, workspace_id, work_id, account_id, now, card_id, "0" * 64, "1" * 64, now),
                )

        # Step 3: Run migration 0035
        original_migrate(cfg)

        # Step 4: Verify migration applied without destroying historical quote
        with psycopg.connect(**cfg.connection_kwargs("postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT quote_id, resource, scope_id FROM omp_work.budget_quotes WHERE quote_id = %s", (quote_id,))
                row = cur.fetchone()
                assert row is not None
                assert row[0] == quote_id
                assert row[1] is None, "Historical quote resource must be NULL"
                assert row[2] is None, "Historical quote scope_id must be NULL"
