"""Disposable PostgreSQL contract tests for authoritative provider account provisioning and inspection."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work import contract_sha256
from test_workflow_service import _command, _grant, _owner_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

OPERATOR_ID = UUID("00000000-0000-7000-8000-000000000099")


def _setup_operator(service, workspace_id: UUID) -> None:
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    _grant(service, workspace_id)
    operator_path = service.capabilities / "operator.json"
    data = {
        "token": "operator-token",
        "actor_id": str(OPERATOR_ID),
        "actor_kind": "operator",
        "workspaces": [str(workspace_id)],
        "scopes": ["work.read", "work.operate"],
    }
    if operator_path.exists():
        try:
            existing = json.loads(operator_path.read_text())
            workspaces = set(existing.get("workspaces", []))
            workspaces.add(str(workspace_id))
            data["workspaces"] = list(workspaces)
        except Exception:
            pass
    operator_path.write_text(json.dumps(data))
    operator_path.chmod(0o600)


def _put_account(
    service,
    workspace_id: UUID,
    account_id: UUID,
    *,
    provider: str = "openai",
    account_identity: str = "org-123",
    entitlement_evidence: str = "tier-5-active",
    evidence_observed_at: str | None = None,
    billing_mode: str = "metered",
    budget_resource: str | None = None,
    rate_card_version: str | None = None,
    observed_balance: str | None = "500.00",
    balance_provenance: str = "provider_observed",
    reset_at: str | None = None,
    concurrency_limit: int = 4,
    token: str = "operator-token",
    operation_id: UUID | None = None,
) -> tuple[int, dict]:
    obs_time = evidence_observed_at or datetime.now(UTC).isoformat()
    return _command(
        service,
        workspace_id,
        {
            "type": "put_provider_account",
            "payload": {
                "account_id": str(account_id),
                "provider": provider,
                "account_identity": account_identity,
                "entitlement_evidence": entitlement_evidence,
                "evidence_observed_at": obs_time,
                "billing_mode": billing_mode,
                "budget_resource": budget_resource,
                "rate_card_version": rate_card_version,
                "observed_balance": observed_balance,
                "balance_provenance": balance_provenance,
                "reset_at": reset_at,
                "concurrency_limit": concurrency_limit,
            },
        },
        token=token,
        operation_id=operation_id,
    )


def test_put_provider_account_insert_and_inspect(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    obs_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    reset_at = datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC).isoformat()

    status_rc, resp_rc = _command(
        service,
        workspace_id,
        {
            "type": "register_rate_card",
            "payload": {
                "rate_card_id": str(uuid4()),
                "provider": "anthropic",
                "version": "2026-q3",
                "billing_modes": ["purchased_credit"],
                "effective_from": obs_at,
                "effective_until": None,
                "currency": "USD",
                "unit_prices": {"input": "0.000003", "output": "0.000015"},
                "evidence_sha256": "a" * 64,
                "evidence_source": "https://anthropic.com/pricing",
                "observed_at": obs_at,
                "qualification": "qualified",
            },
        },
        token="operator-token",
    )
    assert status_rc == 200, resp_rc

    status, resp = _put_account(
        service,
        workspace_id,
        account_id,
        provider="anthropic",
        account_identity="team-scale",
        entitlement_evidence="api-entitlement-v2",
        evidence_observed_at=obs_at,
        billing_mode="purchased_credit",
        rate_card_version="2026-q3",
        observed_balance="1250.75",
        balance_provenance="provider_observed",
        reset_at=reset_at,
        concurrency_limit=8,
    )
    assert status == 200, resp
    assert resp["receipt"]["state"] == "applied"
    result = resp["result"]
    assert result["type"] == "put_provider_account"
    assert result["status"] == "inserted"
    assert result["account_id"] == str(account_id)
    account = result["account"]
    assert account["account_id"] == str(account_id)
    assert account["workspace_id"] == str(workspace_id)
    assert account["provider"] == "anthropic"
    assert account["account_identity"] == "team-scale"
    assert account["rate_card_version"] == "2026-q3"
    assert account["observed_balance"] == "1250.75"
    assert account["balance_provenance"] == "provider_observed"
    assert account["concurrency_limit"] == 8

    # Single read route
    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{account_id}",
        headers=headers,
    )
    assert read_resp.status_code == 200, read_resp.json()
    item = read_resp.json()
    assert item["account_id"] == str(account_id)
    assert item["provider"] == "anthropic"
    assert item["account_identity"] == "team-scale"
    assert item["rate_card_version"] == "2026-q3"
    assert item["observed_balance"] == "1250.75"
    assert item["billing_mode"] == "purchased_credit"

    # Workspace list route
    list_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts",
        headers=headers,
    )
    assert list_resp.status_code == 200, list_resp.json()
    list_data = list_resp.json()
    assert list_data["workspace_id"] == str(workspace_id)
    assert len(list_data["accounts"]) >= 1
    found = [a for a in list_data["accounts"] if a["account_id"] == str(account_id)]
    assert len(found) == 1
    assert found[0]["account_identity"] == "team-scale"


def test_put_provider_account_update_newer_evidence(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()

    t1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    t2 = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC).isoformat()

    status, resp1 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="google",
        account_identity="proj-alpha",
        entitlement_evidence="tier-1",
        evidence_observed_at=t1,
        observed_balance="50.00",
        concurrency_limit=2,
    )
    assert status == 200
    assert resp1["result"]["status"] == "inserted"

    # Update with newer evidence time
    status, resp2 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="google",
        account_identity="proj-alpha",
        entitlement_evidence="tier-2",
        evidence_observed_at=t2,
        observed_balance="200.00",
        concurrency_limit=6,
    )
    assert status == 200
    assert resp2["result"]["status"] == "updated"
    assert resp2["result"]["account"]["observed_balance"] == "200.00"
    assert resp2["result"]["account"]["concurrency_limit"] == 6
    assert resp2["result"]["account"]["entitlement_evidence"] == "tier-2"


def test_put_provider_account_unchanged_identical_evidence(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    t1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).isoformat()

    status, resp1 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="cohere",
        account_identity="cohere-prod",
        entitlement_evidence="standard",
        evidence_observed_at=t1,
        observed_balance="10.00",
        concurrency_limit=3,
    )
    assert status == 200
    assert resp1["result"]["status"] == "inserted"

    # Send exact same data with exact same evidence_observed_at
    status, resp2 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="cohere",
        account_identity="cohere-prod",
        entitlement_evidence="standard",
        evidence_observed_at=t1,
        observed_balance="10.00",
        concurrency_limit=3,
    )
    assert status == 200
    assert resp2["result"]["status"] == "unchanged"


def test_put_provider_account_stale_evidence_rejected(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    t1 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC).isoformat()
    t0 = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC).isoformat()

    status, _ = _put_account(
        service,
        workspace_id,
        account_id,
        provider="mistral",
        account_identity="mistral-eu",
        evidence_observed_at=t1,
        observed_balance="100.00",
    )
    assert status == 200

    # 1. Older evidence timestamp
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        provider="mistral",
        account_identity="mistral-eu",
        evidence_observed_at=t0,
        observed_balance="90.00",
    )
    assert status == 409
    assert err["error"]["code"] == "stale_evidence"

    # 2. Equal evidence timestamp with differing data
    status, err2 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="mistral",
        account_identity="mistral-eu",
        evidence_observed_at=t1,
        observed_balance="150.00",  # Differing balance
    )
    assert status == 409
    assert err2["error"]["code"] == "stale_evidence"


def test_put_provider_account_conflict_mismatch(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id_1 = uuid4()
    account_id_2 = uuid4()
    obs = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()

    status, _ = _put_account(
        service,
        workspace_id,
        account_id_1,
        provider="bedrock",
        account_identity="aws-us-east-1",
        evidence_observed_at=obs,
    )
    assert status == 200

    # Conflict 1: Put existing provider/account_identity with a different account_id
    status, err1 = _put_account(
        service,
        workspace_id,
        account_id_2,
        provider="bedrock",
        account_identity="aws-us-east-1",
        evidence_observed_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC).isoformat(),
    )
    assert status == 409
    assert err1["error"]["code"] == "revision_conflict"

    # Conflict 2: Put existing account_id with a different provider
    status, err2 = _put_account(
        service,
        workspace_id,
        account_id_1,
        provider="azure",
        account_identity="aws-us-east-1",
        evidence_observed_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC).isoformat(),
    )
    assert status == 409
    assert err2["error"]["code"] == "revision_conflict"

    # Conflict 3: Put existing account_id with a different account_identity
    status, err3 = _put_account(
        service,
        workspace_id,
        account_id_1,
        provider="bedrock",
        account_identity="aws-eu-west-1",
        evidence_observed_at=datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC).isoformat(),
    )
    assert status == 409
    assert err3["error"]["code"] == "revision_conflict"


def test_put_provider_account_cross_workspace_account_id_conflict(service):
    """Provisioning account_id in workspace A then attempting same account_id in workspace B must fail with 409 revision_conflict."""
    workspace_a = uuid4()
    workspace_b = uuid4()
    _setup_operator(service, workspace_a)
    _setup_operator(service, workspace_b)
    account_id = uuid4()

    status, resp = _put_account(
        service,
        workspace_a,
        account_id,
        provider="anthropic",
        account_identity="claude-ws-a",
    )
    assert status == 200
    assert resp["result"]["status"] == "inserted"

    status, err = _put_account(
        service,
        workspace_b,
        account_id,
        provider="anthropic",
        account_identity="claude-ws-b",
    )
    assert status == 409
    assert err["error"]["code"] == "revision_conflict"


def test_put_provider_account_idempotent_replay_and_conflict(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    op_id = uuid4()
    obs = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()

    status, resp1 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="replay-prov",
        account_identity="replay-id",
        evidence_observed_at=obs,
        operation_id=op_id,
    )
    assert status == 200
    assert resp1["receipt"]["state"] == "applied"
    req_hash = resp1["receipt"]["request_sha256"]

    # Replay same envelope
    status, resp2 = _put_account(
        service,
        workspace_id,
        account_id,
        provider="replay-prov",
        account_identity="replay-id",
        evidence_observed_at=obs,
        operation_id=op_id,
    )
    assert status == 200
    assert resp2["receipt"]["state"] == "replayed"
    assert resp2["receipt"]["request_sha256"] == req_hash
    assert resp2["result"] == resp1["result"]

    # Reusing same operation_id with differing payload -> conflict
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        provider="replay-prov",
        account_identity="replay-id",
        evidence_observed_at=obs,
        observed_balance="999.99",  # Different payload
        operation_id=op_id,
    )
    assert status == 409
    assert err["error"]["code"] == "idempotency_conflict"


def test_put_provider_account_validation_invariants(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    now = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)

    # Negative balance
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        observed_balance="-5.00",
        evidence_observed_at=now.isoformat(),
    )
    assert status == 400

    # Unknown provenance with non-null balance
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        observed_balance="10.00",
        balance_provenance="unknown",
        evidence_observed_at=now.isoformat(),
    )
    assert status == 400

    # reset_at in the past / before evidence_observed_at
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        evidence_observed_at=now.isoformat(),
        reset_at=(now - timedelta(hours=1)).isoformat(),
    )
    assert status == 400

    # concurrency_limit <= 0
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        concurrency_limit=0,
        evidence_observed_at=now.isoformat(),
    )
    assert status == 400

    # empty rate_card_version
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        rate_card_version="   ",
        evidence_observed_at=now.isoformat(),
    )
    assert status == 400


def test_put_provider_account_authorization(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()

    # Owner principal has work.execute, work.mutate, work.read, etc., but lacks work.operate
    status, err = _put_account(
        service,
        workspace_id,
        account_id,
        token="owner-token",
    )
    assert status == 403
    assert err["error"]["code"] == "forbidden"

    # Operator principal has work.operate -> succeeds
    status, resp = _put_account(
        service,
        workspace_id,
        account_id,
        token="operator-token",
    )
    assert status == 200
    assert resp["result"]["status"] == "inserted"


def test_provider_account_reads_and_cross_workspace_denial(service):
    w1 = uuid4()
    w2 = uuid4()
    _setup_operator(service, w1)
    _setup_operator(service, w2)

    acc1 = uuid4()
    acc2 = uuid4()

    _put_account(service, w1, acc1, provider="p1", account_identity="w1-acc")
    _put_account(service, w2, acc2, provider="p2", account_identity="w2-acc")

    # Read acc1 from w1 -> 200
    h1 = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(w1),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    r = service.client.get(f"/v1/workspaces/{w1}/provider-accounts/{acc1}", headers=h1)
    assert r.status_code == 200
    assert r.json()["account_identity"] == "w1-acc"

    # Read acc2 from w1 -> 400 (not found in w1)
    r = service.client.get(f"/v1/workspaces/{w1}/provider-accounts/{acc2}", headers=h1)
    assert r.status_code == 400
    assert "not_found" in r.json()["error"]["diagnostics"]

    # List read for w1 only returns w1 accounts
    r = service.client.get(f"/v1/workspaces/{w1}/provider-accounts", headers=h1)
    assert r.status_code == 200
    accounts = r.json()["accounts"]
    ids = [a["account_id"] for a in accounts]
    assert str(acc1) in ids
    assert str(acc2) not in ids


def test_direct_table_mutation_denied_to_app_role(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()

    with psycopg.connect(
        **service.config.connection_kwargs("omp_work_app"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(OPERATOR_ID)),
            )
            # Direct INSERT denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    """
                    INSERT INTO omp_work.provider_accounts(
                        account_id, workspace_id, provider, account_identity,
                        entitlement_evidence, evidence_observed_at, billing_mode,
                        balance_provenance, concurrency_limit
                    ) VALUES (%s, %s, 'hack', 'hack', 'hack', clock_timestamp(), 'subscription', 'provider_observed', 1)
                    """,
                    (account_id, workspace_id),
                )

            # Direct UPDATE denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE omp_work.provider_accounts SET concurrency_limit = 99 WHERE workspace_id = %s",
                    (workspace_id,),
                )

            # Direct DELETE denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM omp_work.provider_accounts WHERE workspace_id = %s",
                    (workspace_id,),
                )


def test_stage_budget_binding_and_reservation_integration(service):
    """Verify that provisioned provider account activates stage budget binding and functions with budget reservations."""
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    scope_id = uuid4()

    # 1. Provision provider account for provider "bound-prov" with concurrency limit 1
    status, resp = _put_account(
        service,
        workspace_id,
        account_id,
        provider="bound-prov",
        account_identity="binding-account-1",
        billing_mode="subscription",
        balance_provenance="provider_observed",
        concurrency_limit=1,
    )
    assert status == 200
    assert resp["result"]["status"] == "inserted"

    # 2. Create budget scope
    status, scope_res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(scope_id),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "100.00"},
            },
        },
    )
    assert status == 200, scope_res

    # 3. Reserve budget using the provisioned provider account
    res_status, res_data = _command(
        service,
        workspace_id,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(account_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(uuid4()),
                "provider": "bound-prov",
                "model": "model-1",
                "effort": "high",
                "resource": "included_credit",
                "worst_case_drawdown": "10.00",
                "context_limit": 4000,
                "output_limit": 1000,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        },
    )
    assert res_status == 200, res_data
    assert res_data["result"]["type"] == "reserve_budget"
    assert res_data["result"]["state"] == "reserved_unsent"

    # 4. Attempting a 2nd concurrent reservation fails because concurrency_limit=1
    res2_status, res2_data = _command(
        service,
        workspace_id,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(account_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(uuid4()),
                "provider": "bound-prov",
                "model": "model-1",
                "effort": "high",
                "resource": "included_credit",
                "worst_case_drawdown": "10.00",
                "context_limit": 4000,
                "output_limit": 1000,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        },
    )
    assert res2_status == 409
    assert res2_data["error"]["code"] == "budget_exhausted"
    assert "account_slots_exhausted" in res2_data["error"]["diagnostics"]


def test_put_provider_account_budget_resource_authority(service):
    """Authority test for explicit budget_resource column on provider_accounts."""
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)

    # 1. Roundtrip all valid budget resources
    valid_resources = ["cash", "included_credit", "native_quota", "local_compute"]
    for resource in valid_resources:
        acct_id = uuid4()
        status, res = _put_account(
            service,
            workspace_id,
            acct_id,
            provider="openai",
            account_identity=f"acct-{resource}",
            budget_resource=resource,
        )
        assert status == 200, res
        assert res["result"]["account"]["budget_resource"] == resource

        # Inspect via read
        read_resp = service.client.get(
            f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id}",
            headers=_owner_headers(service),
        )
        assert read_resp.status_code == 200, read_resp.json()
        assert read_resp.json()["budget_resource"] == resource

    # 2. Legacy / null budget_resource roundtrip
    null_acct_id = uuid4()
    status, res = _put_account(
        service,
        workspace_id,
        null_acct_id,
        provider="openai",
        account_identity="acct-null",
        budget_resource=None,
    )
    assert status == 200, res
    assert res["result"]["account"]["budget_resource"] is None

    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{null_acct_id}",
        headers=_owner_headers(service),
    )
    assert read_resp.status_code == 200, read_resp.json()
    assert read_resp.json()["budget_resource"] is None

    # 3. Invalid budget_resource rejected
    invalid_acct_id = uuid4()
    status, res = _put_account(
        service,
        workspace_id,
        invalid_acct_id,
        provider="openai",
        account_identity="acct-invalid",
        budget_resource="unsupported_crypto",
    )
    assert status == 400, res

    # 4. Monotonicity and evidence update
    t1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    t2 = datetime(2026, 9, 1, 11, 0, 0, tzinfo=UTC).isoformat()
    t_stale = datetime(2026, 9, 1, 9, 0, 0, tzinfo=UTC).isoformat()

    mono_acct_id = uuid4()
    status, res = _put_account(
        service,
        workspace_id,
        mono_acct_id,
        provider="anthropic",
        account_identity="acct-mono",
        evidence_observed_at=t1,
        budget_resource="included_credit",
    )
    assert status == 200, res

    # Update with newer timestamp and changed resource succeeds
    status, res = _put_account(
        service,
        workspace_id,
        mono_acct_id,
        provider="anthropic",
        account_identity="acct-mono",
        evidence_observed_at=t2,
        budget_resource="cash",
    )
    assert status == 200, res
    assert res["result"]["account"]["budget_resource"] == "cash"

    # Stale evidence rejected
    status, res = _put_account(
        service,
        workspace_id,
        mono_acct_id,
        provider="anthropic",
        account_identity="acct-mono",
        evidence_observed_at=t_stale,
        budget_resource="local_compute",
    )
    assert status == 409, res
    assert res["error"]["code"] == "stale_evidence"
