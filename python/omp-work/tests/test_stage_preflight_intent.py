"""PostgreSQL integration tests for stage preflight intent lifecycle (OMP-233)."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.operations import database as database_module
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap, migrate
from pg_native import native_postgres
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


def _base_preflight_payload(
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


def _begin_payload_from(record_payload: dict) -> dict:
    payload = {
        "work_id": record_payload["work_id"],
        "role": record_payload["role"],
        "tool_call_id": record_payload["tool_call_id"],
        "task_sha256": record_payload["task_sha256"],
        "probe_sha256": record_payload["probe_sha256"],
        "ordinal": record_payload["ordinal"],
        "requested_selector": record_payload["requested_selector"],
        "requested_provider": record_payload["requested_provider"],
        "requested_model": record_payload["requested_model"],
        "requested_api": record_payload["requested_api"],
        "requested_effort": record_payload["requested_effort"],
        "requested_wire_model": record_payload["requested_wire_model"],
        "is_fallback": record_payload["is_fallback"],
    }
    for k in ("revision_id", "candidate_id", "grant_id", "attempt_id"):
        if k in record_payload and record_payload[k] is not None:
            payload[k] = record_payload[k]
    return payload


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


def test_stage_preflight_intent_begin_applied_and_transport_id_parity(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "preflight item")
    work_id = item["work_id"]

    rec_payload = _base_preflight_payload(work_id)
    begin_payload = _begin_payload_from(rec_payload)

    operation_id = uuid4()
    correlation_id = uuid4()

    status, body = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        operation_id=operation_id,
        correlation_id=correlation_id,
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"
    intent = body["result"]["intent"]
    assert intent["status"] == "begun"
    # Service mints transport ID from operation_id
    assert intent["transport_attempt_id"] == str(operation_id)
    # Service records correlation_id as host owner
    assert intent["host_owner_id"] == str(correlation_id)
    assert body["result"]["preflight"] is None

    # Verify DB row
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, settled_at, host_owner_id, transport_attempt_id FROM omp_work.stage_preflight_intents WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, operation_id),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[0] == "begun"
            assert row[1] is None
            assert str(row[2]) == str(correlation_id)
            assert str(row[3]) == str(operation_id)


def test_stage_preflight_intent_begin_rejects_session_id_wire_boundary(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "preflight item")
    work_id = item["work_id"]

    rec_payload = _base_preflight_payload(work_id)
    begin_payload = _begin_payload_from(rec_payload)

    # Wire boundary regression: sending session_id in begin_stage_preflight MUST fail with HTTP 400
    forbidden_payload = dict(begin_payload, session_id="forbidden-session-id")
    status, body = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": forbidden_payload},
    )
    assert status == 400, f"Expected 400 for forbidden session_id, got {status}: {body}"
    assert "session_id" in str(body), f"Error response must reference session_id: {body}"

    # Valid payload without session_id succeeds with 200
    status, body = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
    )
    assert status == 200, body
    assert body["result"]["status"] == "applied"


def test_stage_preflight_intent_exact_replay_and_distinct_operation_logical_replay(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "preflight item")
    work_id = item["work_id"]

    rec_payload = _base_preflight_payload(work_id)
    begin_payload = _begin_payload_from(rec_payload)

    op_1 = uuid4()
    corr_1 = uuid4()

    # 1. Initial applied begin
    status, body1 = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        operation_id=op_1,
        correlation_id=corr_1,
    )
    assert status == 200, body1
    assert body1["result"]["status"] == "applied"
    initial_transport = body1["result"]["intent"]["transport_attempt_id"]
    assert initial_transport == str(op_1)

    # 2. Same operation exact replay remains standard command replay
    status, body_exact = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        operation_id=op_1,
        correlation_id=corr_1,
    )
    assert status == 200, body_exact
    assert body_exact["result"]["intent"]["transport_attempt_id"] == initial_transport

    # 3. Distinct operation with same logical identity returns replayed existing intent
    op_2 = uuid4()
    corr_2 = uuid4()
    status, body_logical = _exec_command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
        operation_id=op_2,
        correlation_id=corr_2,
    )
    assert status == 200, body_logical
    assert body_logical["result"]["status"] == "replayed"
    assert body_logical["result"]["intent"]["transport_attempt_id"] == initial_transport
    assert body_logical["result"]["intent"]["status"] == "begun"
    # Replayed begun returns no preflight
    assert body_logical["result"]["preflight"] is None

    # 4. Assert exactly one row exists for this logical identity
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM omp_work.stage_preflight_intents WHERE workspace_id=%s AND logical_sha256=%s",
                (workspace_id, body1["result"]["intent"]["logical_sha256"]),
            )
            count = cur.fetchone()[0]
            assert count == 1


def test_stage_preflight_intent_record_requires_intent_and_atomically_settles(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "settle item")
    work_id = item["work_id"]

    rec_payload = _base_preflight_payload(work_id)
    begin_payload = _begin_payload_from(rec_payload)

    # 1. Recording without intent fails / refused
    random_transport = uuid4()
    rec_without_intent = dict(rec_payload, transport_attempt_id=str(random_transport))
    status, err_body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": rec_without_intent},
    )
    assert status == 400, err_body
    assert err_body["error"]["code"] == "invalid_request"
    assert "preflight_intent_required" in err_body["error"]["diagnostics"]

    # 2. Begin valid intent
    status, begin_body = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
    )
    assert status == 200, begin_body
    transport_attempt_id = begin_body["result"]["intent"]["transport_attempt_id"]

    # 3. Record stage preflight matches intent and atomically settles
    valid_rec = dict(rec_payload, transport_attempt_id=transport_attempt_id)
    status, rec_body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": valid_rec},
    )
    assert status == 200, rec_body
    assert rec_body["result"]["status"] == "applied"

    # Verify atomic settlement in DB
    with psycopg.connect(**service.config.connection_kwargs("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, settled_at FROM omp_work.stage_preflight_intents WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            intent_row = cur.fetchone()
            assert intent_row is not None
            assert intent_row[0] == "settled"
            assert intent_row[1] is not None

            cur.execute(
                "SELECT outcome, stop_reason FROM omp_work.stage_preflights WHERE workspace_id=%s AND transport_attempt_id=%s",
                (workspace_id, transport_attempt_id),
            )
            evidence_row = cur.fetchone()
            assert evidence_row is not None
            assert evidence_row[0] == "selected"

    # 4. Terminal replay of record_stage_preflight returns evidence
    status, replay_rec = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": valid_rec},
    )
    assert status == 200, replay_rec
    assert replay_rec["result"]["status"] == "replayed"
    assert replay_rec["result"]["preflight"]["transport_attempt_id"] == transport_attempt_id

    # 5. Replay of begin_stage_preflight on settled intent returns terminal preflight evidence
    status, replay_begin = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
    )
    assert status == 200, replay_begin
    assert replay_begin["result"]["status"] == "replayed"
    assert replay_begin["result"]["intent"]["status"] == "settled"
    assert replay_begin["result"]["preflight"] is not None
    assert replay_begin["result"]["preflight"]["transport_attempt_id"] == transport_attempt_id


def test_stage_preflight_intent_mismatched_identity_stale_evidence_and_unknown_transport(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "mismatch item")
    work_id = item["work_id"]

    rec_payload = _base_preflight_payload(work_id)
    begin_payload = _begin_payload_from(rec_payload)

    status, begin_body = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_payload},
    )
    assert status == 200, begin_body
    transport_attempt_id = begin_body["result"]["intent"]["transport_attempt_id"]

    # 1. Unknown transport refused
    unknown_payload = dict(rec_payload, transport_attempt_id=str(uuid4()))
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": unknown_payload},
    )
    assert status == 400, body
    assert body["error"]["code"] == "invalid_request"
    assert "preflight_intent_required" in body["error"]["diagnostics"]

    # 2. Mismatched route/identity fields against begun intent -> stale_evidence
    mismatched_model_payload = dict(
        rec_payload,
        transport_attempt_id=transport_attempt_id,
        requested_model="gemini-different-model",
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": mismatched_model_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"
    assert "preflight_intent_identity_mismatch" in body["error"]["diagnostics"]

    # 3. Mismatched ordinal against begun intent -> stale_evidence
    mismatched_ordinal_payload = dict(
        rec_payload,
        transport_attempt_id=transport_attempt_id,
        ordinal=99,
    )
    status, body = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": mismatched_ordinal_payload},
    )
    assert status == 409, body
    assert body["error"]["code"] == "stale_evidence"


def test_stage_preflight_group_ordinal_sequencing_and_terminal_selection(service) -> None:
    workspace_id = uuid4()
    _grant(service, workspace_id)
    item = _create(service, workspace_id, "group sequencing item")
    work_id = item["work_id"]

    tool_call_id = f"tool-{uuid4()}"
    base_probe = "b" * 64

    # Ordinal 0
    rec_0 = _base_preflight_payload(
        work_id,
        tool_call_id=tool_call_id,
        probe_sha256=base_probe,
        ordinal=0,
        outcome="failed",
        error="probe provider connection failed",
    )
    begin_0 = _begin_payload_from(rec_0)

    # Ordinal 1 (sibling in same group)
    rec_1 = _base_preflight_payload(
        work_id,
        tool_call_id=tool_call_id,
        probe_sha256=base_probe,
        ordinal=1,
        requested_provider="anthropic",
        requested_model="claude-3-7-sonnet",
        requested_wire_model="claude-3-7-sonnet-20250219",
        outcome="selected",
    )
    begin_1 = _begin_payload_from(rec_1)

    # Ordinal 2 (later sibling)
    rec_2 = _base_preflight_payload(
        work_id,
        tool_call_id=tool_call_id,
        probe_sha256=base_probe,
        ordinal=2,
        requested_provider="openai",
        requested_model="gpt-4o",
        requested_wire_model="gpt-4o",
        outcome="selected",
    )
    begin_2 = _begin_payload_from(rec_2)

    # 1. Begin ordinal 0
    status, b0 = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_0},
    )
    assert status == 200, b0
    t0 = b0["result"]["intent"]["transport_attempt_id"]

    # 2. While ordinal 0 is begun, ordinal 1 in same group is BLOCKED
    status, b1_blocked = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_1},
    )
    assert status == 409, b1_blocked
    assert b1_blocked["error"]["code"] == "preflight_intent_active"
    assert "preflight_group_sibling_active" in b1_blocked["error"]["diagnostics"]

    # 3. Settle ordinal 0 with failed settlement
    rec_0["transport_attempt_id"] = t0
    status, r0 = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": rec_0},
    )
    assert status == 200, r0
    assert r0["result"]["status"] == "applied"

    # 4. Now that ordinal 0 failed, ordinal 1 can begin
    status, b1 = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_1},
    )
    assert status == 200, b1
    t1 = b1["result"]["intent"]["transport_attempt_id"]

    # 5. Settle ordinal 1 with selected settlement
    rec_1["transport_attempt_id"] = t1
    status, r1 = _command(
        service,
        workspace_id,
        {"type": "record_stage_preflight", "payload": rec_1},
    )
    assert status == 200, r1
    assert r1["result"]["status"] == "applied"

    # 6. Selected terminal route blocks later ordinal begin in same group
    status, b2_blocked = _command(
        service,
        workspace_id,
        {"type": "begin_stage_preflight", "payload": begin_2},
    )
    assert status == 409, b2_blocked
    assert b2_blocked["error"]["code"] == "preflight_intent_active"
    assert "preflight_group_terminal_route_selected" in b2_blocked["error"]["diagnostics"]


def test_stage_preflight_workspace_isolation_and_table_privilege_denials(service) -> None:
    ws_a = uuid4()
    ws_b = uuid4()
    _grant(service, ws_a)
    _grant(service, ws_b)

    item_a = _create(service, ws_a, "workspace A item")
    rec_a = _base_preflight_payload(item_a["work_id"])
    begin_a = _begin_payload_from(rec_a)

    status, b_a = _command(
        service,
        ws_a,
        {"type": "begin_stage_preflight", "payload": begin_a},
    )
    assert status == 200, b_a
    transport_a = b_a["result"]["intent"]["transport_attempt_id"]

    # Workspace isolation: workspace B cannot record or settle intent of workspace A
    rec_b = dict(rec_a, transport_attempt_id=transport_a)
    status, body_b = _command(
        service,
        ws_b,
        {"type": "record_stage_preflight", "payload": rec_b},
    )
    assert status == 409 or status == 400, body_b

    # Privilege denials for omp_work_app:
    # omp_work_app can UPDATE (status, settled_at) only.
    # Direct UPDATE on other columns, DELETE, and TRUNCATE must be denied.
    with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path = pg_catalog")
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)",
                (str(ws_a),),
            )
            cur.execute(
                "SELECT set_config('omp.actor_id', %s, true)",
                (str(uuid4()),),
            )

            # Direct UPDATE of forbidden column (e.g. role) must fail with permission denied
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "UPDATE omp_work.stage_preflight_intents SET role='audit' WHERE transport_attempt_id=%s",
                    (transport_a,),
                )
            conn.rollback()

            cur.execute("SET LOCAL search_path = pg_catalog")
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)",
                (str(ws_a),),
            )
            cur.execute(
                "SELECT set_config('omp.actor_id', %s, true)",
                (str(uuid4()),),
            )
            # Direct DELETE must fail
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "DELETE FROM omp_work.stage_preflight_intents WHERE transport_attempt_id=%s",
                    (transport_a,),
                )
            conn.rollback()

            cur.execute("SET LOCAL search_path = pg_catalog")
            cur.execute(
                "SELECT set_config('omp.workspace_id', %s, true)",
                (str(ws_a),),
            )
            cur.execute(
                "SELECT set_config('omp.actor_id', %s, true)",
                (str(uuid4()),),
            )
            # Direct TRUNCATE must fail
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute("TRUNCATE TABLE omp_work.stage_preflight_intents")
            conn.rollback()


def _make_operations_config(tmp_path) -> OperationsConfig:
    import secrets
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


def test_migration_0030_backfills_preexisting_0029_evidence_row(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omp_work.operations.database.validate_bundle", lambda **kw: None)
    cfg = _make_operations_config(tmp_path)
    original_migrate = database_module.migrate

    # Step 1: Bootstrap up to target migration 29 (prior to 0030)
    monkeypatch.setattr(
        database_module,
        "migrate",
        lambda c, target=None, lock_timeout=30: original_migrate(c, target=29, lock_timeout=lock_timeout),
    )
    with native_postgres(cfg.state_dir, cfg.port):
        bootstrap(cfg)
        monkeypatch.setattr(database_module, "migrate", original_migrate)

        workspace_id = uuid4()
        work_id = uuid4()
        transport_attempt_id = uuid4()
        preflight_id = uuid4()
        observed_time = datetime.now(timezone.utc)

        # Step 2: Insert a preexisting 0029 stage_preflight evidence row
        with psycopg.connect(**cfg.connection_kwargs("postgres"), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)", (workspace_id,))
                cur.execute(
                    "INSERT INTO omp_work.work_items(work_id, workspace_id, state) VALUES (%s, %s, 'NOW')",
                    (work_id, workspace_id),
                )
                cur.execute(
                    """
                    INSERT INTO omp_work.stage_preflights (
                        preflight_id, workspace_id, work_id, role, tool_call_id,
                        task_sha256, probe_sha256, transport_attempt_id, ordinal,
                        requested_selector, requested_provider, requested_model,
                        requested_api, requested_effort, requested_wire_model,
                        is_fallback, outcome, stop_reason, error, requests,
                        usage, provider_request_id, observed_at
                    ) VALUES (
                        %s, %s, %s, 'implement', 'tool-legacy-1',
                        %s, %s, %s, 0,
                        'gemini:flash', 'gemini', 'gemini-3.8-flash',
                        'google-genai', 'medium', 'gemini-3.8-flash-preview',
                        false, 'selected', 'stop', null, 1,
                        '{"input": 100, "output": 50}'::jsonb, 'req-legacy-1', %s
                    )
                    """,
                    (
                        preflight_id,
                        workspace_id,
                        work_id,
                        "a" * 64,
                        "b" * 64,
                        transport_attempt_id,
                        observed_time,
                    ),
                )

                # Capture exact row data before migration 0030
                cur.execute(
                    "SELECT preflight_id, workspace_id, work_id, transport_attempt_id, outcome, observed_at FROM omp_work.stage_preflights WHERE preflight_id=%s",
                    (preflight_id,),
                )
                row_before = cur.fetchone()

        # Step 3: Run migration 0030
        original_migrate(cfg)

        # Step 4: Verify preflight row is completely unchanged and intent is backfilled
        with psycopg.connect(**cfg.connection_kwargs("postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT preflight_id, workspace_id, work_id, transport_attempt_id, outcome, observed_at FROM omp_work.stage_preflights WHERE preflight_id=%s",
                    (preflight_id,),
                )
                row_after = cur.fetchone()
                assert row_before == row_after, "preflight row must be preserved unchanged"

                cur.execute(
                    """
                    SELECT
                        intent_id, workspace_id, work_id, transport_attempt_id,
                        status, host_owner_id, created_at, settled_at,
                        logical_sha256, group_sha256
                    FROM omp_work.stage_preflight_intents
                    WHERE workspace_id=%s AND transport_attempt_id=%s
                    """,
                    (workspace_id, transport_attempt_id),
                )
                intent_row = cur.fetchone()
                assert intent_row is not None
                assert intent_row[4] == "settled"
                assert intent_row[5] is None, "legacy owner must be null"
                assert intent_row[6] == observed_time
                assert intent_row[7] == observed_time

                expected_logical = hashlib.sha256(f"legacy:logical:{transport_attempt_id}".encode()).hexdigest()
                expected_group = hashlib.sha256(f"legacy:group:{transport_attempt_id}".encode()).hexdigest()
                assert intent_row[8] == expected_logical
                assert intent_row[9] == expected_group

                # Verify RLS is enabled and forced on both tables
                cur.execute(
                    "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid IN ('omp_work.stage_preflights'::regclass, 'omp_work.stage_preflight_intents'::regclass)"
                )
                rows = cur.fetchall()
                assert len(rows) == 2
                for relname, rls, force in rows:
                    assert rls, f"RLS must be enabled on {relname}"
                    assert force, f"FORCE RLS must be enabled on {relname}"
