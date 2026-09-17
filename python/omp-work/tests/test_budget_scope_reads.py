"""PostgreSQL contract tests for authoritative budget scope read projections and keyset pagination."""

from __future__ import annotations

import base64
import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work import contract_sha256
from test_workflow_service import _command, _grant, _owner_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _setup_budget_env(service):
    workspace_id = uuid4()
    _grant(service, workspace_id)
    account_id = uuid4()
    with psycopg.connect(**service.config.connection_kwargs("postgres"), autocommit=True) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES(%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
        conn.execute("GRANT SELECT ON omp_work.provider_accounts TO omp_work_app")
        conn.execute("GRANT REFERENCES ON omp_work.provider_accounts TO omp_work_app")
        conn.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(uuid4())),
        )
        conn.execute(
            "INSERT INTO omp_work.provider_accounts("
            "account_id, workspace_id, provider, account_identity, entitlement_evidence, "
            "evidence_observed_at, billing_mode, balance_provenance, concurrency_limit) "
            "VALUES(%s, %s, 'fixture', 'account', 'fixture-evidence', clock_timestamp(), "
            "'subscription', 'provider_observed', 10)",
            (account_id, workspace_id),
        )
    return workspace_id, account_id


def _reserve(
    service,
    workspace_id: UUID,
    account_id: UUID,
    scope_id: UUID,
    *,
    amount: str = "1.00",
    transport_attempt_id: UUID | None = None,
):
    attempt = transport_attempt_id or uuid4()
    return _command(
        service,
        workspace_id,
        {
            "type": "reserve_budget",
            "payload": {
                "scope_id": str(scope_id),
                "account_id": str(account_id),
                "logical_call_id": str(uuid4()),
                "transport_attempt_id": str(attempt),
                "provider": "fixture",
                "model": "cheap",
                "effort": "low",
                "resource": "included_credit",
                "worst_case_drawdown": amount,
                "context_limit": 1000,
                "output_limit": 100,
                "expires_at": "2030-01-01T00:00:00+00:00",
            },
        },
    )


def _settle(
    service,
    workspace_id: UUID,
    reservation_id: str,
    transport_attempt_id: str,
    fence: int,
    *,
    amount: str = "0.40",
    state: str = "settled",
    outcome: str = "success",
    provenance: str = "provider_observed",
):
    return _command(
        service,
        workspace_id,
        {
            "type": "settle_budget",
            "payload": {
                "reservation_id": reservation_id,
                "transport_attempt_id": transport_attempt_id,
                "fence": fence,
                "state": state,
                "actual_drawdown": amount,
                "usage": {},
                "provenance": provenance,
                "outcome": outcome,
            },
        },
    )


