from __future__ import annotations

import hmac
import json
import stat
from pathlib import Path
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from omp_work import contract_sha256
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import collect_health, migration_set_sha256
from omp_work.operations.fingerprints import (
    code_fingerprint,
    service_runtime_fingerprint,
)

from .api_models import CommandResponse
from .models import CommandEnvelope, SetExecutionStateCommand
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
                candidate_ids = (
                    frozenset(UUID(value) for value in data.get("candidate_ids", ()))
                    or None
                )
                scopes = frozenset(data["scopes"])
                if "work.candidate.read" in scopes and candidate_ids is None:
                    continue
                return Principal(
                    actor_id=UUID(data["actor_id"]),
                    actor_kind=str(data["actor_kind"]),
                    workspaces=frozenset(UUID(value) for value in data["workspaces"]),
                    scopes=scopes,
                    candidate_ids=candidate_ids,
                )
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        pass
    raise HTTPException(401, "unauthenticated")


def _error(error: WorkError, request_id: UUID, correlation_id: UUID) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "code": error.code,
                "request_id": str(request_id),
                "correlation_id": str(correlation_id),
                "diagnostics": list(error.diagnostics[:8]),
            }
        },
        status_code=error.status,
    )


def _require_contract(request: Request, service_digest: str) -> None:
    """OMP-143 fail-first handshake: refuse a stale or headerless host BEFORE
    parsing a command body or authenticating — no command, budget, event, or
    idempotency row is ever written for a mismatched contract."""
    host_digest = request.headers.get("x-omp-contract-sha256")
    if host_digest != service_digest:
        raise WorkError(
            "contract_mismatch",
            status=409,
            diagnostics=(
                f"host contract digest: {host_digest or 'missing'}",
                f"service contract digest: {service_digest}",
                "restart the OMP session",
            ),
        )


