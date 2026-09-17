"""Disposable PostgreSQL contract tests for immutable rate-card authority (OMP-233)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work import contract_sha256
from test_provider_account_authority import _put_account, _setup_operator
from test_workflow_service import _command

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _register_card(
    service,
    workspace_id: UUID,
    rate_card_id: UUID,
    *,
    provider: str = "anthropic",
    version: str = "2026-q3",
    billing_modes: list[str] | None = None,
    effective_from: str | None = None,
    effective_until: str | None = None,
    currency: str = "USD",
    unit_prices: dict[str, str] | None = None,
    evidence_sha256: str = "a" * 64,
    evidence_source: str = "https://example.com/pricing",
    observed_at: str | None = None,
    qualification: str = "qualified",
    token: str = "operator-token",
    operation_id: UUID | None = None,
) -> tuple[int, dict]:
    obs_time = observed_at or datetime.now(UTC).isoformat()
    eff_from = effective_from or datetime.now(UTC).isoformat()
    b_modes = billing_modes if billing_modes is not None else ["metered", "purchased_credit"]
    prices = unit_prices if unit_prices is not None else {"input": "0.000003", "output": "0.000015"}
    payload = {
        "rate_card_id": str(rate_card_id),
        "provider": provider,
        "version": version,
        "billing_modes": b_modes,
        "effective_from": eff_from,
        "effective_until": effective_until,
        "currency": currency,
        "unit_prices": prices,
        "evidence_sha256": evidence_sha256,
        "evidence_source": evidence_source,
        "observed_at": obs_time,
        "qualification": qualification,
    }
    return _command(
        service,
        workspace_id,
        {
            "type": "register_rate_card",
            "payload": payload,
        },
        token=token,
        operation_id=operation_id,
    )


def test_register_rate_card_insert_and_inspect(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    card_id = uuid4()
    obs_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    eff_from = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC).isoformat()
    eff_until = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC).isoformat()

    status, resp = _register_card(
        service,
        workspace_id,
        card_id,
        provider="anthropic",
        version="v1-2026",
        billing_modes=["metered", "subscription"],
        effective_from=eff_from,
        effective_until=eff_until,
        currency="USD",
        unit_prices={"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"},
        evidence_sha256="b" * 64,
        evidence_source="https://anthropic.com/pricing-v1",
        observed_at=obs_at,
        qualification="qualified",
    )
    assert status == 200, resp
    assert resp["receipt"]["state"] == "applied"
    result = resp["result"]
    assert result["type"] == "register_rate_card"
    assert result["status"] == "inserted"
    assert result["rate_card_id"] == str(card_id)
    card = result["rate_card"]
    assert card["rate_card_id"] == str(card_id)
    assert card["workspace_id"] == str(workspace_id)
    assert card["provider"] == "anthropic"
    assert card["version"] == "v1-2026"
    assert card["billing_modes"] == ["metered", "subscription"]
    assert card["currency"] == "USD"
    assert card["unit_prices"] == {"input": "0.000003", "output": "0.000015", "cacheRead": "0.0000003"}
    assert card["qualification"] == "qualified"
    assert "registered_at" in card

    # Single read
    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/rate-cards/{card_id}",
        headers=headers,
    )
    assert read_resp.status_code == 200, read_resp.text
    card_read = read_resp.json()
    assert card_read["rate_card_id"] == str(card_id)
    assert card_read["unit_prices"] == card["unit_prices"]

    # List read
    list_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/rate-cards",
        headers=headers,
    )
    assert list_resp.status_code == 200, list_resp.text
    list_body = list_resp.json()
    assert list_body["workspace_id"] == str(workspace_id)
    assert any(c["rate_card_id"] == str(card_id) for c in list_body["rate_cards"])


def test_register_rate_card_exact_replay(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    card_id = uuid4()
    obs_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    eff_from = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC).isoformat()

    status1, resp1 = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="gpt-4o-2026",
        billing_modes=["metered"],
        effective_from=eff_from,
        observed_at=obs_at,
        operation_id=uuid4(),
    )
    assert status1 == 200, resp1
    assert resp1["result"]["status"] == "inserted"

    # Exact replay with a different operation_id
    status2, resp2 = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="gpt-4o-2026",
        billing_modes=["metered"],
        effective_from=eff_from,
        observed_at=obs_at,
        operation_id=uuid4(),
    )
    assert status2 == 200, resp2
    assert resp2["result"]["status"] == "replayed"
    assert resp2["result"]["rate_card_id"] == str(card_id)


def test_register_rate_card_identity_conflicts(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    card_id = uuid4()
    obs_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    eff_from = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC).isoformat()

    status, resp = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="o3-2026",
        billing_modes=["metered"],
        effective_from=eff_from,
        unit_prices={"input": "0.000005"},
        observed_at=obs_at,
    )
    assert status == 200, resp

    # Same (provider, version) with different card_id -> 409 revision_conflict
    status_conflict1, resp_conflict1 = _register_card(
        service,
        workspace_id,
        uuid4(),
        provider="openai",
        version="o3-2026",
        billing_modes=["metered"],
        effective_from=eff_from,
        unit_prices={"input": "0.000005"},
        observed_at=obs_at,
    )
    assert status_conflict1 == 409
    assert resp_conflict1["error"]["code"] == "revision_conflict"
    assert "rate_card_identity_conflict" in resp_conflict1["error"]["diagnostics"]

    # Same (provider, version) with different price -> 409 revision_conflict
    status_conflict2, resp_conflict2 = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="o3-2026",
        billing_modes=["metered"],
        effective_from=eff_from,
        unit_prices={"input": "0.000010"},
        observed_at=obs_at,
    )
    assert status_conflict2 == 409
    assert resp_conflict2["error"]["code"] == "revision_conflict"

    # Same card_id with different version -> 409 revision_conflict
    status_conflict3, resp_conflict3 = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="o3-different-version",
        billing_modes=["metered"],
        effective_from=eff_from,
        unit_prices={"input": "0.000005"},
        observed_at=obs_at,
    )
    assert status_conflict3 == 409
    assert resp_conflict3["error"]["code"] == "revision_conflict"


def test_register_rate_card_unauthorized(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    # Owner token lacks work.operate -> 403 forbidden
    status, resp = _register_card(
        service,
        workspace_id,
        uuid4(),
        token="owner-token",
    )
    assert status == 403
    assert resp["error"]["code"] == "forbidden"


def test_cross_workspace_read_and_reference(service):
    w1 = uuid4()
    w2 = uuid4()
    _setup_operator(service, w1)
    _setup_operator(service, w2)

    card_id = uuid4()
    eff_from = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC).isoformat()
    obs_at = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()

    status, resp = _register_card(
        service,
        w1,
        card_id,
        provider="anthropic",
        version="cross-w-card",
        billing_modes=["metered"],
        effective_from=eff_from,
        observed_at=obs_at,
    )
    assert status == 200, resp

    # w2 single read of w1 card -> 400 not_found
    headers_w2 = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(w2),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp = service.client.get(
        f"/v1/workspaces/{w2}/rate-cards/{card_id}",
        headers=headers_w2,
    )
    assert read_resp.status_code == 400
    assert "not_found" in read_resp.json()["error"]["diagnostics"]

    # w2 list read excludes w1 card
    list_resp = service.client.get(
        f"/v1/workspaces/{w2}/rate-cards",
        headers=headers_w2,
    )
    assert list_resp.status_code == 200
    assert all(c["rate_card_id"] != str(card_id) for c in list_resp.json()["rate_cards"])

    # w2 account referencing w1 card -> 400 rate_card_missing
    acct_id = uuid4()
    status_acct, resp_acct = _put_account(
        service,
        w2,
        acct_id,
        provider="anthropic",
        account_identity="team-w2",
        billing_mode="metered",
        rate_card_version="cross-w-card",
    )
    assert status_acct == 400
    assert "rate_card_missing" in resp_acct["error"]["diagnostics"]


def test_direct_table_mutation_denied(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    card_id = uuid4()

    status, resp = _register_card(
        service,
        workspace_id,
        card_id,
    )
    assert status == 200, resp

    # Direct UPDATE, DELETE, TRUNCATE with omp_work_app role MUST fail with InsufficientPrivilege
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE omp_work.rate_cards SET qualification='unqualified' WHERE workspace_id=%s",
                    (workspace_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM omp_work.rate_cards WHERE workspace_id=%s",
                    (workspace_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE omp_work.rate_cards")
        conn.rollback()


def test_rate_card_validation_negatives(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)

    # 1. effective_until <= effective_from
    now = datetime.now(UTC)
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        effective_from=now.isoformat(),
        effective_until=(now - timedelta(hours=1)).isoformat(),
    )
    assert status == 400

    # 2. naive observed_at
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        observed_at="2026-09-01T12:00:00",
    )
    assert status == 400

    # 3. naive effective_from
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        effective_from="2026-09-01T00:00:00",
    )
    assert status == 400

    # 4. currency lowercase or invalid length
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        currency="usd",
    )
    assert status == 400

    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        currency="US",
    )
    assert status == 400

    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        currency="USDT",
    )
    assert status == 400

    # 5. empty unit_prices
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        unit_prices={},
    )
    assert status == 400

    # 6. blank key in unit_prices
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        unit_prices={"   ": "0.001"},
    )
    assert status == 400

    # 7. price negative or non-decimal
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        unit_prices={"input": "-1.0"},
    )
    assert status == 400

    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        unit_prices={"input": "1e3"},
    )
    assert status == 400

    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        unit_prices={"input": "abc"},
    )
    assert status == 400

    # 8. empty or duplicate billing_modes
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        billing_modes=[],
    )
    assert status == 400

    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        billing_modes=["metered", "metered"],
    )
    assert status == 400

    # 9. bad evidence_sha256
    status, _ = _register_card(
        service,
        workspace_id,
        uuid4(),
        evidence_sha256="not-a-sha256",
    )
    assert status == 400


def test_provider_account_compatibility_and_unqualified(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)

    # 1. Register a metered-only card
    card_id = uuid4()
    status, resp = _register_card(
        service,
        workspace_id,
        card_id,
        provider="anthropic",
        version="metered-only-v1",
        billing_modes=["metered"],
        qualification="qualified",
    )
    assert status == 200, resp

    # Account references metered-only card with billing_mode='subscription' -> 400 rate_card_incompatible
    acct_id1 = uuid4()
    status1, resp1 = _put_account(
        service,
        workspace_id,
        acct_id1,
        provider="anthropic",
        billing_mode="subscription",
        rate_card_version="metered-only-v1",
    )
    assert status1 == 400
    assert "rate_card_incompatible" in resp1["error"]["diagnostics"]

    # Verify no row was inserted
    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id1}",
        headers=headers,
    )
    assert read_resp.status_code == 400
    assert "not_found" in read_resp.json()["error"]["diagnostics"]

    # 2. Register an unqualified card
    card_unqual_id = uuid4()
    status_uq, resp_uq = _register_card(
        service,
        workspace_id,
        card_unqual_id,
        provider="anthropic",
        version="unqualified-v1",
        billing_modes=["metered"],
        qualification="unqualified",
    )
    assert status_uq == 200, resp_uq

    # Account references unqualified card -> 400 rate_card_unqualified
    acct_id2 = uuid4()
    status2, resp2 = _put_account(
        service,
        workspace_id,
        acct_id2,
        provider="anthropic",
        billing_mode="metered",
        rate_card_version="unqualified-v1",
    )
    assert status2 == 400
    assert "rate_card_unqualified" in resp2["error"]["diagnostics"]

    # Verify no row was inserted
    read_resp2 = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id2}",
        headers=headers,
    )
    assert read_resp2.status_code == 400

    # 3. Reference non-existent rate card -> 400 rate_card_missing
    acct_id3 = uuid4()
    status3, resp3 = _put_account(
        service,
        workspace_id,
        acct_id3,
        provider="anthropic",
        billing_mode="metered",
        rate_card_version="does-not-exist",
    )
    assert status3 == 400
    assert "rate_card_missing" in resp3["error"]["diagnostics"]

    # Verify no row was inserted
    read_resp3 = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id3}",
        headers=headers,
    )
    assert read_resp3.status_code == 400


def test_null_reference_unpriced_and_update_to_qualified(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    acct_id = uuid4()

    obs_time1 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC).isoformat()
    # 1. Null reference inserts successfully (unpriced/unknown)
    status1, resp1 = _put_account(
        service,
        workspace_id,
        acct_id,
        provider="openai",
        account_identity="team-scale-1",
        billing_mode="metered",
        rate_card_version=None,
        evidence_observed_at=obs_time1,
    )
    assert status1 == 200, resp1
    assert resp1["result"]["status"] == "inserted"
    assert resp1["result"]["account"]["rate_card_version"] is None

    # Verify read returns null rate_card_version
    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp1 = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id}",
        headers=headers,
    )
    assert read_resp1.status_code == 200
    assert read_resp1.json()["rate_card_version"] is None

    # 2. Register qualified card
    card_id = uuid4()
    status_card, resp_card = _register_card(
        service,
        workspace_id,
        card_id,
        provider="openai",
        version="card-qualified-v2",
        billing_modes=["metered"],
        qualification="qualified",
    )
    assert status_card == 200, resp_card

    # 3. Update account with newer evidence referencing qualified card -> updated
    obs_time2 = datetime(2026, 9, 2, 10, 0, 0, tzinfo=UTC).isoformat()
    status2, resp2 = _put_account(
        service,
        workspace_id,
        acct_id,
        provider="openai",
        account_identity="team-scale-1",
        billing_mode="metered",
        rate_card_version="card-qualified-v2",
        evidence_observed_at=obs_time2,
    )
    assert status2 == 200, resp2
    assert resp2["result"]["status"] == "updated"
    assert resp2["result"]["account"]["rate_card_version"] == "card-qualified-v2"

    # Verify read reflects updated card
    read_resp2 = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{acct_id}",
        headers=headers,
    )
    assert read_resp2.status_code == 200
    assert read_resp2.json()["rate_card_version"] == "card-qualified-v2"


def test_rate_card_read_invalid_uuid(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/rate-cards/not-a-valid-uuid",
        headers=headers,
    )
    assert read_resp.status_code == 400
    assert "invalid_rate_card_id" in read_resp.json()["error"]["diagnostics"]


def test_rate_card_list_ordering(service):
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)

    # Register 3 cards: (openai, v2), (anthropic, v1), (openai, v1)
    # Expected sort order: provider ASC, version ASC, effective_from ASC
    _register_card(service, workspace_id, uuid4(), provider="openai", version="v2")
    _register_card(service, workspace_id, uuid4(), provider="anthropic", version="v1")
    _register_card(service, workspace_id, uuid4(), provider="openai", version="v1")

    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    list_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/rate-cards",
        headers=headers,
    )
    assert list_resp.status_code == 200
    cards = list_resp.json()["rate_cards"]
    keys = [(c["provider"], c["version"]) for c in cards]
    assert keys == [("anthropic", "v1"), ("openai", "v1"), ("openai", "v2")]


def test_historical_unresolved_rate_card_row_readable_and_mutation_rejected(service):
    """Verify historical pre-migration provider_accounts row with unresolved free-text

    rate_card_version remains readable unchanged, future mutation rejects with
    rate_card_missing without changing persisted bytes, and can later update to a
    qualified card.
    """
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    obs_time1 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

    # 1. Simulate pre-migration historical row seeded directly via owner role
    # with non-null free-text rate_card_version that has NO corresponding row in omp_work.rate_cards.
    legacy_version = "legacy-free-text-2025-q4"
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO omp_work.provider_accounts (
                    account_id,
                    workspace_id,
                    provider,
                    account_identity,
                    entitlement_evidence,
                    evidence_observed_at,
                    billing_mode,
                    rate_card_version,
                    observed_balance,
                    balance_provenance,
                    reset_at,
                    concurrency_limit
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    account_id,
                    workspace_id,
                    "anthropic",
                    "team-legacy-1",
                    "legacy-evidence-v1",
                    obs_time1,
                    "metered",
                    legacy_version,
                    Decimal("250.00"),
                    "provider_observed",
                    None,
                    6,
                ),
            )

    headers = {
        "Authorization": "Bearer operator-token",
        "X-OMP-Workspace-ID": str(workspace_id),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    # 2. Verify single read returns historical row unchanged
    read_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{account_id}",
        headers=headers,
    )
    assert read_resp.status_code == 200, read_resp.json()
    item = read_resp.json()
    assert item["account_id"] == str(account_id)
    assert item["provider"] == "anthropic"
    assert item["account_identity"] == "team-legacy-1"
    assert item["rate_card_version"] == legacy_version
    assert item["entitlement_evidence"] == "legacy-evidence-v1"
    assert item["concurrency_limit"] == 6

    # 3. Verify workspace list read also returns historical row unchanged
    list_resp = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts",
        headers=headers,
    )
    assert list_resp.status_code == 200, list_resp.json()
    accts = [a for a in list_resp.json()["accounts"] if a["account_id"] == str(account_id)]
    assert len(accts) == 1
    assert accts[0]["rate_card_version"] == legacy_version

    # 4. Attempt future mutation on that row with newer evidence keeping unresolved reference
    # Must be rejected with rate_card_missing
    obs_time2 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC).isoformat()
    status_mut, resp_mut = _put_account(
        service,
        workspace_id,
        account_id,
        provider="anthropic",
        account_identity="team-legacy-1",
        entitlement_evidence="newer-evidence-v2",
        evidence_observed_at=obs_time2,
        billing_mode="metered",
        rate_card_version=legacy_version,
        observed_balance="300.00",
        concurrency_limit=10,
    )
    assert status_mut == 400
    assert resp_mut["error"]["code"] == "invalid_request"
    assert "rate_card_missing" in resp_mut["error"]["diagnostics"]

    # 5. Verify persisted bytes remain completely unchanged
    # Via API:
    read_after_failed = service.client.get(
        f"/v1/workspaces/{workspace_id}/provider-accounts/{account_id}",
        headers=headers,
    )
    assert read_after_failed.status_code == 200
    after_item = read_after_failed.json()
    assert after_item["entitlement_evidence"] == "legacy-evidence-v1"
    assert after_item["concurrency_limit"] == 6
    assert after_item["rate_card_version"] == legacy_version

    # Via direct database query:
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT entitlement_evidence, concurrency_limit, rate_card_version FROM omp_work.provider_accounts WHERE account_id = %s",
                (account_id,),
            )
            row = cur.fetchone()
            assert row[0] == "legacy-evidence-v1"
            assert row[1] == 6
            assert row[2] == legacy_version

    # 6. Verify row CAN subsequently be mutated to a newly registered qualified rate card
    card_id = uuid4()
    status_card, resp_card = _register_card(
        service,
        workspace_id,
        card_id,
        provider="anthropic",
        version="qualified-replacement-v1",
        billing_modes=["metered"],
        qualification="qualified",
    )
    assert status_card == 200, resp_card

    status_upd, resp_upd = _put_account(
        service,
        workspace_id,
        account_id,
        provider="anthropic",
        account_identity="team-legacy-1",
        entitlement_evidence="newer-evidence-v2",
        evidence_observed_at=obs_time2,
        billing_mode="metered",
        rate_card_version="qualified-replacement-v1",
        observed_balance="300.00",
        concurrency_limit=10,
    )
    assert status_upd == 200, resp_upd
    assert resp_upd["result"]["status"] == "updated"
    assert resp_upd["result"]["account"]["rate_card_version"] == "qualified-replacement-v1"
    assert resp_upd["result"]["account"]["concurrency_limit"] == 10
    assert resp_upd["result"]["account"]["entitlement_evidence"] == "newer-evidence-v2"
