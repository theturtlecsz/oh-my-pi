"""Real compiled worker authority across lease renewal (OMP-595-s07-s05)."""

# ruff: noqa: F811  (the `renewed_world` fixture parameter shadows the imported fixture)
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from omp_work.jobs.worker import JobWorker
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator.service import OrchestratorHandler, submit
from omp_work.v1.server import create_app as work_app
from orchestrator_e2e_support import open_e2e_world, open_worker, tick_until
from psycopg.rows import dict_row
from test_item_budget_alerts import _seed_budget
from test_model_proxy import serving
from test_model_proxy_upstream import gemini_route
from test_orchestrator_production_worker import (
    CONTENT,
    CONTROL_TOKEN,
    PROVIDER_KEY,
    binary,  # noqa: F401 (fixture)
    upstream,
)
from test_workflow_service import OWNER

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_GATE_TIMEOUT_S = 30.0
_LEASE_SECONDS = 4

# Concrete authority configured by this fixture's gemini_route defaults. These are
# independent expected values, not the descriptor the fixture itself produced.
_EXPECTED_CONTEXT_TOKENS = 100_000
_EXPECTED_MAX_OUTPUT_TOKENS = 1000
# Cost of one measured call: 10 input tokens at 0.30 USD/M and 5 output tokens at
# 2.50 USD/M. Two consumed calls total 0.000031.
_PER_CALL_USD = Decimal("0.0000155")
_TOTAL_USD = Decimal("0.000031")


def test_namespaces_are_available() -> None:
    """Fail, not skip, when the jail cannot be built: the cases below need a real worker."""
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
        capture_output=True,
        timeout=10,
    )
    assert probe.returncode == 0, probe.stderr