def create_app(
    config: OperationsConfig, *, capabilities_dir: Path, store: WorkStore | None = None
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "invalid_request",
                    "request_id": None,
                    "correlation_id": None,
                    "diagnostics": [str(err) for err in exc.errors()[:8]],
                }
            },
            status_code=400,
        )

    service = WorkService(store or PostgresWorkStore(config))
    # OMP-89: writes fail closed when the on-disk source or migration set no
    # longer matches what this process loaded — an editable install can change
    # under a running service, and a stale service burns bounded budgets or
    # writes against missing migrations. Startup migration enforcement plus
    # this disk-snapshot compare make silent stale serving unreachable.
    source_snapshot = code_fingerprint() + migration_set_sha256()
    # OMP-143: the digest this service process loaded — compared against every
    # authenticated request's X-OMP-Contract-SHA256 before any other work.
    service_digest = contract_sha256()

    @app.get("/v1/health/live")
    def live() -> dict[str, object]:
        return {
            "live": collect_health(config, role="omp_work_app").live,
            "ready": False,
            "alerts": [],
        }

    @app.get("/v1/health/ready")
    def ready() -> dict[str, object]:
        report = collect_health(config, role="omp_work_app")
        return {
            "live": report.live,
            "ready": report.ready,
            "alerts": report.alerts,
            "service_fingerprint": service_runtime_fingerprint(),
        }

    def read_route(
        request: Request, workspace_id: UUID, kind: str, value: str
    ) -> JSONResponse:
        try:
            _require_contract(request, service_digest)
            principal = _principal(request, capabilities_dir)
            return JSONResponse(
                jsonable_encoder(service.read(principal, workspace_id, kind, value))
            )
        except WorkError as error:
            return JSONResponse(
                {
                    "error": {
                        "code": error.code,
                        "request_id": None,
                        "correlation_id": None,
                        "diagnostics": list(error.diagnostics[:8]),
                    }
                },
                status_code=error.status,
            )

    @app.get("/v1/work-items/{key}")
    def item(
        request: Request,
        key: str,
        x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID"),
    ) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "item", key)

    @app.get("/v1/work-items/{key}/workflow")
    def workflow(
        request: Request,
        key: str,
        x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID"),
    ) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "workflow", key)

    @app.get("/v1/workspaces/{workspace_id}/tree")
    def tree(request: Request, workspace_id: UUID) -> JSONResponse:
        return read_route(request, workspace_id, "tree", "")

    @app.get("/v1/workspaces/{workspace_id}/focus/{owner_id}")
    def focus(request: Request, workspace_id: UUID, owner_id: UUID) -> JSONResponse:
        return read_route(request, workspace_id, "focus", str(owner_id))

    @app.get("/v1/workspaces/{workspace_id}/authority")
    def authority(request: Request, workspace_id: UUID) -> JSONResponse:
        return read_route(request, workspace_id, "authority", "")

    @app.get("/v1/workspaces/{workspace_id}/execution")
    @app.get("/v1/workspaces/{workspace_id}/execution/{grant_id}")
    def execution(request: Request, workspace_id: UUID, grant_id: str = "") -> JSONResponse:
        return read_route(request, workspace_id, "execution", grant_id)

    @app.get("/v1/workspaces/{workspace_id}/activity")
    def activity(
        request: Request,
        workspace_id: UUID,
        project_id: UUID | None = None,
        limit: int = Query(8, ge=1, le=20),
    ) -> JSONResponse:
        try:
            _require_contract(request, service_digest)
            principal = _principal(request, capabilities_dir)
            return JSONResponse(
                jsonable_encoder(
                    service.activity(
                        principal, workspace_id, project_id=project_id, limit=limit
                    )
                )
            )
        except WorkError as error:
            return JSONResponse(
                {
                    "error": {
                        "code": error.code,
                        "request_id": None,
                        "correlation_id": None,
                        "diagnostics": list(error.diagnostics[:8]),
                    }
                },
                status_code=error.status,
            )

    @app.get("/v1/operations/{operation_id}")
    def operation(
        request: Request,
        operation_id: UUID,
        x_omp_workspace_id: UUID = Header(alias="X-OMP-Workspace-ID"),
    ) -> JSONResponse:
        return read_route(request, x_omp_workspace_id, "operation", str(operation_id))

    @app.post("/v1/commands")
    async def command(request: Request) -> JSONResponse:
        envelope: CommandEnvelope | None = None
        try:
            # OMP-143: the handshake runs BEFORE the body is parsed — a retired
            # stale host never reaches discriminator validation.
            _require_contract(request, service_digest)
            envelope = CommandEnvelope.model_validate(await request.json())
            principal = _principal(request, capabilities_dir)
            if code_fingerprint() + migration_set_sha256() != source_snapshot:
                is_service_refresh = (
                    isinstance(envelope.command, SetExecutionStateCommand)
                    and envelope.command.payload.target_state == "active"
                    and envelope.command.payload.reason == "service_refresh"
                )
                if not is_service_refresh:
                    raise WorkError(
                        "unavailable",
                        status=503,
                        diagnostics=(
                            "service_stale: on-disk source or migration set changed since this service started (OMP-89)",
                            "apply pending migrations, then restart the work service (python -m omp_work serve) — no command was executed and no budget was spent",
                        ),
                    )
            receipt, result = service.execute(principal, envelope)
            response = CommandResponse.model_validate(
                {"receipt": receipt, "result": result}
            )
            return JSONResponse(response.model_dump(mode="json"))
        except WorkError as error:
            if envelope is None:
                return JSONResponse(
                    {
                        "error": {
                            "code": error.code,
                            "request_id": None,
                            "correlation_id": None,
                            "diagnostics": list(error.diagnostics[:8]),
                        }
                    },
                    status_code=error.status,
                )
            return _error(error, envelope.request_id, envelope.correlation_id)
        except HTTPException as error:
            return JSONResponse(
                {
                    "error": {
                        "code": error.detail,
                        "request_id": None,
                        "correlation_id": None,
                        "diagnostics": [],
                    }
                },
                status_code=error.status_code,
            )
        except Exception as ex:
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_request",
                        "request_id": None,
                        "correlation_id": None,
                        "diagnostics": [f"{type(ex).__name__}: {ex}"],
                    }
                },
                status_code=400,
            )

    return app
