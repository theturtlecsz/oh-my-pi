"""Disposable PostgreSQL contract tests for stage preflight accounting (OMP-233)."""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work import contract_sha256
from test_workflow_service import (
    _command,
    _create,
    _execution_grant_audited_attempt,
    _grant,
    _owner_headers,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


def _preflight_payload(
    work_id: str | UUID,
    transport_attempt_id: str | UUID | None = None,
    **overrides,
) -> dict:
    base = {
        "work_id": str(work_id),
        "role": "implement",
        "tool_call_id": f"tool-{uuid4()}",
        "task_sha256": "a" * 64,
        "probe_sha256": "b" * 64,
        "transport_attempt_id": str(transport_attempt_id or uuid4()),
        "ordinal": 0,
        "requested_selector": "gemini:gemini-3.8-flash",
        "requested_provider": "gemini",
        "requested_model": "gemini-3.8-flash",
        "requested_api": "google-genai",
        "requested_effort": "medium",
        "requested_wire_model": "gemini-3.8-flash-preview",
        "is_fallback": False,
        "outcome": "selected",
        "stop_reason": "stop",
        "requests": 1,
        "usage": {
            "input": 100,
            "output": 50,
            "cacheRead": 10,
            "cacheWrite": 20,
            "totalTokens": 150,
            "contextTokens": 1000,
            "premiumRequests": 1,
            "reasoningTokens": 0,
            "orchestration": {"input": 100, "output": 50, "cacheRead": 10},
            "cttl": {"ephemeral5m": 5, "ephemeral1h": 10},
            "server": {"webSearch": 1, "webFetch": 2},
        },
        "provider_request_id": "req-123",
    }
    base.update(overrides)
    return base


def test_record_stage_preflight_applied_and_workflow_readback(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "preflight item")
    work_id = item["work_id"]

    transport_attempt_id = str(uuid4())
    payload = _preflight_payload(work_id, transport_attempt_id=transport_attempt_id)

    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": payload},
    )
    assert status == 200, body
    result = body["result"]
    assert result["type"] == "record_stage_preflight"
    assert result["status"] == "applied"
    preflight = result["preflight"]
    assert preflight is not None
    assert preflight["workspace_id"] == str(workspace_id)
    assert preflight["work_id"] == str(work_id)
    assert preflight["transport_attempt_id"] == transport_attempt_id
    assert preflight["role"] == "implement"
    assert preflight["outcome"] == "selected"
    assert preflight["stop_reason"] == "stop"
    assert preflight["requests"] == 1
    assert preflight["usage"]["input"] == 100
    assert preflight["usage"]["orchestration"]["input"] == 100
    assert preflight["usage"]["cttl"]["ephemeral5m"] == 5
    assert preflight["usage"]["server"]["webSearch"] == 1
    assert preflight["provider_request_id"] == "req-123"
    assert preflight["observed_at"] is not None

    # Readback via workflow API
    wf_resp = service.client.get(
        f"/v1/work-items/{item['key']}/workflow",
        headers=_owner_headers(workspace_id),
    )
    assert wf_resp.status_code == 200, wf_resp.text
    wf = wf_resp.json()
    assert "stage_preflights" in wf
    assert len(wf["stage_preflights"]) == 1
    readback = wf["stage_preflights"][0]
    assert readback["preflight_id"] == preflight["preflight_id"]
    assert readback["transport_attempt_id"] == transport_attempt_id
    assert readback["usage"] == preflight["usage"]
    assert readback["task_sha256"] == payload["task_sha256"]
    assert readback["probe_sha256"] == payload["probe_sha256"]


def test_record_stage_preflight_exact_replay_and_conflict_and_distinct_transport(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "replay item")
    work_id = item["work_id"]

    transport_attempt_id = str(uuid4())
    payload = _preflight_payload(work_id, transport_attempt_id=transport_attempt_id)

    # First attempt: applied
    status, body1 = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": payload},
    )
    assert status == 200, body1
    assert body1["result"]["status"] == "applied"
    preflight_id_1 = body1["result"]["preflight"]["preflight_id"]

    # Exact replay with same transport_attempt_id
    status, body2 = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": payload},
    )
    assert status == 200, body2
    assert body2["result"]["status"] == "replayed"
    assert body2["result"]["preflight"]["preflight_id"] == preflight_id_1

    # Workflow readback shows only 1 row
    wf = service.client.get(
        f"/v1/work-items/{item['key']}/workflow",
        headers=_owner_headers(workspace_id),
    ).json()
    assert len(wf["stage_preflights"]) == 1

    # Conflicting replay: same transport_attempt_id, different task_sha256
    conflict_payload = dict(payload)
    conflict_payload["task_sha256"] = "c" * 64
    status, conflict_body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": conflict_payload},
    )
    assert status == 409, conflict_body
    assert conflict_body["error"]["code"] == "idempotency_conflict"

    # Same probe hash and task hash, but NEW transport_attempt_id creates second row
    new_transport_payload = dict(payload)
    new_transport_payload["transport_attempt_id"] = str(uuid4())
    status, body3 = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": new_transport_payload},
    )
    assert status == 200, body3
    assert body3["result"]["status"] == "applied"
    preflight_id_2 = body3["result"]["preflight"]["preflight_id"]
    assert preflight_id_2 != preflight_id_1

    # Workflow readback now has 2 preflights
    wf = service.client.get(
        f"/v1/work-items/{item['key']}/workflow",
        headers=_owner_headers(workspace_id),
    ).json()
    assert len(wf["stage_preflights"]) == 2
    preflight_ids = {p["preflight_id"] for p in wf["stage_preflights"]}
    assert preflight_ids == {preflight_id_1, preflight_id_2}


