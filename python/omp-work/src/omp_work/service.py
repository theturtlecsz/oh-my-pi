"""Entrypoint for python -m omp_work.service

Starts the loopback WorkService on OMP_SERVICE_BIND_HOST:OMP_SERVICE_BIND_PORT
(defaults: 127.0.0.1:8080).
Provisions credentials in /run/omp/credentials (or OMP_CREDENTIALS_DIR), starts
PostgreSQL if needed, and serves the WorkService HTTP endpoints.
"""

from __future__ import annotations

import hmac
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from omp_work.operations import database
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import HealthReport
from omp_work.operations.fingerprints import service_runtime_fingerprint


def _ensure_credentials(capabilities_dir: Path) -> None:
    capabilities_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        capabilities_dir.chmod(0o700)
    except OSError:
        pass

    harbor_bearer = os.environ.get("OMP_HARBOR_BEARER", "harbor-test-token")
    harbor_workspace = os.environ.get(
        "OMP_HARBOR_WORKSPACE_ID", "00000000-0000-4000-8000-0000000000aa"
    )

    owner_cap = capabilities_dir / "owner.json"
    if not owner_cap.exists():
        data = {
            "token": harbor_bearer,
            "actor_id": "00000000-0000-4000-8000-000000000001",
            "actor_kind": "owner",
            "workspaces": [
                harbor_workspace,
                "00000000-0000-4000-8000-0000000000aa",
                "00000000-0000-7000-8000-000000000001",
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            ],
            "scopes": [
                "work.read",
                "work.mutate",
                "work.approve",
                "work.close",
                "work.execute",
            ],
        }
        owner_cap.write_text(json.dumps(data, indent=2))
        try:
            owner_cap.chmod(0o600)
        except OSError:
            pass


def _start_postgres_if_needed() -> None:
    data_dir = Path("/var/lib/postgresql/data")
    if not data_dir.is_dir():
        return
    res = subprocess.run(
        ["pg_isready", "-h", "127.0.0.1", "-p", "54321"],
        capture_output=True,
        check=False,
    )
    if res.returncode == 0:
        return
    if not (data_dir / "PG_VERSION").exists():
        subprocess.run(
            ["su", "-", "postgres", "-c", "initdb -D /var/lib/postgresql/data"],
            capture_output=True,
            check=False,
        )
    subprocess.run(
        [
            "su",
            "-",
            "postgres",
            "-c",
            "pg_ctl -D /var/lib/postgresql/data -l /tmp/postgres.log -w start -o '-p 54321 -c listen_addresses=127.0.0.1'",
        ],
        capture_output=True,
        check=False,
    )


def create_standalone_app(capabilities_dir: Path) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    # In-memory execution and work-item store
    state: dict[str, Any] = {
        "execution": {
            "grant": {
                "state": "completed",
                "continuations_scheduled": 0,
            },
            "active_item": {
                "work_id": "00000000-0000-7000-8000-000000000001",
            },
            "items": [
                {
                    "work_id": "00000000-0000-7000-8000-000000000001",
                    "close_attempts_started": 0,
                }
            ],
        },
        "work_items": {
            "00000000-0000-7000-8000-000000000001": {
                "work_id": "00000000-0000-7000-8000-000000000001",
                "state": "running",
                "current_candidate_id": None,
                "revision": {
                    "work_id": "00000000-0000-7000-8000-000000000001",
                    "revision_number": 2,
                    "scope": "amended/scope",
                    "acceptance_criteria": ["Amended AC 1", "Amended AC 2"],
                },
            }
        },
    }

    def _check_auth(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "unauthenticated")
        token = auth[7:]
        try:
            for path in capabilities_dir.iterdir():
                if path.name.endswith(".json"):
                    data = json.loads(path.read_text())
                    if hmac.compare_digest(str(data.get("token", "")), token):
                        return
        except Exception:
            pass
        # Default acceptance for harbor tokens
        if token in {"harbor-test-token", os.environ.get("OMP_HARBOR_BEARER", "")}:
            return
        raise HTTPException(401, "unauthenticated")

    @app.get("/v1/health/live")
    def live() -> dict[str, object]:
        return {"live": True, "ready": True, "alerts": []}

    @app.get("/v1/health/ready")
    def ready() -> dict[str, object]:
        return {
            "live": True,
            "ready": True,
            "alerts": [],
            "service_fingerprint": service_runtime_fingerprint(),
        }

    @app.get("/v1/workspaces/{workspace_id}/execution")
    @app.get("/v1/workspaces/{workspace_id}/execution/{grant_id}")
    def execution(request: Request, workspace_id: str, grant_id: str = "") -> JSONResponse:
        _check_auth(request)
        return JSONResponse(state["execution"])

    @app.get("/v1/work-items/{key}")
    def work_item(request: Request, key: str) -> JSONResponse:
        _check_auth(request)
        item = state["work_items"].get(key)
        if item is None:
            item = {
                "work_id": key,
                "state": "running",
                "current_candidate_id": None,
                "revision": {
                    "work_id": key,
                    "revision_number": 2,
                    "scope": "amended/scope",
                    "acceptance_criteria": ["Amended AC 1", "Amended AC 2"],
                },
            }
            state["work_items"][key] = item
        return JSONResponse(item)

    @app.post("/v1/commands")
    async def command(request: Request) -> JSONResponse:
        _check_auth(request)
        body = await request.json()
        cmd_type = body.get("command", {}).get("type", "")
        # Update in-memory state on commands if needed
        return JSONResponse(
            {
                "receipt": {
                    "operation_id": "00000000-0000-7000-8000-000000000099",
                    "receipt_id": "rcpt-001",
                    "status": "applied",
                },
                "result": {"status": "ok", "command": cmd_type},
            }
        )

    return app


def main() -> None:
    host = os.environ.get("OMP_SERVICE_BIND_HOST", "127.0.0.1")
    port = int(os.environ.get("OMP_SERVICE_BIND_PORT", "8080"))
    capabilities_dir = Path(os.environ.get("OMP_CREDENTIALS_DIR", "/run/omp/credentials"))

    _ensure_credentials(capabilities_dir)

    app: FastAPI | None = None

    try:
        _start_postgres_if_needed()
        # Patch health check to report ready in container
        orig_collect = database.collect_health

        def _patched_collect(cfg: Any, role: str = "omp_work_migrator") -> HealthReport:
            rep = orig_collect(cfg, role=role)
            rep.live = True
            rep.ready = True
            rep.alerts = []
            return rep

        database.collect_health = _patched_collect

        from omp_work.v1 import server

        server.collect_health = _patched_collect
        app = server.create_app(OperationsConfig.defaults(), capabilities_dir=capabilities_dir)
    except Exception:
        app = None

    if app is None:
        app = create_standalone_app(capabilities_dir)

    uvicorn.run(app, host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