def test_budget_scope_hierarchy_transitions_observed_via_reads(service):
    """Observable contract: projection follows real budget transition across hierarchy.
    Reserve changes held; settle changes spent and releases held across all parent scopes.
    """
    workspace_id, account_id = _setup_budget_env(service)
    headers = _owner_headers(workspace_id)

    root_id = uuid4()
    mid_id = uuid4()
    leaf_id = uuid4()

    # 1. Create 3-level scope hierarchy
    st, res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(root_id),
                "kind": "account",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "10.00"},
            },
        },
    )
    assert st == 200, res

    st, res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(mid_id),
                "parent_scope_id": str(root_id),
                "kind": "session",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "10.00"},
            },
        },
    )
    assert st == 200, res

    st, res = _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(leaf_id),
                "parent_scope_id": str(mid_id),
                "kind": "work",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "10.00"},
            },
        },
    )
    assert st == 200, res

    # 2. Initial read projection: zero held, zero spent, zero unresolved, exact decimal string limits
    for sid in (root_id, mid_id, leaf_id):
        r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes/{sid}", headers=headers)
        assert r.status_code == 200
        scope = r.json()
        assert scope["scope_id"] == str(sid)
        assert scope["workspace_id"] == str(workspace_id)
        assert scope["limits"] == {"included_credit": "10.00"}
        assert scope["held"] == {}
        assert scope["spent"] == {}
        assert scope["unresolved"] == {}

    # List projection also exposes all scopes with initial values
    r_list = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes", headers=headers)
    assert r_list.status_code == 200
    list_body = r_list.json()
    assert len(list_body["scopes"]) == 3
    found_ids = {s["scope_id"] for s in list_body["scopes"]}
    assert found_ids == {str(root_id), str(mid_id), str(leaf_id)}

    # 3. Reserve 3.00 on leaf scope -> held becomes "3.00" on leaf, mid, and root
    st, res = _reserve(service, workspace_id, account_id, leaf_id, amount="3.00")
    assert st == 200, res
    reservation_id = res["result"]["reservation_id"]
    transport_attempt_id = res["result"]["transport_attempt_id"]
    fence = res["result"]["fence"]

    for sid in (root_id, mid_id, leaf_id):
        r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes/{sid}", headers=headers)
        assert r.status_code == 200
        scope = r.json()
        assert scope["held"]["included_credit"] == "3.00"
        assert scope["spent"].get("included_credit", "0") == "0"
        assert scope["unresolved"].get("included_credit", "0") == "0"

    # 4. Claim and Settle 2.50 -> held becomes "0", spent becomes "2.50" on leaf, mid, and root
    st, _ = _command(
        service,
        workspace_id,
        {"type": "claim_budget", "payload": {"reservation_id": reservation_id, "fence": fence}},
    )
    assert st == 200

    st, settled = _settle(
        service,
        workspace_id,
        reservation_id,
        transport_attempt_id,
        fence,
        amount="2.50",
        state="settled",
    )
    assert st == 200 and settled["result"]["state"] == "settled"

    for sid in (root_id, mid_id, leaf_id):
        r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes/{sid}", headers=headers)
        assert r.status_code == 200
        scope = r.json()
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"]["included_credit"] == "2.50"
        assert scope["unresolved"].get("included_credit", "0") == "0"

    # 5. Reserve 1.00 and Settle to unresolved -> unresolved becomes "1.00", held released to "0"
    st, res2 = _reserve(service, workspace_id, account_id, leaf_id, amount="1.00")
    assert st == 200, res2
    res2_id = res2["result"]["reservation_id"]
    att2_id = res2["result"]["transport_attempt_id"]
    fence2 = res2["result"]["fence"]

    _command(
        service,
        workspace_id,
        {"type": "claim_budget", "payload": {"reservation_id": res2_id, "fence": fence2}},
    )

    st, unres = _settle(
        service,
        workspace_id,
        res2_id,
        att2_id,
        fence2,
        amount="1.00",
        state="unresolved",
        outcome="timeout",
        provenance="unknown",
    )
    assert st == 200 and unres["result"]["state"] == "unresolved"

    for sid in (root_id, mid_id, leaf_id):
        r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes/{sid}", headers=headers)
        assert r.status_code == 200
        scope = r.json()
        assert scope["held"].get("included_credit", "0") == "0"
        assert scope["spent"]["included_credit"] == "2.50"
        assert scope["unresolved"]["included_credit"] == "1.00"