class RenewalGate:
    """Synchronize model response release with proven DB lease renewal past expiry."""

    def __init__(self, db_kwargs: dict[str, Any]):
        # Every fixture query, including polling and final ledgers, uses these
        # server-side bounds. A Python polling deadline cannot interrupt SQL.
        self.db_kwargs = {
            **db_kwargs,
            "connect_timeout": 3,
            "options": (
                f"{db_kwargs.get('options', '')} "
                "-c statement_timeout=2000 -c lock_timeout=1000"
            ).strip(),
        }
        self.workspace_id: UUID | None = None
        self.job_id: str | None = None
        self.worker_id: str | None = None
        self.fence: int | None = None
        self.initial_lease: datetime | None = None
        self.original_expiry: datetime | None = None
        self.renewed_expiry: datetime | None = None
        self.renewals: list[dict[str, Any]] = []
        # Provider call log shared with the fake upstream; read to prove no second
        # provider call happened while the worker was held across the renewal.
        self.provider_calls: list[Any] = []
        self.hold_started_at: datetime | None = None
        self.calls_at_hold: int | None = None
        self.renewals_at_hold: int | None = None
        self.released_at: datetime | None = None
        self.calls_at_release: int | None = None
        self.renewals_at_release: int | None = None
        self.stop = threading.Event()

    def record_grant(self, payload: dict[str, Any]) -> None:
        self.workspace_id = UUID(str(payload["workspace_id"]))
        self.job_id = str(payload["job_id"])
        self.worker_id = str(payload["worker_id"])
        self.fence = int(payload["fence"])
        with psycopg.connect(
            **self.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            row = conn.execute(
                "SELECT lease_expires_at, worker_id, fence FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (self.workspace_id, self.job_id),
            ).fetchone()
        assert row is not None
        assert row["worker_id"] == self.worker_id
        assert row["fence"] == self.fence
        self.initial_lease = row["lease_expires_at"]
        self.original_expiry = row["lease_expires_at"]

    def mark_call1_committed(self) -> None:
        self.hold_started_at = datetime.now(UTC)
        self.calls_at_hold = len(self.provider_calls)
        self.renewals_at_hold = self._count_renewals()

    def _count_renewals(self) -> int:
        with psycopg.connect(
            **self.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            row = conn.execute(
                "SELECT count(*) AS n FROM omp_jobs.operations WHERE workspace_id=%s AND kind='job_renew' AND result->'job'->>'job_id'=%s",
                (self.workspace_id, self.job_id),
            ).fetchone()
        return int(row["n"])

    async def wait_for_renewal_past_expiry(self) -> None:
        assert self.original_expiry is not None
        deadline = time.monotonic() + _GATE_TIMEOUT_S
        assert self.hold_started_at is not None
        with psycopg.connect(
            **self.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            while not self.stop.is_set():
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        "timed out waiting for DB renewal past original expiry"
                    )
                now = datetime.now(UTC)
                past_expiry = now > self.original_expiry
                job_row = conn.execute(
                    "SELECT lease_expires_at, worker_id, fence FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                    (self.workspace_id, self.job_id),
                ).fetchone()
                renewals = conn.execute(
                    "SELECT * FROM omp_jobs.operations WHERE workspace_id=%s AND kind='job_renew' AND result->'job'->>'job_id'=%s",
                    (self.workspace_id, self.job_id),
                ).fetchall()
                if (
                    job_row is not None
                    and job_row["worker_id"] == self.worker_id
                    and job_row["fence"] == self.fence
                    and job_row["lease_expires_at"] > self.original_expiry
                    and len(renewals) > self.renewals_at_hold
                    and past_expiry
                ):
                    # Requiring a renewal committed inside the hold window proves the
                    # held worker kept its lease past the original expiry without a
                    # second provider call and without waiting on a job lock.
                    self.renewed_expiry = job_row["lease_expires_at"]
                    self.renewals = list(renewals)
                    self.calls_at_release = len(self.provider_calls)
                    self.renewals_at_release = len(renewals)
                    self.released_at = datetime.now(UTC)
                    return
                await asyncio.sleep(0.05)


@dataclass
class HttpExchange:
    """Bytes observed at the proxy boundary, before provider normalization."""

    method: str
    path: str
    query: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    request_body: bytes = b""
    status: int | None = None
    response_headers: tuple[tuple[bytes, bytes], ...] = ()
    response_body: bytes = b""


class HoldFirstProxyResponse:
    """Record HTTP and hold first response after budget commit, with DB locks free."""

    def __init__(self, app: Any, gate: RenewalGate):
        self.app = app
        self.gate = gate
        self.exchanges: list[HttpExchange] = []
        self.errors: list[Exception] = []
        self._model_calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        exchange = HttpExchange(
            scope["method"],
            scope["path"],
            scope["query_string"],
            tuple(scope["headers"]),
        )
        self.exchanges.append(exchange)
        is_grant = exchange.method == "POST" and exchange.path == "/tasks"
        is_model = exchange.method == "POST" and not (
            exchange.path == "/tasks" or exchange.path.startswith("/tasks/")
        )
        if is_model:
            self._model_calls += 1
        hold = is_model and self._model_calls == 1

        async def receive_recorded() -> Any:
            message = await receive()
            if message["type"] == "http.request":
                exchange.request_body += message.get("body", b"")
                if is_grant and not message.get("more_body", False):
                    self.gate.record_grant(json.loads(exchange.request_body))
            return message

        async def send_recorded(message: Any) -> None:
            if message["type"] == "http.response.start":
                exchange.status = message["status"]
                exchange.response_headers = tuple(message.get("headers", ()))
                if hold:
                    self.gate.mark_call1_committed()
                    await self.gate.wait_for_renewal_past_expiry()
            elif message["type"] == "http.response.body":
                exchange.response_body += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, receive_recorded, send_recorded)
        except Exception as exc:
            # Uvicorn otherwise logs ASGI failures only in its server thread.
            self.errors.append(exc)
            raise


@dataclass
class RenewedWorld:
    """Exposes real execution handles, gates, ledgers and recording stores."""

    world: Any
    worker: JobWorker
    gate: RenewalGate
    proxy_url: str
    work_url: str
    proxy: HoldFirstProxyResponse
    manifests: list[dict[str, Any]]
    calls: list[Any]
    provider_entered: Any
    provider_release: Any
    service: Any

    @property
    def grants(self) -> list[HttpExchange]:
        return [r for r in self.proxy.exchanges if r.method == "POST" and r.path == "/tasks"]

    @property
    def revocations(self) -> list[HttpExchange]:
        return [r for r in self.proxy.exchanges if r.method == "DELETE"]

    @property
    def worker_requests(self) -> list[HttpExchange]:
        return [r for r in self.proxy.exchanges if r.method == "POST" and r.path != "/tasks"]

    def run(self) -> dict[str, Any]:
        def implemented(view: dict[str, Any]) -> bool:
            return any(
                step.get("stage") == "implement" and step.get("kind") == "outcome"
                for step in view["steps"]
            )

        try:
            tick_until(self.worker, self.world, implemented, limit=60)
        finally:
            if self.proxy.errors:
                raise ExceptionGroup("proxy server failed", self.proxy.errors)
        handler = OrchestratorHandler(self.world.config)
        handler.mission_id = self.world.mission_id
        return next(
            step
            for step in handler.steps()
            if step.get("stage") == "implement" and step.get("kind") == "outcome"
        )

    def usage_events(self) -> list[dict[str, Any]]:
        with psycopg.connect(
            **self.gate.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            return conn.execute(
                "SELECT * FROM omp_jobs.usage_events WHERE workspace_id=%s ORDER BY recorded_at",
                (self.world.workspace_id,),
            ).fetchall()

    def spend_records(self) -> list[dict[str, Any]]:
        with psycopg.connect(
            **self.gate.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            return conn.execute(
                "SELECT * FROM omp_work.spend_records WHERE workspace_id=%s ORDER BY recorded_at",
                (self.world.workspace_id,),
            ).fetchall()

    def receipts(self) -> list[dict[str, Any]]:
        with psycopg.connect(
            **self.gate.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            return conn.execute(
                "SELECT * FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt' ORDER BY created_at",
                (self.world.workspace_id,),
            ).fetchall()

    def job_renewals(self) -> list[dict[str, Any]]:
        with psycopg.connect(
            **self.gate.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            return conn.execute(
                "SELECT * FROM omp_jobs.operations WHERE workspace_id=%s AND kind='job_renew' ORDER BY created_at",
                (self.world.workspace_id,),
            ).fetchall()

    def job_row(self, job_id: str | None = None) -> dict[str, Any] | None:
        """Current job row: proves the final lease ownership and fence after exit."""
        jid = job_id or self.gate.job_id
        if jid is None:
            return None
        with psycopg.connect(
            **self.gate.db_kwargs, autocommit=True, row_factory=dict_row
        ) as conn:
            return conn.execute(
                "SELECT * FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
                (self.world.workspace_id, jid),
            ).fetchone()


@pytest.fixture
def renewed_world(service: Any, tmp_path: Path, monkeypatch: Any, binary: Any):
    """Fixture wiring real compiled worker sandbox, proxy, DB JobWorker and fake provider."""
    binary_exe, shim, native = binary
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    monkeypatch.setenv("GEMINI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OMP_CONTROL_TOKEN", CONTROL_TOKEN)

    world = open_e2e_world(
        service,
        tmp_path / "renewed",
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    world.request = replace(
        world.request,
        worker_argv=(str(binary_exe), "orchestrator-worker"),
        scope=world.request.scope.model_copy(
            update={
                "budget_policy": world.request.scope.budget_policy.model_copy(
                    update={"tokens": 200_000}
                )
            }
        ),
    )
    capability = world.config.automation_capability_path
    data = json.loads(capability.read_text(encoding="utf-8"))
    data["token"] = CONTROL_TOKEN
    capability.write_text(json.dumps(data), encoding="utf-8")
    capability.chmod(0o600)
    key = service.config.credentials_dir / "gemini-key"
    key.write_text(PROVIDER_KEY, encoding="utf-8")
    key.chmod(0o600)

    db_kwargs = service.config.connection_kwargs("postgres")
    gate = RenewalGate(db_kwargs)

    manifests: list[dict[str, Any]] = []
    real_sandbox = worker_sandbox.run_sandboxed

    def observe_sandbox(
        argv: Any,
        identity: Any,
        recorder: Any,
        workdir: Any,
        env: Any,
        timeout: Any,
        **kwargs: Any,
    ) -> int:
        manifest_path = env.get("OMP_WORKER_MANIFEST") if env else None
        if manifest_path:
            manifest_file = Path(manifest_path)
            assert manifest_file.stat().st_mode & 0o777 == 0o600
            manifests.append(json.loads(manifest_file.read_text(encoding="utf-8")))
        kwargs["ro_binds"] = (*kwargs.get("ro_binds", ()), str(shim), str(native))
        return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", observe_sandbox)

    with upstream(False) as (transport, calls, entered, release):
        gate.provider_calls = calls
        config = ProxyConfig(
            service.config,
            capability,
            {
                "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
            },
            overflow="queue",
        )
        base_app = create_app(config, transport=transport)
        held_app = HoldFirstProxyResponse(base_app, gate)

        with (
            serving(held_app) as proxy_url,
            serving(
                work_app(service.config, capabilities_dir=service.capabilities)
            ) as work_url,
        ):
            world.config = replace(
                world.config,
                model_proxy_url=proxy_url,
                workservice_url=work_url,
                lease_seconds=_LEASE_SECONDS,
            )
            submitted = submit(world.config, world.request)
            work_id = UUID(submitted["work_id"])
            tree = world.store.read(world.workspace_id, OWNER, "tree", "")
            item = next(
                row for row in tree["items"] if str(row.get("work_id")) == str(work_id)
            )
            _seed_budget(
                SimpleNamespace(
                    service=service, workspace_id=world.workspace_id, actor_id=OWNER
                ),
                work_id=work_id,
                revision_id=UUID(str(item["revision"]["revision_id"])),
                budget={
                    "usd": "2.00",
                    "tokens": 200_000,
                    "wall_clock_seconds": 180,
                    "max_subagents": 0,
                },
            )
            worker = open_worker(world)

            state = RenewedWorld(
                world=world,
                worker=worker,
                gate=gate,
                proxy_url=proxy_url,
                work_url=work_url,
                proxy=held_app,
                manifests=manifests,
                calls=calls,
                provider_entered=entered,
                provider_release=release,
                service=service,
            )
            try:
                yield state
            finally:
                gate.stop.set()
                release.set()


def test_renewed_model_authority(renewed_world: RenewedWorld):
    """Real worker calls provider before/after original interval under proven renewal."""
    state = renewed_world
    outcome = state.run()

    # 1. Success and allowed edit
    assert outcome["outcome"] == "succeeded"
    assert outcome["data"]["exit_code"] == 0
    worktree = Path(outcome["data"]["worktree"])
    feature_path = worktree / "feature.txt"
    assert feature_path.is_file()
    assert feature_path.read_text(encoding="utf-8") == CONTENT

    # 2. Fixed token/route/candidate/scopes/USD and token ceilings/output cap
    assert len(state.grants) == 1
    grant_http = state.grants[0]
    assert grant_http.status == 200
    assert dict(grant_http.headers)[b"authorization"] == f"Bearer {CONTROL_TOKEN}".encode()
    grant = json.loads(grant_http.response_body)
    grant_payload = json.loads(grant_http.request_body)
    model_token = grant["model_token"]
    assert model_token and isinstance(model_token, str)

    assert len(state.manifests) == 1
    manifest = state.manifests[0]

    # Fixed route
    assert grant["descriptor"]["id"] == "gemini-3.8-flash-high"
    assert manifest["model"]["id"] == "gemini-3.8-flash-high"
    assert manifest["model"]["api"] == "google-generative-ai"

    # Fixed candidate
    candidate_id = manifest["candidate_id"]
    assert candidate_id and isinstance(candidate_id, str)

    # Fixed scopes
    assert manifest["allowed_paths"] == ["feature.txt"]
    cap_dir = state.world.config.ops.config_dir / "capabilities"
    cap_files = list(cap_dir.glob(f"task-{state.world.mission_id}-*-implement.json"))
    assert len(cap_files) == 1
    task_cap = json.loads(cap_files[0].read_text(encoding="utf-8"))
    assert task_cap["scopes"] == ["work.candidate.read"]
    assert task_cap["candidate_ids"] == [candidate_id]
    assert task_cap["expires_at"] == manifest["task_deadline"]

    # Fixed USD ceiling
    assert Decimal(grant_payload["ceiling_usd"]) == Decimal("2.00")

    # Fixed token ceiling and concrete output/context caps: independent expected
    # limits, not the descriptor the fixture produced, and the caps actually sent
    # to the provider on both calls (a dropped cap or a limit change across
    # renewal would fail here).
    assert state.world.request.scope.budget_policy.tokens == 200_000
    assert manifest["model"]["limits"]["context_window"] == _EXPECTED_CONTEXT_TOKENS
    assert manifest["model"]["limits"]["max_output_tokens"] == _EXPECTED_MAX_OUTPUT_TOKENS
    # Raw worker wire requests retain scoped credentials and tool results; the
    # upstream log alone cannot prove what the worker sent before normalization.
    assert len(state.worker_requests) == 2
    for request in state.worker_requests:
        assert request.status == 200
        assert request.path.endswith("/models/gemini-3.8-flash-high:streamGenerateContent")
        assert request.query == b"alt=sse"
        assert dict(request.headers)[b"x-goog-api-key"] == model_token.encode()
        assert b"data: " in request.response_body
    second_body = json.loads(state.worker_requests[1].request_body)
    assert any(
        part.get("functionResponse", {}).get("name") == "write"
        for row in second_body["contents"]
        for part in row.get("parts", [])
    )
    provider_calls = state.calls
    assert len(provider_calls) == 2
    for path, body, _headers, _held, _at in provider_calls:
        assert path.endswith(":streamGenerateContent?alt=sse")
        config = body["generationConfig"]
        assert config["maxOutputTokens"] == _EXPECTED_MAX_OUTPUT_TOKENS
        assert config["thinkingConfig"]["thinkingBudget"] == 500

    # 3. Proven renewal under same worker/fence; calls before and after original interval
    assert len(state.calls) == 2
    assert state.gate.initial_lease is not None
    assert state.gate.original_expiry is not None
    assert state.gate.renewed_expiry is not None

    call1_time = state.calls[0][4]
    call2_time = state.calls[1][4]
    assert call1_time < state.gate.original_expiry
    assert call2_time > state.gate.original_expiry
    assert state.gate.renewed_expiry > state.gate.original_expiry

    # The hold began after the first call committed and ended only after the renewed
    # lease passed the original expiry. While held, exactly the first provider call had
    # landed, so renewal needed no second provider call and no job-lock wait to progress.
    assert state.gate.hold_started_at is not None
    assert state.gate.released_at is not None
    assert state.gate.calls_at_hold == 1
    assert state.gate.calls_at_release == 1
    assert state.gate.hold_started_at > call1_time
    assert state.gate.released_at > state.gate.original_expiry
    assert state.gate.calls_at_release < len(state.calls)
    # At least one committed renewal landed inside the hold window: the held worker
    # kept its lease without another provider call or a job-lock wait.
    assert state.gate.renewals_at_release > state.gate.renewals_at_hold

    renewals = state.job_renewals()
    assert len(renewals) >= 1
    for renewal in renewals:
        job = renewal["result"]["job"]
        assert job["worker_id"] == state.gate.worker_id
        assert job["fence"] == state.gate.fence
        assert job["job_id"] == str(grant_payload["job_id"])

    # 4. Exactly-once charges: one measured usage event/spend record/receipt per
    # consumed call, correlated by settlement identity and priced against the
    # configured rates. 10 input tokens at 0.30 USD/M plus 5 output tokens at
    # 2.50 USD/M is 0.0000155 per call and 0.000031 total. Equality across
    # ledgers alone would permit consistently wrong pricing; these are concrete
    # expected amounts.
    usage_events = state.usage_events()
    spend_records = state.spend_records()
    receipts = state.receipts()

    assert len(usage_events) == 2
    assert len(spend_records) == 2
    assert len(receipts) == 2

    seen_spend: set[str] = set()
    seen_usage: set[str] = set()
    for receipt in receipts:
        settlement = receipt["result"]["settlement"]
        assert settlement["measurement"] == "measured"
        assert settlement["model"] == "gemini-3.8-flash-high"
        assert settlement["input_tokens"] == 10
        assert settlement["output_tokens"] == 5
        assert settlement["cache_tokens"] == 0
        assert Decimal(str(settlement["actual_usd"])) == _PER_CALL_USD

        # Each receipt carries the settlement identity that keys its spend row
        # (record_id = identity[:16] bytes) and its usage event (request_id).
        identity = receipt["operation_id"].removeprefix("model_call:receipt:")
        assert len(identity) == 64
        record_id = str(UUID(bytes=bytes.fromhex(identity[:32])))
        matching_spend = [row for row in spend_records if str(row["record_id"]) == record_id]
        matching_usage = [
            event for event in usage_events
            if event["request_id"] == f"model_call:request:{identity}"
        ]
        assert len(matching_spend) == 1
        assert len(matching_usage) == 1
        seen_spend.add(record_id)
        seen_usage.add(identity)

        event = matching_usage[0]
        assert event["measurement"] == "measured"
        assert event["role_derived"] == "model_call"
        assert event["model"] == "gemini-3.8-flash-high"
        assert event["input_tokens"] == 10
        assert event["output_tokens"] == 5
        assert Decimal(str(event["price_usd"])) == _PER_CALL_USD

        record = matching_spend[0]
        assert Decimal(str(record["amount_usd"])) == _PER_CALL_USD
        assert str(record["project_id"]) == str(state.world.project_id)
        assert str(record["mission_id"]) == str(state.world.mission_id)

    assert len(seen_spend) == 2
    assert len(seen_usage) == 2

    total_usage_usd = sum(
        (Decimal(str(event["price_usd"])) for event in usage_events), Decimal()
    )
    total_spend_usd = sum(
        (Decimal(str(record["amount_usd"])) for record in spend_records), Decimal()
    )
    total_receipt_usd = sum(
        (Decimal(str(r["result"]["settlement"]["actual_usd"])) for r in receipts), Decimal()
    )
    assert total_usage_usd == total_spend_usd == total_receipt_usd == _TOTAL_USD

    total_tokens = sum(
        event["input_tokens"] + event["output_tokens"] for event in usage_events
    )
    assert total_tokens == 30

    # 5. One grant and final revocation
    assert len(state.grants) == 1
    assert len(state.revocations) == 1
    revoke_http = state.revocations[0]
    assert revoke_http.path == f"/tasks/{grant['id']}"
    assert revoke_http.status == 200
    assert dict(revoke_http.headers)[b"authorization"] == f"Bearer {CONTROL_TOKEN}".encode()
    revocation = json.loads(revoke_http.response_body)
    assert revocation["refused"] is False
    assert Decimal(revocation["spent_usd"]) == _TOTAL_USD

    # The job settled terminal for the same worker and fence that renewed it.
    job_row = state.job_row()
    assert job_row is not None
    assert job_row["status"] == "sealed"
    assert job_row["worker_id"] == state.gate.worker_id
    assert job_row["fence"] == state.gate.fence

    # 6. Post-exit token probe adds no provider call/charge
    with httpx.Client(base_url=state.proxy_url, timeout=30) as client:
        probe_resp = client.post(
            "/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
            json={"contents": [{"parts": [{"text": "probe"}]}]},
            headers={"Authorization": f"Bearer {model_token}"},
        )
        assert probe_resp.status_code == 403
        assert probe_resp.json()["detail"] == "grant_refused"

        probe_resp2 = client.post(
            "/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
            json={"contents": [{"parts": [{"text": "probe2"}]}]},
            headers={"x-goog-api-key": model_token},
        )
        assert probe_resp2.status_code == 403
        assert probe_resp2.json()["detail"] == "grant_refused"

    assert len(state.calls) == 2
    assert len(state.usage_events()) == 2
    assert len(state.spend_records()) == 2
    assert len(state.receipts()) == 2
    assert sum(event["price_usd"] for event in state.usage_events()) == total_usage_usd
    assert (
        sum(record["amount_usd"] for record in state.spend_records()) == total_spend_usd
    )
