"""PostgreSQL integration tests for stage preflight reconciliation authority (OMP-233)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.v1.models import StagePreflightDisposition, StagePreflightOutcome
from test_workflow_service import (
    _command,
    _create,
    _grant,
    _owner_headers,
)

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
    provider: str = "gemini",
    account_identity: str = "gemini-proj-123",
    token: str = "operator-token",
) -> tuple[int, dict]:
    obs_time = datetime.now(UTC).isoformat()
    return _command(
        service,
        workspace_id,
        {
            "type": "put_provider_account",
            "payload": {
                "account_id": str(account_id),
                "provider": provider,
                "account_identity": account_identity,
                "entitlement_evidence": "active-subscription",
                "evidence_observed_at": obs_time,
                "billing_mode": "metered",
                "rate_card_version": "2026-q3",
                "observed_balance": "500.00",
                "balance_provenance": "provider_observed",
                "concurrency_limit": 4,
            },
        },
        token=token,
    )


def _exec_command(
    service,
    workspace_id: UUID,
    command: dict,
    *,
    token: str = "owner-token",
    operation_id: UUID | str | None = None,
    correlation_id: UUID | str | None = None,
) -> tuple[int, dict]:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(operation_id or uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(correlation_id or uuid4()),
        "command": command,
    }
    response = service.client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id) | {"Authorization": f"Bearer {token}"},
        json=envelope,
    )
    return response.status_code, response.json()


def _setup_dispatched_intent(
    service,
    workspace_id: UUID,
    *,
    provider: str = "gemini",
    owner_correlation_id: UUID | None = None,
) -> tuple[dict, UUID, str]:
    item = _create(service, workspace_id, f"test item {uuid4()}")
    work_id = item["work_id"]
    owner_id = owner_correlation_id or uuid4()
    begin_op_id = uuid4()

    begin_payload = {
        "work_id": str(work_id),
        "role": "implement",
        "tool_call_id": f"call-{uuid4()}",
        "task_sha256": "1" * 64,
        "probe_sha256": "2" * 64,
        "ordinal": 0,
        "requested_selector": f"{provider}:test-model",
        "requested_provider": provider,
        "requested_model": "test-model",
        "requested_api": "google-genai" if provider == "gemini" else "chat",
        "requested_effort": "medium",
        "requested_wire_model": "test-model-preview",
        "is_fallback": False,
    }

    status, begin_res = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        token="owner-token",
        operation_id=begin_op_id,
        correlation_id=owner_id,
    )
    assert status == 200, begin_res
    transport_attempt_id = UUID(begin_res["result"]["intent"]["transport_attempt_id"])
    logical_sha256 = begin_res["result"]["intent"]["logical_sha256"]

    # Admit intent to dispatched
    admit_status, admit_res = _exec_command(
        service,
        workspace_id,
        {
            "type": "admit_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
            },
        },
        token="owner-token",
        correlation_id=owner_id,
    )
    assert admit_status == 200, admit_res
    assert admit_res["result"]["intent"]["status"] == "dispatched"

    return begin_payload, transport_attempt_id, logical_sha256


def test_reconciliation_requires_work_operate_scope(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(service, workspace_id)

    reconcile_payload = {
        "transport_attempt_id": str(transport_attempt_id),
        "logical_sha256": logical_sha256,
        "account_id": str(account_id),
        "observation_id": str(uuid4()),
        "observed_at": datetime.now(UTC).isoformat(),
        "evidence_sha256": "3" * 64,
        "disposition": "indeterminate",
    }

    # owner-token has work.execute, work.mutate, etc., but lacks work.operate -> 403 forbidden
    status, body = _exec_command(
        service,
        workspace_id,
        {"type": "reconcile_stage_preflight", "payload": reconcile_payload},
        token="owner-token",
    )
    assert status == 403, body
    assert body["error"]["code"] == "forbidden"

    # operator-token has work.operate -> 200 OK
    status, body = _exec_command(
        service,
        workspace_id,
        {"type": "reconcile_stage_preflight", "payload": reconcile_payload},
        token="operator-token",
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["reconciliation"]["disposition"] == "indeterminate"


def test_reconciliation_workspace_mismatch_and_unknown_account_and_provider_mismatch(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(service, workspace_id, provider="gemini")

    obs_time = datetime.now(UTC).isoformat()

    # Unknown account -> 400 invalid_request
    unknown_acc_id = uuid4()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(unknown_acc_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "3" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 400, body
    assert "provider_account_unknown" in body["error"]["diagnostics"]

    # Account provider mismatch: account is openai, intent requested gemini -> 400 invalid_request
    openai_acc_id = uuid4()
    _put_account(service, workspace_id, openai_acc_id, provider="openai")
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(openai_acc_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "3" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 400, body
    assert "account_provider_mismatch" in body["error"]["diagnostics"]

    # Logical sha256 mismatch -> 409 stale_evidence
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": "f" * 64,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "3" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"


def test_reconciliation_observation_timestamp_negatives(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(service, workspace_id, provider="gemini")

    # Observed at far in future -> 400 invalid_request (observation_in_future)
    future_time = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": future_time,
                "evidence_sha256": "3" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 400, body
    assert "observation_in_future" in body["error"]["diagnostics"]

    # Observed at preceding intent dispatch -> 400 invalid_request (observation_precedes_dispatch)
    past_time = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": past_time,
                "evidence_sha256": "3" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 400, body
    assert "observation_precedes_dispatch" in body["error"]["diagnostics"]


def test_reconciliation_timezoneless_observed_at_rejected_by_boundary(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(
        service, workspace_id, provider="gemini"
    )

    # Timezone-less ISO string (no 'Z' or '+00:00' offset)
    naive_iso_time = "2026-09-17T12:05:00"
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": naive_iso_time,
                "evidence_sha256": "3" * 64,
                "disposition": "completed",
                "provider_request_id": "prov-naive-1",
                "requests": 1,
            },
        },
        token="operator-token",
    )
    # Rejected at envelope/payload schema validation boundary without entering store mutation
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert any("timezone" in diag.lower() for diag in body["error"]["diagnostics"])

    # Verify no store mutation occurred: intent remains dispatched and no reconciliation was persisted
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM omp_work.stage_preflight_reconciliations WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            assert cur.fetchone()[0] == 0

            cur.execute(
                "SELECT status FROM omp_work.stage_preflight_intents WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            assert cur.fetchone()[0] == "dispatched"


def test_reconciliation_indeterminate_disposition(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    begin_payload, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(
        service, workspace_id, provider="gemini"
    )

    obs_time = datetime.now(UTC).isoformat()
    observation_id = uuid4()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(observation_id),
                "observed_at": obs_time,
                "evidence_sha256": "4" * 64,
                "disposition": "indeterminate",
            },
        },
        token="operator-token",
    )
    assert status == 200, body
    result = body["result"]
    assert result["status"] == "applied"
    assert result["preflight"] is None
    assert result["intent"]["status"] == "dispatched"
    assert result["reconciliation"]["disposition"] == "indeterminate"
    assert result["reconciliation"]["observation_id"] == str(observation_id)

    # Verify DB: reconciliation row exists, but stage_preflights has NO row
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM omp_work.stage_preflight_reconciliations WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM omp_work.stage_preflights WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            assert cur.fetchone()[0] == 0

    # Intent is still dispatched: beginning a sibling route in same group is blocked
    sibling_payload = dict(begin_payload)
    sibling_payload["ordinal"] = 1
    sibling_payload["requested_wire_model"] = "sibling-preview"
    sibling_status, sibling_res = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": sibling_payload},
        token="owner-token",
    )
    assert sibling_status == 409, sibling_res
    assert "preflight_group_undispatched_route_exists" in sibling_res["error"]["diagnostics"] or sibling_res["error"]["code"] == "preflight_intent_active"


def test_reconciliation_completed_disposition(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    begin_payload, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(
        service, workspace_id, provider="gemini"
    )

    obs_time = datetime.now(UTC).isoformat()
    observation_id = uuid4()
    usage = {
        "input": 200,
        "output": 100,
        "cacheRead": 20,
        "cacheWrite": 0,
        "totalTokens": 300,
        "contextTokens": 2000,
        "premiumRequests": 1,
        "reasoningTokens": 0,
        "orchestration": {"input": 200, "output": 100, "cacheRead": 20},
    }

    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(observation_id),
                "observed_at": obs_time,
                "evidence_sha256": "5" * 64,
                "disposition": "completed",
                "provider_request_id": "prov-req-999",
                "requests": 1,
                "usage": usage,
                "stop_reason": "stop",
            },
        },
        token="operator-token",
    )
    assert status == 200, body
    result = body["result"]
    assert result["status"] == "applied"
    assert result["intent"]["status"] == "settled"
    assert result["intent"]["settled_at"] is not None
    assert result["preflight"]["outcome"] == "selected"
    assert result["preflight"]["provider_request_id"] == "prov-req-999"
    assert result["preflight"]["transport_attempt_id"] == str(transport_attempt_id)
    assert result["reconciliation"]["disposition"] == "completed"

    # Verify DB: terminal stage_preflights row exists and matches
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, provider_request_id FROM omp_work.stage_preflights WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "selected"
            assert row[1] == "prov-req-999"

    # Existing begin_stage_preflight replay returns terminal evidence
    status, replay_body = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        token="owner-token",
    )
    assert status == 200, replay_body
    assert replay_body["result"]["status"] == "replayed"
    assert replay_body["result"]["preflight"]["outcome"] == "selected"
    assert replay_body["result"]["preflight"]["transport_attempt_id"] == str(transport_attempt_id)


def test_reconciliation_failed_and_confirmed_absent_disposition(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    # 1. Failed disposition
    _, t_id_failed, hash_failed = _setup_dispatched_intent(service, workspace_id, provider="gemini")
    obs_time = datetime.now(UTC).isoformat()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(t_id_failed),
                "logical_sha256": hash_failed,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "6" * 64,
                "disposition": "failed",
                "provider_request_id": "prov-failed-123",
                "requests": 1,
                "error": "provider rate limit exceeded",
                "stop_reason": "rate_limit",
            },
        },
        token="operator-token",
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["intent"]["status"] == "settled"
    assert body["result"]["preflight"]["outcome"] == "failed"
    assert body["result"]["preflight"]["error"] == "provider rate limit exceeded"

    # 2. Confirmed absent disposition
    _, t_id_absent, hash_absent = _setup_dispatched_intent(service, workspace_id, provider="gemini")
    obs_time = datetime.now(UTC).isoformat()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(t_id_absent),
                "logical_sha256": hash_absent,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "7" * 64,
                "disposition": "confirmed_absent",
                "requests": 0,
                "error": "no provider request record found in log window",
            },
        },
        token="operator-token",
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["intent"]["status"] == "settled"
    assert body["result"]["preflight"]["outcome"] == "failed"
    assert body["result"]["preflight"]["provider_request_id"] is None
    assert body["result"]["preflight"]["requests"] == 0
    assert body["result"]["preflight"]["usage"] is None
    assert body["result"]["preflight"]["error"] == "no provider request record found in log window"


def test_reconciliation_idempotent_replay_and_conflict(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(service, workspace_id, provider="gemini")

    obs_time = datetime.now(UTC).isoformat()
    obs_id = uuid4()
    rec_payload = {
        "transport_attempt_id": str(transport_attempt_id),
        "logical_sha256": logical_sha256,
        "account_id": str(account_id),
        "observation_id": str(obs_id),
        "observed_at": obs_time,
        "evidence_sha256": "8" * 64,
        "disposition": "completed",
        "provider_request_id": "prov-req-replay",
        "requests": 1,
        "stop_reason": "stop",
    }

    status, first_res = _exec_command(
        service,
        workspace_id,
        {"type": "reconcile_stage_preflight", "payload": rec_payload},
        token="operator-token",
    )
    assert status == 200, first_res
    assert first_res["result"]["status"] == "applied"

    # Replay exact same payload -> status: "replayed"
    status, replay_res = _exec_command(
        service,
        workspace_id,
        {"type": "reconcile_stage_preflight", "payload": rec_payload},
        token="operator-token",
    )
    assert status == 200, replay_res
    assert replay_res["result"]["status"] == "replayed"
    assert replay_res["result"]["reconciliation"]["reconciliation_id"] == first_res["result"]["reconciliation"]["reconciliation_id"]

    # Conflicting reconciliation on already settled intent -> status: "refused"
    conflicting_payload = dict(rec_payload)
    conflicting_payload["observation_id"] = str(uuid4())
    conflicting_payload["disposition"] = "failed"
    conflicting_payload["error"] = "new error"
    status, conflict_res = _exec_command(
        service,
        workspace_id,
        {"type": "reconcile_stage_preflight", "payload": conflicting_payload},
        token="operator-token",
    )
    assert status == 200, conflict_res
    assert conflict_res["result"]["status"] == "refused"
    assert conflict_res["result"]["reason"] == "preflight_intent_already_settled"


def test_concurrency_race_between_record_and_reconcile(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    owner_id = uuid4()
    begin_payload, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(
        service, workspace_id, provider="gemini", owner_correlation_id=owner_id
    )

    # 1. Reconciliation wins first: completed
    obs_time = datetime.now(UTC).isoformat()
    status, rec_res = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": obs_time,
                "evidence_sha256": "9" * 64,
                "disposition": "completed",
                "provider_request_id": "prov-winner",
                "requests": 1,
            },
        },
        token="operator-token",
    )
    assert status == 200, rec_res
    assert rec_res["result"]["status"] == "applied"

    # Ordinary record_stage_preflight by the dispatch owner now arrives
    record_payload = dict(begin_payload)
    record_payload["transport_attempt_id"] = str(transport_attempt_id)
    record_payload["outcome"] = "selected"
    record_payload["provider_request_id"] = "prov-late-record"
    record_payload["requests"] = 1

    status, rec_late_res = _exec_command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": record_payload},
        token="owner-token",
        correlation_id=owner_id,
    )
    # Ordinary record must receive a clean refusal, not overwrite the winner
    assert status == 200, rec_late_res
    assert rec_late_res["result"]["status"] == "refused"
    assert rec_late_res["result"]["preflight"]["provider_request_id"] == "prov-winner"

    # 2. Reverse: Ordinary record wins first on a new intent, then reconciliation arrives
    owner_id_2 = uuid4()
    begin_payload_2, t_id_2, hash_2 = _setup_dispatched_intent(
        service, workspace_id, provider="gemini", owner_correlation_id=owner_id_2
    )
    record_payload_2 = dict(begin_payload_2)
    record_payload_2["transport_attempt_id"] = str(t_id_2)
    record_payload_2["outcome"] = "selected"
    record_payload_2["provider_request_id"] = "prov-early-record"
    record_payload_2["requests"] = 1

    status, early_record_res = _exec_command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": record_payload_2},
        token="owner-token",
        correlation_id=owner_id_2,
    )
    assert status == 200, early_record_res
    assert early_record_res["result"]["status"] == "applied"

    # Reconciliation now arrives for t_id_2
    status, late_reconcile_res = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(t_id_2),
                "logical_sha256": hash_2,
                "account_id": str(account_id),
                "observation_id": str(uuid4()),
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence_sha256": "a" * 64,
                "disposition": "completed",
                "provider_request_id": "prov-conflicting-reconcile",
                "requests": 1,
            },
        },
        token="operator-token",
    )
    assert status == 200, late_reconcile_res
    assert late_reconcile_res["result"]["status"] == "refused"
    assert late_reconcile_res["result"]["reason"] == "preflight_intent_already_settled"


def test_table_privilege_denial_for_direct_mutation(service) -> None:
    workspace_id = uuid4()
    _setup_operator(service, workspace_id)
    account_id = uuid4()
    _put_account(service, workspace_id, account_id, provider="gemini")

    _, transport_attempt_id, logical_sha256 = _setup_dispatched_intent(service, workspace_id, provider="gemini")

    obs_time = datetime.now(UTC).isoformat()
    obs_id = uuid4()
    status, body = _exec_command(
        service,
        workspace_id,
        {
            "type": "reconcile_stage_preflight",
            "payload": {
                "transport_attempt_id": str(transport_attempt_id),
                "logical_sha256": logical_sha256,
                "account_id": str(account_id),
                "observation_id": str(obs_id),
                "observed_at": obs_time,
                "evidence_sha256": "b" * 64,
                "disposition": "completed",
                "provider_request_id": "prov-priv-test",
                "requests": 1,
            },
        },
        token="operator-token",
    )
    assert status == 200, body

    # Direct UPDATE, DELETE, TRUNCATE with omp_work_app role MUST fail with permission denied
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE omp_work.stage_preflight_reconciliations SET disposition='failed' WHERE workspace_id=%s",
                    (workspace_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM omp_work.stage_preflight_reconciliations WHERE workspace_id=%s",
                    (workspace_id,),
                )
        conn.rollback()

        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE omp_work.stage_preflight_reconciliations")
        conn.rollback()