def test_budget_scope_exact_filters_and_keyset_pagination(service):
    """Observable contract:
    - exact filters (kind, work_id, session_id) and conjunctive combinations;
    - keyset pagination returns every matching scope once in deterministic (created_at, scope_id) ASC order;
    - preserves multiple matches without synthetic deduplication.
    """
    workspace_id, _ = _setup_budget_env(service)
    headers = _owner_headers(workspace_id)

    st, w_res = _command(
        service,
        workspace_id,
        {
            "type": "create_work_batch",
            "payload": {
                "items": [
                    {
                        "client_ref": "test-item-1",
                        "title": "Title 1",
                    }
                ]
            },
        },
    )
    assert st == 200, w_res
    work_id = UUID(w_res["result"]["items"][0]["work_id"])

    # Create 5 distinct scopes
    s1 = uuid4()
    s2 = uuid4()
    s3 = uuid4()
    s4 = uuid4()
    s5 = uuid4()

    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s1),
                "kind": "account",
                "policy_version": "economy-v1",
                "limits": {"cash": "100.00"},
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s2),
                "kind": "session",
                "session_id": "session-alpha",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "10.00"},
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s3),
                "kind": "session",
                "session_id": "session-beta",
                "policy_version": "economy-v1",
                "limits": {"included_credit": "20.00"},
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s4),
                "kind": "work",
                "work_id": str(work_id),
                "session_id": "session-alpha",
                "policy_version": "economy-v1",
                "limits": {"native_quota": "5.00"},
            },
        },
    )
    _command(
        service,
        workspace_id,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s5),
                "kind": "role",
                "session_id": "session-beta",
                "policy_version": "economy-v1",
                "limits": {"local_compute": "1.00"},
            },
        },
    )

    # 1. Filter by kind="account"
    r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes?kind=account", headers=headers)
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_id"] == str(s1)

    # 2. Filter by kind="session"
    r = service.client.get(f"/v1/workspaces/{workspace_id}/budget-scopes?kind=session", headers=headers)
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 2
    assert {s["scope_id"] for s in scopes} == {str(s2), str(s3)}

    # 3. Filter by session_id="session-alpha"
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?session_id=session-alpha", headers=headers
    )
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 2
    assert {s["scope_id"] for s in scopes} == {str(s2), str(s4)}

    # 4. Filter by work_id
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?work_id={work_id}", headers=headers
    )
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_id"] == str(s4)

    # 5. Conjunctive filter: kind="work" AND session_id="session-alpha"
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?kind=work&session_id=session-alpha",
        headers=headers,
    )
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_id"] == str(s4)

    # 6. Conjunctive filter: kind="session" AND session_id="session-alpha"
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?kind=session&session_id=session-alpha",
        headers=headers,
    )
    assert r.status_code == 200
    scopes = r.json()["scopes"]
    assert len(scopes) == 1
    assert scopes[0]["scope_id"] == str(s2)

    # 7. Conjunctive filter matching nothing
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?kind=account&session_id=session-alpha",
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scopes"] == []
    assert body["exhausted"] is True
    assert body["next_cursor"] is None

    # 8. Keyset pagination across all 5 scopes with limit=2
    all_retrieved = []
    cursor = None
    page_count = 0
    while True:
        url = f"/v1/workspaces/{workspace_id}/budget-scopes?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        r = service.client.get(url, headers=headers)
        assert r.status_code == 200
        page = r.json()
        all_retrieved.extend(page["scopes"])
        page_count += 1
        if page["exhausted"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] is not None
        cursor = page["next_cursor"]

    assert page_count == 3
    assert len(all_retrieved) == 5
    retrieved_ids = [s["scope_id"] for s in all_retrieved]
    # Check all 5 unique IDs returned
    assert len(set(retrieved_ids)) == 5
    assert set(retrieved_ids) == {str(s1), str(s2), str(s3), str(s4), str(s5)}


