# ruff: noqa: F811
"""OMP-430-s08: client-neutral delivery e2e test on PostgreSQL.

Proves that any push subscriber, Grok Bot included, receives budget notices,
OMP-406 alerts, and daily digests identically.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, time, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401 (fixture)
from omp_work import alarm_classify, event_push
from omp_work.control_actions import perform
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.budget_relay import relay_budget_alerts
from omp_work.jobs.store import NativeJobStore
from omp_work.jobs.usage import record_usage
from omp_work.operations.capabilities import (
    OWNER_SCOPES,
    provision_client,
    provision_event_push,
)
from omp_work.operations.config import OperationsConfig
from omp_work.v1.api_models import (
    DomainEventsPage,
    DomainEventView,
    EventSubscriptionsPage,
    MissionEventsPage,
)
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    BeginCloseAttemptCommand,
    BeginCloseAttemptPayload,
    CommandEnvelope,
    OperationReceipt,
    PutEventSubscription,
    PutEventSubscriptionCommand,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)
from omp_work.v1.service import Principal, WorkService
from omp_work.v1.store import PostgresWorkStore
from test_control_actions import (
    NOW,
    REPOS,
    TIER3_SUBMISSIONS,
    _Executor,
    _mandate_all_tier3,
    _resolver_for,
)
from test_item_budget_alerts import _GENEROUS
from test_workflow_service import OWNER, _create, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_HOST = "hooks.example"
_PUSH_IP = "93.184.216.34"


def _seed_item_budget(
    config: OperationsConfig,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    work_id: UUID,
    revision_id: UUID,
    budget: dict[str, object],
) -> None:
    candidate_id, receipt_id = uuid4(), uuid4()
    payload = {
        "draft": {"budget": budget},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(actor_id),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            """
            INSERT INTO omp_work.candidates(
                candidate_id, workspace_id, work_id, revision_id,
                candidate_sha256, commit_sha, kind, allocated_at
            ) VALUES (%s, %s, %s, %s, %s, NULL, 'planned', %s)
            """,
            (
                candidate_id,
                workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(timezone.utc),
            ),
        )
        conn.execute(
            """
            INSERT INTO omp_evidence.receipts(
                receipt_id, workspace_id, work_id, revision_id, candidate_id,
                kind, payload, payload_sha256, issuer, issued_at,
                candidate_sha256, candidate_commit
            ) VALUES (%s, %s, %s, %s, %s, 'intake_publication', %s, %s, 'work-service/bounded-intake', %s, %s, NULL)
            """,
            (
                receipt_id,
                workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(timezone.utc),
                "0" * 64,
            ),
        )


class _WorkServiceClientAdapter:
    def __init__(
        self,
        service: WorkService,
        workspace_id: UUID,
        principal: Principal,
    ) -> None:
        self._service = service
        self._workspace_id = workspace_id
        self._principal = principal

    def event_subscriptions(self) -> EventSubscriptionsPage:
        raw = self._service.event_subscriptions(self._principal, self._workspace_id)
        return EventSubscriptionsPage.model_validate(raw)

    def events(self, after_sequence: int = 0, limit: int = 500) -> DomainEventsPage:
        raw = self._service.events(
            self._principal,
            self._workspace_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return DomainEventsPage.model_validate(raw)

    def mission_events(
        self, after_sequence: int = 0, limit: int = 500
    ) -> MissionEventsPage:
        raw = self._service.mission_events(
            self._principal,
            self._workspace_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return MissionEventsPage.model_validate(raw)

    def execute(
        self, envelope: CommandEnvelope
    ) -> tuple[OperationReceipt, dict[str, object]]:
        return self._service.execute(self._principal, envelope)


def test_client_neutral_delivery_e2e(
    native_jobs, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = native_jobs.service
    config = service.config
    workspace_id = uuid4()
    _grant(service, workspace_id)

    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (workspace_id,),
        )

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_stub(
        host: object, port: object = None, *args: object, **kwargs: object
    ):
        name = (
            (
                host.decode("ascii", "replace")
                if isinstance(host, bytes)
                else str(host or "")
            )
            .strip()
            .lower()
            .rstrip(".")
        )
        if name == _HOST:
            return [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    (_PUSH_IP, 0),
                )
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo_stub)
    monkeypatch.setattr(event_push.socket, "getaddrinfo", getaddrinfo_stub)

    # Allowlist host and write push-signing key
    dest_file = config.config_dir / "push-destinations.json"
    dest_file.write_text(json.dumps({"allowed_hosts": [_HOST]}), encoding="utf-8")
    master_key = bytes(range(32))
    key_file = config.config_dir / "push-signing.key"
    key_file.write_bytes(master_key)
    key_file.chmod(0o600)

    work_store = PostgresWorkStore(config)
    work_service = WorkService(
        work_store,
        push_destination_check=lambda url: event_push.check_destination(
            url, allowed_hosts=event_push.load_allowed_hosts(config.config_dir)
        ),
    )

    # Two provision_client principals: one named "grok-bot"
    grok_cap = provision_client(config, workspace_id, "grok-bot")
    grok_data = json.loads(grok_cap.read_text(encoding="utf-8"))
    grok_principal = Principal(
        actor_id=UUID(str(grok_data["actor_id"])),
        actor_kind="client",
        workspaces=frozenset({workspace_id}),
        scopes=frozenset(grok_data["scopes"]),
    )

    second_cap = provision_client(config, workspace_id, "second-client")
    second_data = json.loads(second_cap.read_text(encoding="utf-8"))
    second_principal = Principal(
        actor_id=UUID(str(second_data["actor_id"])),
        actor_kind="client",
        workspaces=frozenset({workspace_id}),
        scopes=frozenset(second_data["scopes"]),
    )

    owner_principal = Principal(
        actor_id=OWNER,
        actor_kind="owner",
        workspaces=frozenset({workspace_id}),
        scopes=frozenset(OWNER_SCOPES),
    )

    # Both clients register ops.alarm and ops.digest push subscriptions
    grok_alarm_id = uuid4()
    grok_digest_id = uuid4()
    second_alarm_id = uuid4()
    second_digest_id = uuid4()

    grok_alarm_url = f"https://{_HOST}/grokbot/alarm"
    grok_digest_url = f"https://{_HOST}/grokbot/digest"
    second_alarm_url = f"https://{_HOST}/second/alarm"
    second_digest_url = f"https://{_HOST}/second/digest"

    for client_p, alarm_id, alarm_url, digest_id, digest_url in (
        (
            grok_principal,
            grok_alarm_id,
            grok_alarm_url,
            grok_digest_id,
            grok_digest_url,
        ),
        (
            second_principal,
            second_alarm_id,
            second_alarm_url,
            second_digest_id,
            second_digest_url,
        ),
    ):
        work_service.execute(
            client_p,
            CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=PutEventSubscriptionCommand(
                    type="put_event_subscription",
                    payload=PutEventSubscription(
                        subscription_id=alarm_id,
                        push_url=alarm_url,
                        event_types=("ops.alarm",),
                    ),
                ),
            ),
        )
        work_service.execute(
            client_p,
            CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=PutEventSubscriptionCommand(
                    type="put_event_subscription",
                    payload=PutEventSubscription(
                        subscription_id=digest_id,
                        push_url=digest_url,
                        event_types=("ops.digest",),
                    ),
                ),
            ),
        )

    # 1. 50/80/100% tokens budget rows relayed by relay_budget_alerts
    item = _create(service, workspace_id, "budget tokens delivery work item")
    work_id = UUID(item["work_id"])
    revision_id = UUID(item["revision_id"])

    _seed_item_budget(
        config,
        workspace_id,
        OWNER,
        work_id=work_id,
        revision_id=revision_id,
        budget={**_GENEROUS, "tokens": 1000},
    )

    job_store = NativeJobStore(config)
    job_id = f"job-{uuid4()}"
    enqueue_job(
        job_store,
        operation_id=str(uuid4()),
        workspace_id=workspace_id,
        actor_id=OWNER,
        job_id=job_id,
        work_id=work_id,
        kind="compute",
        required_capabilities=["compute.cpu"],
        resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
        lease_seconds=30,
    )

    # 50% threshold notice
    record_usage(
        job_store,
        operation_id=f"op-{uuid4()}",
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=work_id,
        job_id=job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=300,
        output_tokens=200,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.10",
    )
    # 80% threshold notice
    record_usage(
        job_store,
        operation_id=f"op-{uuid4()}",
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=work_id,
        job_id=job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=200,
        output_tokens=100,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.10",
    )
    # 100% exceeded notice
    record_usage(
        job_store,
        operation_id=f"op-{uuid4()}",
        workspace_id=workspace_id,
        actor_id=OWNER,
        work_id=work_id,
        job_id=job_id,
        request_id=f"req-{uuid4()}",
        role="coder",
        model="gemini-3.8-flash",
        input_tokens=200,
        output_tokens=0,
        cache_tokens=0,
        measurement="measured",
        price_usd="0.10",
    )

    relayed = relay_budget_alerts(
        job_store,
        work_store,
        workspace_id=workspace_id,
        actor_id=OWNER,
    )
    assert relayed == 3

    # Snapshot alerts caused by setup prior to the control_actions perform calls
    event_push_bearer = provision_event_push(config, workspace_id)
    push_data = json.loads(event_push_bearer.read_text(encoding="utf-8"))
    push_principal = Principal(
        actor_id=UUID(str(push_data["actor_id"])),
        actor_kind="automation",
        workspaces=frozenset({workspace_id}),
        scopes=frozenset(push_data["scopes"]),
    )

    early_events_page = work_service.events(
        push_principal, workspace_id, after_sequence=0, limit=500
    )
    early_events = [
        DomainEventView.model_validate(e) for e in early_events_page["events"]
    ]
    early_alerts, _, _ = alarm_classify.classify(early_events)
    setup_oaa = sum(1 for a in early_alerts if a.kind == "owner_approval_attempt")

    # 2. Held tier 3 action and tier 2 action refused standing_policy_required
    project_id = work_store.ensure_project(
        workspace_id, OWNER, "control", "Control key", "surface"
    )
    work_store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    _mandate_all_tier3(work_store, workspace_id, project_id)
    resolver = _resolver_for(work_store, workspace_id, project_id)

    held_action = perform(
        work_store,
        workspace_id,
        OWNER,
        project_id,
        None,
        dict(TIER3_SUBMISSIONS["billing_change"]),
        resolver,
        _Executor(),
        NOW,
    )
    assert held_action.status == "held"

    tier2_submission = {
        "kind": "git_push",
        "repository": "repo-alpha",
        "branch": "feature/uncovered",
    }
    tier2_action = perform(
        work_store,
        workspace_id,
        OWNER,
        project_id,
        None,
        tier2_submission,
        resolver,
        _Executor(),
        NOW,
    )
    assert tier2_action.status == "refused"
    assert tier2_action.code == "standing_policy_required"

    # 3. safety_check_failed and credential_appeared signals
    work_service.execute(
        owner_principal,
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordAlarmSignalCommand(
                type="record_alarm_signal",
                payload=RecordAlarmSignalPayload(
                    signal="safety_check_failed",
                    subject="safety check failed signal",
                    detail="explicit safety check failed",
                ),
            ),
        ),
    )
    work_service.execute(
        owner_principal,
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=RecordAlarmSignalCommand(
                type="record_alarm_signal",
                payload=RecordAlarmSignalPayload(
                    signal="credential_appeared",
                    subject="credential appeared signal",
                    detail="explicit credential appeared",
                ),
            ),
        ),
    )

    # 4. Three refusals on one aggregate (triggers repeated_failure alert)
    for _ in range(3):
        _receipt, res = work_service.execute(
            owner_principal,
            CommandEnvelope(
                api_version="work.omp.dev/v1",
                workspace_id=workspace_id,
                operation_id=uuid4(),
                request_id=uuid4(),
                correlation_id=uuid4(),
                command=BeginCloseAttemptCommand(
                    type="begin_close_attempt",
                    payload=BeginCloseAttemptPayload(
                        work_id=work_id,
                        attempt_id=uuid4(),
                        authorization_ref=f"summary:{uuid4()}",
                        owner_session_id="session-test",
                        owner_session_started_at=datetime.now(timezone.utc),
                        owner_session_start_commit="e" * 40,
                        repository="/repo",
                        diff_sha256="0" * 64,
                    ),
                ),
            ),
        )
        assert res.get("status") == "refused"

    # 5. Work on the previous UTC day
    today_utc = datetime.now(timezone.utc).date()
    previous_day = today_utc - timedelta(days=1)
    previous_dt = datetime.combine(previous_day, time(12, 0), tzinfo=timezone.utc)

    with psycopg.connect(**config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "ALTER TABLE omp_audit.domain_events DISABLE TRIGGER immutable_events"
        )
        try:
            conn.execute(
                "UPDATE omp_audit.domain_events SET occurred_at = %s WHERE workspace_id = %s",
                (previous_dt, workspace_id),
            )
        finally:
            conn.execute(
                "ALTER TABLE omp_audit.domain_events ENABLE TRIGGER immutable_events"
            )

    # Compute expected alerts across all workspace domain events
    all_events_page = work_service.events(
        push_principal, workspace_id, after_sequence=0, limit=500
    )
    all_events = [DomainEventView.model_validate(e) for e in all_events_page["events"]]
    expected_alerts, _, _ = alarm_classify.classify(all_events)
    expected_kinds = {a.kind for a in expected_alerts}
    assert expected_kinds == alarm_classify.ALERT_KINDS
    expected_keys = [a.idempotency_key for a in expected_alerts]

    # Run push with today = next day after work (today_utc)
    recorded_sends: list[dict[str, Any]] = []

    def record_send(
        url: str,
        idempotency_key: str,
        body: Any,
        *,
        key: bytes,
    ) -> None:
        parsed_body = json.loads(body) if isinstance(body, (str, bytes)) else body
        recorded_sends.append(
            {
                "url": str(url),
                "idempotency_key": str(idempotency_key),
                "body": parsed_body,
                "key": key,
            }
        )

    push_client = _WorkServiceClientAdapter(work_service, workspace_id, push_principal)
    allowed_hosts = event_push.load_allowed_hosts(config.config_dir)

    run_result = event_push.run_push(
        push_client,
        workspace_id=workspace_id,
        master_key=master_key,
        allowed_hosts=allowed_hosts,
        send=record_send,
        today=today_utc,
    )
    assert "failed" not in run_result
    assert "refused" not in run_result
    assert run_result["pushed"] == len(recorded_sends)

    # Deliveries grouped by destination
    grok_alarm = [s for s in recorded_sends if s["url"] == grok_alarm_url]
    grok_digest = [s for s in recorded_sends if s["url"] == grok_digest_url]
    second_alarm = [s for s in recorded_sends if s["url"] == second_alarm_url]
    second_digest = [s for s in recorded_sends if s["url"] == second_digest_url]

    # Done criteria:
    # 1. Each client's ops.alarm gets all six OMP-406 kinds
    assert {s["body"]["kind"] for s in grok_alarm} == alarm_classify.ALERT_KINDS
    assert {s["body"]["kind"] for s in second_alarm} == alarm_classify.ALERT_KINDS

    # 2. cost_threshold x2 and budget_exceeded x1 from the relay
    assert sum(1 for s in grok_alarm if s["body"]["kind"] == "cost_threshold") == 2
    assert sum(1 for s in second_alarm if s["body"]["kind"] == "cost_threshold") == 2
    assert sum(1 for s in grok_alarm if s["body"]["kind"] == "budget_exceeded") == 1
    assert sum(1 for s in second_alarm if s["body"]["kind"] == "budget_exceeded") == 1

    # 3. owner_approval_attempt x2 plus the setup's
    assert (
        sum(1 for s in grok_alarm if s["body"]["kind"] == "owner_approval_attempt")
        == 2 + setup_oaa
    )
    assert (
        sum(1 for s in second_alarm if s["body"]["kind"] == "owner_approval_attempt")
        == 2 + setup_oaa
    )

    # 4. Each alert once with Idempotency-Key "{event_id}:{kind}", same keys for both clients
    grok_keys = [s["idempotency_key"] for s in grok_alarm]
    second_keys = [s["idempotency_key"] for s in second_alarm]
    assert len(grok_keys) == len(set(grok_keys))
    assert len(second_keys) == len(set(second_keys))
    assert grok_keys == expected_keys
    assert second_keys == expected_keys

    for s in grok_alarm:
        expected_key = f"{s['body']['event']['event_id']}:{s['body']['kind']}"
        assert s["idempotency_key"] == expected_key
    for s in second_alarm:
        expected_key = f"{s['body']['event']['event_id']}:{s['body']['kind']}"
        assert s["idempotency_key"] == expected_key

    # 5. ops.digest receives one "digest:{workspace}:{day}"
    assert len(grok_digest) == 1
    assert len(second_digest) == 1
    expected_digest_key = f"digest:{workspace_id}:{previous_day.isoformat()}"
    assert grok_digest[0]["idempotency_key"] == expected_digest_key
    assert second_digest[0]["idempotency_key"] == expected_digest_key
    assert grok_digest[0]["body"]["type"] == "digest"
    assert second_digest[0]["body"]["type"] == "digest"

    # 6. A rerun sends nothing
    rerun_sends: list[str] = []
    rerun_result = event_push.run_push(
        push_client,
        workspace_id=workspace_id,
        master_key=master_key,
        allowed_hosts=allowed_hosts,
        send=lambda url, idem, body, key=None: rerun_sends.append(idem),
        today=today_utc,
    )
    assert rerun_sends == []
    assert rerun_result == {"pushed": 0}