def test_record_stage_preflight_usage_validation(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "usage validation item")
    work_id = item["work_id"]

    # 1. Unknown null roundtrip (usage=None on failed preflight)
    null_usage_payload = _preflight_payload(
        work_id,
        outcome="failed",
        error="connection timeout",
        usage=None,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": null_usage_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["preflight"]["usage"] is None

    # 2. Valid zeros roundtrip
    zero_usage_payload = _preflight_payload(
        work_id,
        outcome="selected",
        usage={
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "orchestration": {"input": 0, "output": 0, "cacheRead": 0},
        },
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": zero_usage_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["preflight"]["usage"]["input"] == 0
    assert body["result"]["preflight"]["usage"]["orchestration"]["input"] == 0

    # 3. Corrupt / invalid usages reject with 400
    invalid_cases = [
        # empty dict {}
        {},
        # forbidden extra key 'cost'
        {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": "0.05",
        },
        # negative counter
        {
            "input": -1,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
        },
        # fractional counter
        {
            "input": 1.5,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
        },
        # boolean counter
        {
            "input": True,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
        },
        # string counter
        {
            "input": "100",
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
        },
        # corrupt nested empty orchestration
        {
            "input": 10,
            "output": 10,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 20,
            "orchestration": {},
        },
    ]

    for bad_usage in invalid_cases:
        bad_payload = _preflight_payload(work_id, usage=bad_usage)
        status, body = _command(
            service,
            workspace_id,
            {"type": "record_stage_preflight", "payload": bad_payload},
        )
        assert status == 400, f"Expected 400 for bad usage: {bad_usage!r}, got {status}: {body}"


def test_record_stage_preflight_identity_binding_and_no_launch_side_effects(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    grant_id, work_id, revision_id, candidate_id, attempt_id, _push_id, _judge, item = (
        _execution_grant_audited_attempt(service, workspace_id, "identity binding item")
    )
    other_item = _create(service, workspace_id, "other work item")

    # Matching identities succeed
    valid_bound_payload = _preflight_payload(
        work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        attempt_id=attempt_id,
        grant_id=grant_id,
        session_id="session-1",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": valid_bound_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    pf = body["result"]["preflight"]
    assert pf["revision_id"] == revision_id
    assert pf["candidate_id"] == candidate_id
    assert pf["attempt_id"] == attempt_id
    assert pf["grant_id"] == grant_id
    assert pf["session_id"] == "session-1"

    # Mismatched identities: revision_id from first work attached to other work item
    mismatched_payload = _preflight_payload(
        other_item["work_id"],
        revision_id=revision_id,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": mismatched_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"

    # Null identities succeed
    null_identities_payload = _preflight_payload(
        work_id,
        revision_id=None,
        candidate_id=None,
        attempt_id=None,
        grant_id=None,
        session_id=None,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": null_identities_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"

    # Stage-launch count unchanged: recording preflights does NOT create stage launches
    wf = service.client.get(
        f"/v1/work-items/{item['key']}/workflow",
        headers=_owner_headers(workspace_id),
    ).json()
    assert len(wf["stage_launches"]) == 0
    assert len(wf["stage_preflights"]) == 2


def test_record_stage_preflight_outcome_and_hash_invariants(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "invariants item")
    work_id = item["work_id"]

    # outcome='selected' cannot have error
    bad_selected = _preflight_payload(work_id, outcome="selected", error="failed unexpected")
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_selected},
    )
    assert status == 400, body

    # outcome='failed' requires non-empty error
    bad_failed_none = _preflight_payload(work_id, outcome="failed", error=None)
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_failed_none},
    )
    assert status == 400, body

    bad_failed_empty = _preflight_payload(work_id, outcome="failed", error="   ")
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_failed_empty},
    )
    assert status == 400, body

    # outcome='cancelled' requires non-empty error
    bad_cancelled_none = _preflight_payload(work_id, outcome="cancelled", error=None)
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_cancelled_none},
    )
    assert status == 400, body

    # outcome='cancelled' with valid error succeeds
    valid_cancelled = _preflight_payload(
        work_id,
        outcome="cancelled",
        error="user aborted launch",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": valid_cancelled},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["preflight"]["outcome"] == "cancelled"

    # task_sha256 not 64 hex chars rejects 400
    bad_task_hash = _preflight_payload(work_id, task_sha256="not-valid-hash")
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_task_hash},
    )
    assert status == 400, body

    # probe_sha256 uppercase rejects 400
    bad_probe_hash = _preflight_payload(work_id, probe_sha256="A" * 64)
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": bad_probe_hash},
    )
    assert status == 400, body


