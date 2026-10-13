"""Single-process, memory-only model grants on an IPv4 loopback listener."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import math
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from omp_work.jobs.budget import item_budget, item_spend, root_work_id
from omp_work.jobs.lease import current_authority
from omp_work.jobs.store import NativeJobStore
from omp_work.model_proxy_budget import ModelProxyBudget, ModelProxyBudgetError
from omp_work.model_proxy_upstream import (
    GEMINI, OLLAMA, ModelProxyUpstream, ModelRoute, Transport, UpstreamRefused,
    prepare, validate_route, UpstreamResult, Prepared,
)
from omp_work.operations.config import OperationsConfig
from omp_work.routing.policy import load_policy, RoutingRefused
from omp_work.routing.router import route_stage
from omp_work.v1.agent_stop import read_stop_state
from omp_work.v1.server import _capability_expired
from omp_work.v1.store import PostgresWorkStore


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    project_id: UUID
    mission_id: UUID | None = None
    work_id: UUID
    job_id: str = Field(min_length=1)
    worker_id: str = Field(min_length=1)
    fence: int = Field(ge=0, strict=True)
    stage: Literal["implement"]
    expires_at: datetime
    ceiling_usd: Decimal = Field(gt=0, allow_inf_nan=False)


@dataclass(frozen=True)
class ProxyConfig:
    operations: OperationsConfig
    control_capability: Path
    transports: dict[str, ModelRoute]
    host: str = "127.0.0.1"
    port: int = 8091
    max_request_bytes: int = 1024 * 1024
    max_response_bytes: int = 4 * 1024 * 1024
    timeout_s: float = 120

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("loopback_required")
        if not 0 < self.port < 65536 or min(self.max_request_bytes, self.max_response_bytes, self.timeout_s) <= 0 or not math.isfinite(self.timeout_s):
            raise ValueError("invalid_limits")
        policy = load_policy()
        primary = route_stage(policy, "implement")
        routes = [primary, route_stage(policy, "implement", provider_in_flight={primary.provider: policy.concurrency.providers[primary.provider]})]
        for selected in routes:
            route = self.transports[selected.provider]
            validate_route(route)
            if route.model != selected.model or route.api != (GEMINI if selected.provider == "gemini_flash" else OLLAMA):
                raise ValueError("invalid_transport_map")

    @classmethod
    def load(cls, path: Path) -> ProxyConfig:
        data = json.loads(path.read_text())
        operations = data.pop("operations")
        for name in ("config_dir", "state_dir", "data_dir"):
            operations[name] = Path(operations[name])
        transports = data.pop("transports")
        for route in transports.values():
            for name in ("input_usd", "output_usd", "cache_usd"):
                route[name] = Decimal(str(route[name]))
        config = cls(operations=OperationsConfig(**operations), control_capability=Path(data.pop("control_capability")), transports={k: ModelRoute(**v) for k, v in transports.items()}, **data)
        if not config.control_capability.is_file() or stat.S_IMODE(config.control_capability.stat().st_mode) != 0o600 or config.control_capability.stat().st_uid != os.getuid():
            raise ValueError("unsafe_capability_permissions")
        for route in config.transports.values():
            config.operations.read_secret(route.key_file)
        return config


@dataclass
class Grant:
    task: TaskRequest
    provider: str
    actor: UUID
    route: ModelRoute
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    refused: bool = False
    revoked: bool = False
    spent: Decimal = Decimal(0)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def create_app(config: ProxyConfig, *, transport: Transport | None = None) -> FastAPI:
    # One app/registry per process. No persistent token or output cache.
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    policy = load_policy()
    jobs = NativeJobStore(config.operations)
    projects = PostgresWorkStore(config.operations)
    budget = ModelProxyBudget(config.operations, jobs=jobs, projects=projects)
    upstream = ModelProxyUpstream(config.operations, transport)
    grants: dict[str, Grant] = {}
    registry_lock = threading.RLock()
    counts: dict[str, int] = {}

    @app.exception_handler(Exception)
    async def internal_error(request, error):
        return JSONResponse({"detail": "proxy_refused"}, status_code=403)

    def control(request: Request) -> dict:
        try:
            path = config.control_capability
            metadata = path.stat()
            if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid() or not stat.S_ISREG(metadata.st_mode):
                raise ValueError
            capability = json.loads(path.read_text())
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if (not request.headers.get("authorization", "").startswith("Bearer ")
                or not hmac.compare_digest(token, capability["token"])
                or capability["actor_kind"] != "automation"
                or "work.execute" not in capability.get("scopes", ())
                or _capability_expired(capability.get("expires_at"), datetime.now(UTC))):
                raise ValueError
            UUID(capability["actor_id"])
            if not isinstance(capability["workspaces"], list):
                raise ValueError
            capability["workspaces"] = [str(UUID(value)) for value in capability["workspaces"]]
            return capability
        except (OSError, ValueError, KeyError, TypeError):
            raise HTTPException(401, "unauthenticated") from None

    def refuse(grant: Grant, code: str) -> None:
        with registry_lock:
            grant.refused = True
        raise HTTPException(403, code)

    def active(grant: Grant) -> None:
        if grant.refused or grant.revoked or grant.task.expires_at <= datetime.now(UTC):
            refuse(grant, "grant_refused")

    def authority(task: TaskRequest, cur) -> None:
        lease = current_authority(jobs, task.workspace_id, task.job_id, task.worker_id, task.fence, cur=cur)
        if not lease.is_live:
            raise ModelProxyBudgetError("agent_stop_engaged" if lease.is_frozen else "job_fence_stale")
        cur.execute("SELECT work_id FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s", (task.workspace_id, task.job_id))
        row = cur.fetchone()
        if row is None or row["work_id"] != task.work_id:
            raise ModelProxyBudgetError("job_work_mismatch")
        if read_stop_state(cur, task.workspace_id)["stopped"]:
            raise ModelProxyBudgetError("agent_stop_engaged")

    async def body(request: Request, grant: Grant | None = None) -> bytes:
        data = bytearray()
        try:
            async with asyncio.timeout(config.timeout_s):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data) > config.max_request_bytes:
                        raise ValueError
        except (ValueError, TimeoutError):
            if grant is not None:
                refuse(grant, "request_limit")
            raise HTTPException(413, "request_limit") from None
        return bytes(data)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"detail": "invalid_request"}, status_code=400)

    def register(task: TaskRequest, capability: dict) -> dict:
        actor = UUID(capability["actor_id"])
        if str(task.workspace_id) not in capability["workspaces"]:
            raise HTTPException(403, "workspace_refused")
        now = datetime.now(UTC)
        if task.expires_at.tzinfo is None or task.expires_at <= now:
            raise HTTPException(403, "expiry_refused")
        try:
            with jobs.transaction(task.workspace_id, actor) as cur:
                authority(task, cur)
                lease = current_authority(jobs, task.workspace_id, task.job_id, task.worker_id, task.fence, cur=cur)
                if task.expires_at > lease.expires_at:
                    raise ModelProxyBudgetError("expiry_refused")
                if task.mission_id is not None:
                    cur.execute("SELECT project_id FROM omp_work.project_missions WHERE workspace_id=%s AND mission_id=%s", (task.workspace_id, task.mission_id))
                    row = cur.fetchone()
                    if row is None or row["project_id"] != task.project_id:
                        raise ModelProxyBudgetError("mission_refused")
                project_b, _ = projects._load_budget(cur, task.workspace_id, task.project_id, None)
                mission_b = projects._load_budget(cur, task.workspace_id, task.project_id, task.mission_id)[0] if task.mission_id else None
                budgets = [b for b in (project_b, mission_b) if b is not None]
                if not budgets:
                    raise ModelProxyBudgetError("budget_decision_required")
                spent = projects._spent_by_budget(cur, task.workspace_id, task.project_id, task.mission_id, mission_b, project_b)
                remaining = min(b.ceiling_usd - spent[b.budget_id] for b in budgets)
                root = root_work_id(cur, task.workspace_id, task.job_id)
                item_b = item_budget(cur, task.workspace_id, root)
                item_used = item_spend(cur, task.workspace_id, root).usd
                if item_b is not None:
                    remaining = min(remaining, Decimal(item_b.usd) - item_used)
                # s01 task ceilings include already recorded work spend.
                work_used = item_spend(cur, task.workspace_id, task.work_id).usd
                if task.ceiling_usd <= work_used or task.ceiling_usd - work_used > remaining:
                    raise ModelProxyBudgetError("ceiling_refused")
        except ModelProxyBudgetError as error:
            raise HTTPException(403, error.code) from None
        with registry_lock:
            for key, old in list(grants.items()):
                if old.task.expires_at <= now and not old.lock.locked():
                    del grants[key]
            selected = route_stage(policy, "implement", provider_in_flight=counts)
            route = replace(config.transports[selected.provider], max_response_bytes=min(config.max_response_bytes, config.transports[selected.provider].max_response_bytes), timeout_s=min(config.timeout_s, config.transports[selected.provider].timeout_s))
            grant = Grant(task, selected.provider, actor, route)
            task_id = str(uuid4())
            grants[task_id] = grant
        return {"id": task_id, "model_token": grant.token, "descriptor": {"api": route.api, "id": route.model, "limits": {"context_tokens": route.context_tokens, "max_output_tokens": route.max_output_tokens}, "baseUrl": f"http://127.0.0.1:{config.port}"}}

    @app.post("/tasks")
    async def tasks(request: Request):
        capability = control(request)
        try:
            task = TaskRequest.model_validate_json(await body(request))
        except ValidationError:
            raise HTTPException(400, "invalid_request") from None
        try:
            return await run_in_threadpool(register, task, capability)
        except RoutingRefused as error:
            raise HTTPException(403, error.code) from None

    @app.delete("/tasks/{task_id}")
    def revoke(task_id: str, request: Request):
        capability = control(request)
        with registry_lock:
            grant = grants.get(task_id)
            if grant is None:
                raise HTTPException(404, "unknown_task")
            if str(grant.task.workspace_id) not in capability["workspaces"]:
                raise HTTPException(403, "workspace_refused")
            grant.revoked = True
        # Wait until any incurred spend is settled, then report it.
        with grant.lock:
            return {"refused": grant.refused, "spent_usd": str(grant.spent)}

    def invoke(grant: Grant, payload: bytes, path: str, query: str) -> Response:
        deadline = time.monotonic() + config.timeout_s
        with grant.lock:
            active(grant)
            route = grant.route
            if route.api == GEMINI:
                # The worker posts to `{baseUrl}/models/{id}:streamGenerateContent`
                # with no version, `google.ts` uses `v1beta/models/...`; both
                # spellings of the same generate-content route are accepted.
                allowed = frozenset((
                    f"models/{route.model}:streamGenerateContent",
                    f"v1beta/models/{route.model}:streamGenerateContent",
                ))
                expected_query = ("", "alt=sse")
            else:
                allowed = frozenset(("api/chat",))
                expected_query = ("",)
            if path not in allowed or query not in expected_query:
                refuse(grant, "route_mismatch")
            with registry_lock:
                if counts.get(grant.provider, 0) >= policy.concurrency.providers[grant.provider] or sum(counts.values()) >= policy.concurrency.in_flight_max:
                    refuse(grant, "provider_saturated")
                counts[grant.provider] = counts.get(grant.provider, 0) + 1
            try:
                prepared = prepare(route, payload)
                task = grant.task
                actor = grant.actor
                request_id = str(uuid4())

                class GrantUpstream(ModelProxyUpstream):
                    def __init__(self) -> None:
                        self.result: UpstreamResult | None = None

                    def invoke(self, prepared: Prepared) -> UpstreamResult:
                        try:
                            active(grant)
                        except HTTPException:
                            raise UpstreamRefused("grant_refused") from None
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise UpstreamRefused("request_timeout")
                        prepared = replace(prepared, route=replace(prepared.route, timeout_s=min(prepared.route.timeout_s, remaining)))
                        self.result = upstream.invoke(prepared)
                        return self.result

                guarded = GrantUpstream()
                try:
                    receipt = budget.call(task.workspace_id, actor, task.project_id, task.mission_id, task.work_id, task.job_id, task.worker_id, task.fence, request_id, task.ceiling_usd, prepared, guarded)
                    grant.spent += Decimal(str(receipt["actual_usd"]))
                except Exception:
                    # s01 can charge the bound and still refuse. Read that
                    # call's durable charge, never infer success from output.
                    with jobs.transaction(task.workspace_id, actor) as cur:
                        cur.execute("SELECT COALESCE(sum((result->'settlement'->>'actual_usd')::numeric),0) AS usd FROM omp_jobs.operations WHERE workspace_id=%s AND kind='model_call_receipt' AND result->'fingerprint'->>'request_id'=%s", (task.workspace_id, request_id))
                        # COALESCE yields a numeric when charged and an int 0 otherwise.
                        grant.spent += Decimal(str(cur.fetchone()["usd"]))
                    raise
                result = guarded.result
                if result is None:
                    raise AssertionError
                with jobs.transaction(task.workspace_id, actor) as cur:
                    authority(task, cur)
                active(grant)
                if time.monotonic() > deadline:
                    refuse(grant, "request_timeout")
                for secret in (*[config.operations.read_secret(r.key_file) for r in config.transports.values()], grant.token, json.loads(config.control_capability.read_text())["token"]):
                    if secret.encode() in result.body:
                        refuse(grant, "unsafe_response")
                return Response(result.body, media_type="text/event-stream" if route.api == GEMINI else "application/x-ndjson")
            except (ModelProxyBudgetError, UpstreamRefused) as error:
                refuse(grant, error.code if isinstance(error, ModelProxyBudgetError) else error.reason)
            except HTTPException:
                raise
            except Exception:
                refuse(grant, "proxy_refused")
            finally:
                with registry_lock:
                    counts[grant.provider] -= 1

    @app.post("/{path:path}")
    async def native(path: str, request: Request):
        # A Bearer authorization is the credential; the Gemini worker instead
        # sends its token in x-goog-api-key. Anything else is unauthenticated.
        authorization = request.headers.get("authorization", "")
        credential = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else request.headers.get("x-goog-api-key")
        with registry_lock:
            grant = next((g for g in grants.values() if credential is not None and hmac.compare_digest(g.token, credential)), None)
        if grant is None:
            raise HTTPException(401, "unauthenticated")
        active(grant)
        return await run_in_threadpool(invoke, grant, await body(request, grant), path, request.url.query)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = ProxyConfig.load(args.config)
        app = create_app(config)
    except Exception:
        parser.exit(2, "invalid proxy configuration\n")
    uvicorn.run(app, host=config.host, port=config.port, workers=1, access_log=False, log_level="critical")


if __name__ == "__main__":
    main()
