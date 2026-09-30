"""OMP-416: the seven monitoring-client GET reads.

Each route authenticates, calls ``WorkService.client_read``, and returns
``client_body``. A ``WorkError`` becomes ``client_error_body``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .client_response import client_body, client_error_body
from .service import Principal, WorkError, WorkService

_CLIENT = "/v1/workspaces/{workspace_id}/client"


def register_client_reads(
    app: FastAPI,
    service: WorkService,
    *,
    authenticate: Callable[[Request], Principal],
) -> None:
    """Register the seven client GETs. ``authenticate`` returns the principal."""

    def read(
        request: Request,
        workspace_id: UUID,
        operation: str,
        ident: UUID | str | None,
        detail: bool,
    ) -> JSONResponse:
        try:
            principal = authenticate(request)
            view = service.client_read(principal, workspace_id, operation, ident)
            state, evidence, decisions = _projection(operation, view, ident)
            return JSONResponse(
                client_body(
                    operation,
                    result=view,
                    detail=detail,
                    state=state,
                    evidence=evidence,
                    decisions=decisions,
                )
            )
        except WorkError as error:
            return JSONResponse(
                client_error_body(
                    error.code,
                    error.diagnostics[:8],
                    request_id=None,
                    correlation_id=None,
                    detail=detail,
                ),
                status_code=error.status,
            )

    @app.get(f"{_CLIENT}/projects")
    def client_projects(
        request: Request, workspace_id: UUID, detail: bool = False
    ) -> JSONResponse:
        return read(request, workspace_id, "project.list", None, detail)

    @app.get(f"{_CLIENT}/projects/{{project_id}}/context")
    def client_project_context(
        request: Request,
        workspace_id: UUID,
        project_id: UUID,
        detail: bool = False,
    ) -> JSONResponse:
        return read(request, workspace_id, "project.context", project_id, detail)

    @app.get(f"{_CLIENT}/projects/{{project_id}}/status")
    def client_project_status(
        request: Request,
        workspace_id: UUID,
        project_id: UUID,
        detail: bool = False,
    ) -> JSONResponse:
        return read(request, workspace_id, "project.status", project_id, detail)

    @app.get(f"{_CLIENT}/projects/{{project_id}}/decisions")
    def client_project_decisions(
        request: Request,
        workspace_id: UUID,
        project_id: UUID,
        detail: bool = False,
    ) -> JSONResponse:
        return read(request, workspace_id, "project.decisions", project_id, detail)

    @app.get(f"{_CLIENT}/missions/{{mission_id}}")
    def client_mission(
        request: Request,
        workspace_id: UUID,
        mission_id: str,
        detail: bool = False,
    ) -> JSONResponse:
        return read(request, workspace_id, "mission.status", mission_id, detail)

    @app.get(f"{_CLIENT}/evidence/{{receipt_id}}")
    def client_evidence(
        request: Request,
        workspace_id: UUID,
        receipt_id: str,
        detail: bool = False,
    ) -> JSONResponse:
        return read(request, workspace_id, "evidence.inspect", receipt_id, detail)

    @app.get(f"{_CLIENT}/stop")
    def client_stop(
        request: Request, workspace_id: UUID, detail: bool = False
    ) -> JSONResponse:
        return read(request, workspace_id, "stop.status", None, detail)


def _projection(
    operation: str, view: dict[str, Any], ident: UUID | str | None
) -> tuple[str | None, tuple[str, ...], tuple[UUID, ...]]:
    """state, evidence, and decisions for one client read.

    Mission state is the mission's status. Stop state is stopped or running.
    Project decisions contribute their decision ids. Evidence inspect cites
    the requested receipt. Every other read leaves those fields empty.
    """
    if operation == "mission.status":
        status = view.get("status")
        return (None if status is None else str(status), (), ())
    if operation == "stop.status":
        return ("stopped" if view.get("stopped") else "running", (), ())
    if operation == "evidence.inspect":
        evidence = () if ident is None else (str(ident),)
        return (None, evidence, ())
    if operation == "project.decisions":
        return (None, (), _decision_ids(view))
    return (None, (), ())


def _decision_ids(view: dict[str, Any]) -> tuple[UUID, ...]:
    rows = view.get("decisions")
    if not isinstance(rows, (list, tuple)):
        return ()
    ids: list[UUID] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("decision_id")
        if isinstance(value, UUID):
            ids.append(value)
        elif value is not None:
            ids.append(UUID(str(value)))
    return tuple(ids)