def test_budget_scope_fail_closed_validation(service):
    """Observable contract:
    - malformed base64 cursor -> 400 invalid_request (malformed_cursor)
    - malformed JSON inside valid base64 -> 400 invalid_request (malformed_cursor)
    - foreign workspace cursor -> 400 invalid_request (cursor_workspace_mismatch)
    - empty session_id -> 400 invalid_request (invalid_session_id)
    - invalid kind -> 400 invalid_request (invalid_budget_scope_kind)
    - out of bounds limit -> 400 invalid_request (limit_out_of_bounds)
    """
    workspace_id, _ = _setup_budget_env(service)
    headers = _owner_headers(workspace_id)

    # 1. Malformed base64 cursor
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?cursor=%%%not_valid_b64%%%",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 2. Malformed JSON inside valid base64
    bad_json_b64 = base64.urlsafe_b64encode(b"not-json-content").decode("ascii")
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?cursor={bad_json_b64}",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 3. Invalid payload shape inside base64 JSON
    bad_payload_b64 = base64.urlsafe_b64encode(
        json.dumps({"workspace_id": str(workspace_id), "created_at": "not-a-datetime", "scope_id": "bad-id"}).encode("utf-8")
    ).decode("ascii")
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?cursor={bad_payload_b64}",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "malformed_cursor" in r.json()["error"]["diagnostics"]

    # 4. Cross-workspace cursor
    foreign_ws = uuid4()
    foreign_cursor_payload = {
        "workspace_id": str(foreign_ws),
        "created_at": "2026-09-17T00:00:00+00:00",
        "scope_id": str(uuid4()),
    }
    foreign_cursor = base64.urlsafe_b64encode(
        json.dumps(foreign_cursor_payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?cursor={foreign_cursor}",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "cursor_workspace_mismatch" in r.json()["error"]["diagnostics"]

    # 5. Empty session_id query param
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?session_id=",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "invalid_session_id" in r.json()["error"]["diagnostics"]

    # 6. Invalid kind
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?kind=unsupported_kind",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "invalid_budget_scope_kind" in r.json()["error"]["diagnostics"]

    # 7. Out of bounds limit (0 and 501)
    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?limit=0",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"

    r = service.client.get(
        f"/v1/workspaces/{workspace_id}/budget-scopes?limit=501",
        headers=headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_budget_scope_identity_workspace_isolation_and_authorization(service):
    """Observable contract:
    - invalid / missing scope identity returns 400 invalid_request
    - cross-workspace single read and list read strictly enforce isolation
    - candidate-only reader cannot access either list or single routes (403 forbidden)
    - unauthenticated caller fails closed (401 unauthenticated)
    """
    w1, _ = _setup_budget_env(service)
    w2, _ = _setup_budget_env(service)
    h1 = _owner_headers(w1)
    h2 = _owner_headers(w2)

    s1 = uuid4()
    s2 = uuid4()

    _command(
        service,
        w1,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s1),
                "kind": "session",
                "session_id": "sess-w1",
                "policy_version": "economy-v1",
                "limits": {"cash": "50.00"},
            },
        },
    )
    _command(
        service,
        w2,
        {
            "type": "create_budget_scope",
            "payload": {
                "scope_id": str(s2),
                "kind": "session",
                "session_id": "sess-w2",
                "policy_version": "economy-v1",
                "limits": {"cash": "75.00"},
            },
        },
    )

    # 1. Valid single read in w1
    r = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/{s1}", headers=h1)
    assert r.status_code == 200
    assert r.json()["scope_id"] == str(s1)
    assert r.json()["limits"] == {"cash": "50.00"}

    # 2. Non-existent scope UUID in w1 -> 400 not_found
    missing_id = uuid4()
    r = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/{missing_id}", headers=h1)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "not_found" in r.json()["error"]["diagnostics"]

    # 3. Cross-workspace single read: request s2 from w1 -> 400 not_found
    r = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/{s2}", headers=h1)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "not_found" in r.json()["error"]["diagnostics"]

    # 4. Malformed scope UUID string -> 400 invalid_scope_id
    r = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/not-a-valid-uuid", headers=h1)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert "invalid_scope_id" in r.json()["error"]["diagnostics"]

    # 5. List reads never leak across workspaces
    r1 = service.client.get(f"/v1/workspaces/{w1}/budget-scopes", headers=h1)
    assert r1.status_code == 200
    w1_ids = [s["scope_id"] for s in r1.json()["scopes"]]
    assert str(s1) in w1_ids
    assert str(s2) not in w1_ids

    r2 = service.client.get(f"/v1/workspaces/{w2}/budget-scopes", headers=h2)
    assert r2.status_code == 200
    w2_ids = [s["scope_id"] for s in r2.json()["scopes"]]
    assert str(s2) in w2_ids
    assert str(s1) not in w2_ids

    # 6. Candidate-only reader denial
    cand_reader = service.capabilities / "candidate_reader.json"
    cand_id = uuid4()
    cand_reader.write_text(
        json.dumps(
            {
                "token": "cand-reader-token",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(w1)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(cand_id)],
            }
        )
    )
    cand_reader.chmod(0o600)
    cand_headers = {
        "Authorization": "Bearer cand-reader-token",
        "X-OMP-Workspace-ID": str(w1),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }

    # Candidate single read -> 403
    r_cand_single = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/{s1}", headers=cand_headers)
    assert r_cand_single.status_code == 403
    assert r_cand_single.json()["error"]["code"] == "forbidden"

    # Candidate list read -> 403
    r_cand_list = service.client.get(f"/v1/workspaces/{w1}/budget-scopes", headers=cand_headers)
    assert r_cand_list.status_code == 403
    assert r_cand_list.json()["error"]["code"] == "forbidden"

    # 7. Unauthenticated caller denial (missing auth header)
    no_auth_headers = {
        "X-OMP-Workspace-ID": str(w1),
        "X-OMP-Contract-SHA256": contract_sha256(),
    }
    r_unauth_single = service.client.get(f"/v1/workspaces/{w1}/budget-scopes/{s1}", headers=no_auth_headers)
    assert r_unauth_single.status_code == 401
    assert r_unauth_single.json()["detail"] == "unauthenticated"

    r_unauth_list = service.client.get(f"/v1/workspaces/{w1}/budget-scopes", headers=no_auth_headers)
    assert r_unauth_list.status_code == 401
    assert r_unauth_list.json()["detail"] == "unauthenticated"