def test_record_stage_preflight_workspace_isolation_and_table_privilege_denials(service) -> None:
    ws_a = uuid4()
    ws_b = uuid4()
    _grant(service, ws_a)
    _grant(service, ws_b)

    item_a = _create(service, ws_a, "workspace A item")
    item_b = _create(service, ws_b, "workspace B item")

    payload_a = _preflight_payload(item_a["work_id"])
    status, body = _command(
        service,
        ws_a,
        {"type": "record_stage_preflight", "payload": payload_a},
    )
    assert status == 200, body

    # Preflight for workspace A is not visible in workspace B workflow
    wf_b = service.client.get(
        f"/v1/work-items/{item_b['key']}/workflow",
        headers=_owner_headers(ws_b),
    ).json()
    assert len(wf_b["stage_preflights"]) == 0

    # Direct DB queries as app role verify RLS isolation and privilege denials
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app"), autocommit=True) as conn:
        with conn.cursor() as cur:
            # Set workspace B: RLS filters out workspace A rows
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws_b), str(uuid4())),
            )
            cur.execute("SELECT count(*) FROM omp_work.stage_preflights")
            assert cur.fetchone()[0] == 0

            # Set workspace A: RLS sees workspace A row
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(ws_a), str(uuid4())),
            )
            cur.execute("SELECT count(*) FROM omp_work.stage_preflights")
            assert cur.fetchone()[0] == 1

            # Direct UPDATE denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("UPDATE omp_work.stage_preflights SET requests = 99")

            # Direct DELETE denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM omp_work.stage_preflights")

            # Direct TRUNCATE denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE omp_work.stage_preflights")


def test_record_stage_preflight_scope_denial(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "scope test item")

    # Create token with only work.candidate.read (lacks work.execute)
    reader = service.capabilities / "candidate-reader.json"
    reader.write_text(
        json.dumps(
            {
                "token": "candidate-reader-token",
                "actor_id": str(uuid4()),
                "actor_kind": "task-agent",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.candidate.read"],
                "candidate_ids": [str(uuid4())],
            }
        )
    )
    reader.chmod(0o600)

    payload = _preflight_payload(item["work_id"])
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": payload},
        token="candidate-reader-token",
    )
    assert status == 403, body
    assert body["error"]["code"] == "forbidden"


def test_stage_preflight_and_launch_grant_inactive_precedence(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    grant_id, work_id, revision_id, candidate_id, attempt_id, _push_id, _judge, item = (
        _execution_grant_audited_attempt(service, workspace_id, "grant precedence item")
    )
    other_item = _create(service, workspace_id, "unrelated item")

    # Set grant state to paused (inactive)
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
                (str(workspace_id), str(uuid4())),
            )
            cur.execute(
                "UPDATE omp_work.execution_grants SET state = 'paused', paused_at = clock_timestamp() WHERE workspace_id = %s AND grant_id = %s",
                (workspace_id, grant_id),
            )

    # 1. reserve_stage_launch with inactive grant AND unrelated work item (not in grant)
    # MUST raise execution_grant_inactive before missing/mismatched grant-item check (stale_evidence)
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "reserve_stage_launch",
            "payload": {
                "work_id": other_item["work_id"],
                "role": "audit",
                "request_sha256": "c" * 64,
                "tool_call_id": f"tc-{uuid4()}",
                "task_sha256": "d" * 64,
                "prepared_context_sha256": "e" * 64,
                "grant_id": grant_id,
                "requested_selector": "gemini:gemini-3.8-flash",
                "requested_provider": "gemini",
                "requested_model": "gemini-3.8-flash",
                "requested_api": "google-genai",
                "requested_effort": "medium",
                "requested_wire_model": "gemini-3.8-flash-preview",
            },
        },
    )
    assert status == 409, body
    assert body["error"]["code"] == "execution_grant_inactive"

    # 2. record_stage_preflight with inactive grant succeeds: skips active-state check
    preflight_payload = _preflight_payload(
        work_id,
        grant_id=grant_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        attempt_id=attempt_id,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": preflight_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    assert body["result"]["preflight"]["grant_id"] == grant_id
