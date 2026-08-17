from __future__ import annotations

import hmac
import json
import stat
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import collect_health
from .api_models import CommandResponse
from .models import CommandEnvelope
from .service import Principal, WorkError, WorkService
from .store import PostgresWorkStore, WorkStore


def _principal(request: Request, capabilities_dir: Path) -> Principal:
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "unauthenticated")
    token = authorization[7:]
    try:
        if stat.S_IMODE(capabilities_dir.stat().st_mode) != 0o700:
            raise ValueError
        for path in capabilities_dir.iterdir():
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                continue
            data = json.loads(path.read_text())
            if hmac.compare_digest(str(data["token"]), token):
                candidate_ids = frozenset(UUID(value) for value in data.get("candidate_ids", ())) or None
                scopes = frozenset(data["scopes"])
                if "work.candidate.read" in scopes and candidate_ids is None:
                    continue
                return Principal(actor_id=UUID(data["actor_id"]), actor_kind=str(data["actor_kind"]), workspaces=frozenset(UUID(value) for value in data["workspaces"]), scopes=scopes, candidate_ids=candidate_ids)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    raise HTTPException(401, "unauthenticated")


def _error(error: WorkError, request_id: UUID, correlation_id: UUID) -> JSONResponse:
    return JSONResponse({"error": {"code": error.code, "request_id": str(request_id), "correlation_id": str(correlation_id), "diagnostics": list(error.diagnostics[:8])}}, status_code=error.status)


def create_app(config: OperationsConfig, *, capabilities_dir: Path, store: WorkStore | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": {"code": "invalid_request", "request_id": None, "correlation_id": None, "diagnostics": []}}, status_code=400)
    service = WorkService(store or PostgresWorkStore(config))

    @app.get("/v1/health/live")
    def live() -> dict[str, object]:
        return {"live": collect_health(config, role="omp_work_app").live, "ready": False, "alerts": []}

    @app.get("/v1/health/ready")
    def ready() -> dict[str, object]:
        report = collect_health(config, role="omp_work_app")
        return {"live": report.live, "ready": report.ready, "alerts": report.alerts}

    def read_route(request: Request, workspace_id: UUID, kind: str, value: str) -> JSONResponse:
        try:
            principal = _principal(request, capabilities_dir)
            return JSONResponse(jsonable_encoder(service.read(principal, workspace_id, kind, value)))
        except WorkError as error:
            return JSONResponse({"error": {"code": error.code, "request_id": None, "correlation_id": None, "diagnostics": list(error.diagnostics[:8])}}, status_code=error.status)

    @app.get("/v1/work-items/{key}")
    def item(request: Request, key: str, x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID")) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "item", key)

    @app.get("/v1/work-items/{key}/workflow")
    def workflow(request: Request, key: str, x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID")) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "workflow", key)

    @app.get("/v1/workspaces/{workspace_id}/tree")
    def tree(request: Request, workspace_id: UUID) -> JSONResponse:
        return read_route(request, workspace_id, "tree", "")

    @app.get("/v1/workspaces/{workspace_id}/focus/{owner_id}")
    def focus(request: Request, workspace_id: UUID, owner_id: UUID) -> JSONResponse:
        return read_route(request, workspace_id, "focus", str(owner_id))

    @app.get("/v1/operations/{operation_id}")
    def operation(request: Request, operation_id: UUID, x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID")) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "operation", str(operation_id))
    @app.post("/v1/commands")
    async def command(request: Request) -> JSONResponse:
        envelope: CommandEnvelope | None = None
        try:
            envelope = CommandEnvelope.model_validate(await request.json())
            principal = _principal(request, capabilities_dir)
            receipt, result = service.execute(principal, envelope)
            response = CommandResponse.model_validate({"receipt": receipt, "result": result})
            return JSONResponse(response.model_dump(mode="json"))
        except WorkError as error:
            if envelope is None:
                return JSONResponse({"error": {"code": error.code, "request_id": None, "correlation_id": None, "diagnostics": list(error.diagnostics[:8])}}, status_code=error.status)
            return _error(error, envelope.request_id, envelope.correlation_id)
        except HTTPException as error:
            return JSONResponse({"error": {"code": error.detail, "request_id": None, "correlation_id": None, "diagnostics": []}}, status_code=error.status_code)
        except Exception:
            return JSONResponse({"error": {"code": "invalid_request", "request_id": None, "correlation_id": None, "diagnostics": []}}, status_code=400)

    return app
