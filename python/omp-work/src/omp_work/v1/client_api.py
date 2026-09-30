"""OMP-416: the client contract on HTTP — seven GET reads and ten POST mutations.

Each route authenticates (contract header, then principal), calls the service,
and returns ``client_body``. A ``WorkError`` becomes ``client_error_body``.
Mutations carry ``{request_id, payload}``; ``WorkService.execute_client``
builds the envelope and owns the rest of the checks.
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
# Receipt state -> ClientResponse outcome. A rejected operation never returns
# a body; keeping the entry maps any future state instead of raising.
_OUTCOMES = {
    "applied": "applied",
    "replayed": "replayed",
    "pending_approval": "pending_approval",
    "rejected": "applied",
}


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


def register_client_mutations(
    app: FastAPI,
    service: WorkService,
    *,
    authenticate: Callable[[Request], Principal],
) -> None:
    """Register the ten client POSTs. ``authenticate`` returns the principal."""

    def mutate(
        request: Request,
        workspace_id: UUID,
        operation: str,
        body: dict[str, Any],
        detail: bool,
        path_ident: UUID | None,
    ) -> JSONResponse:
        try:
            principal = authenticate(request)
        except WorkError as error:
            return JSONResponse(
                client_error_body(
                    error.code,
                    error.diagnostics[:8],
                    detail=detail,
                ),
                status_code=error.status,
            )
        try:
            request_id = UUID(str(body.get("request_id")))
        except (ValueError, TypeError):
            return JSONResponse(
                client_error_body("invalid_request", detail=detail),
                status_code=400,
            )
        try:
            receipt, result = service.execute_client(
                principal,
                workspace_id,
                operation,
                request_id,
                body.get("payload"),
                path_ident,
            )
            outcome = _OUTCOMES.get(str(getattr(receipt, "state", "")), "applied")
            return JSONResponse(
                client_body(
                    operation,
                    result=result,
                    detail=detail,
                    outcome=outcome,  # type: ignore[arg-type]
                )
            )
        except WorkError as error:
            return JSONResponse(
                client_error_body(
                    error.code,
                    error.diagnostics[:8],
                    request_id=request_id,
                    correlation_id=request_id,
                    detail=detail,
                ),
                status_code=error.status,
            )

    @app.post(f"{_CLIENT}/missions")
    def client_mission_submit(
        request: Request,
        workspace_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(request, workspace_id, "mission.submit", body, detail, None)

    @app.post(f"{_CLIENT}/mission-intake")
    def client_mission_intake(
        request: Request,
        workspace_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(request, workspace_id, "mission.intake", body, detail, None)

    @app.post(f"{_CLIENT}/stop")
    def client_stop_engage(
        request: Request,
        workspace_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(request, workspace_id, "stop.engage", body, detail, None)

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/pause")
    def client_mission_pause(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "mission.pause", body, detail, mission_id
        )

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/resume")
    def client_mission_resume(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "mission.resume", body, detail, mission_id
        )

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/cancel")
    def client_mission_cancel(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "mission.cancel", body, detail, mission_id
        )

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/priority")
    def client_mission_reprioritise(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "mission.reprioritise", body, detail, mission_id
        )

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/scope/confirm")
    def client_mission_scope_confirm(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request,
            workspace_id,
            "mission.scope.confirm",
            body,
            detail,
            mission_id,
        )

    @app.post(f"{_CLIENT}/missions/{{mission_id}}/scope/edit")
    def client_mission_scope_edit(
        request: Request,
        workspace_id: UUID,
        mission_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "mission.scope.edit", body, detail, mission_id
        )

    @app.post(f"{_CLIENT}/decisions/{{decision_id}}/answer")
    def client_decision_answer(
        request: Request,
        workspace_id: UUID,
        decision_id: UUID,
        body: dict[str, Any],
        detail: bool = False,
    ) -> JSONResponse:
        return mutate(
            request, workspace_id, "decision.answer", body, detail, decision_id
        )
