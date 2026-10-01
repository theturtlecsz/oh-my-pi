"""OMP-423-s04: section-17 acceptance over the s01-s03 reads.

A service process and a native-jobs worker die after a mission is submitted,
approved, linked, and given one pending decision and one leased job. A fresh
client that knows only the base URL, the workspace id, and the client token
reads where things stand. Restarting the worker seals that job once.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from omp_work import contract_sha256
from omp_work.jobs.admission import enqueue_job
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.capabilities import CLIENT_SCOPES
from omp_work.v1.missions import _MISSION_EVENTS
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import row_json
from psycopg.rows import dict_row
from native_jobs_support import native_jobs  # noqa: F401
from test_jobs_worker_process import _config_dict, _env_for_child
from test_mission_links_store import _ITEM_A, _draft, _seed_budget
from test_native_jobs_process_recovery import _CHILD_PRELUDE
from test_research_contract import _register_component
from test_workflow_service import _owner_headers

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)

_CLIENT_TOKEN = "where-are-we-client-token"
_SERVICE_SCRIPT = _CHILD_PRELUDE + """
import uvicorn
from omp_work.v1.server import create_app

uvicorn.run(
    create_app(config, capabilities_dir=Path(sys.argv[3])),
    host="127.0.0.1",
    port=int(sys.argv[4]),
    access_log=False,
    log_level="warning",
)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connect(config):
    return psycopg.connect(
        **config.connection_kwargs("postgres"),
        row_factory=dict_row,
        autocommit=True,
    )


def _latest_mission_status(config, workspace_id: UUID, mission_id: UUID) -> str:
    with _connect(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND aggregate_type='mission' AND aggregate_id=%s"
            " AND outcome='applied' AND event_type = ANY(%s)"
            " ORDER BY sequence DESC LIMIT 1",
            (workspace_id, mission_id, list(_MISSION_EVENTS)),
        )
        row = cur.fetchone()
    assert row is not None, "no applied mission event"
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return str(payload["mission"]["status"])


def _stored_job(config, workspace_id: UUID, job_id: str) -> dict[str, object]:
    with _connect(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT job_id, work_id, kind, status, attempt, lease_expires_at"
            " FROM omp_jobs.jobs WHERE workspace_id=%s AND job_id=%s",
            (workspace_id, job_id),
        )
        row = cur.fetchone()
    assert row is not None, job_id
    stored = row_json(row)
    assert stored is not None
    return stored


def _job_event_count(config, job_id: str, *, kind: str | None = None) -> int:
    with _connect(config) as conn, conn.cursor() as cur:
        if kind is None:
            cur.execute(
                "SELECT count(*) AS cnt FROM omp_jobs.job_events WHERE job_id=%s",
                (job_id,),
            )
        else:
            cur.execute(
                "SELECT count(*) AS cnt FROM omp_jobs.job_events"
                " WHERE job_id=%s AND kind=%s",
                (job_id, kind),
            )
        return int(cur.fetchone()["cnt"])


def _effect_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text().splitlines() if line.strip()]


class _FreshClient:
    """Client that knows the base URL, the workspace id, and the client token."""

    def __init__(self, base_url: str, workspace_id: UUID, client_token: str) -> None:
        self.base_url = base_url
        self.workspace_id = workspace_id
        self.events: list[tuple] = []

        def on_request(request: httpx.Request) -> None:
            self.events.append(("request", request.method, request.url.path, None))

        def on_response(response: httpx.Response) -> None:
            path = response.request.url.path
            if "/client/projects/" in path and path.endswith("/status"):
                response.read()
                self.events.append(
                    ("response", response.request.method, path, response.json())
                )

        self._http = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            event_hooks={"request": [on_request], "response": [on_response]},
            headers={
                "Authorization": f"Bearer {client_token}",
                "X-OMP-Contract-SHA256": contract_sha256(),
            },
        )

    def get(self, path: str) -> httpx.Response:
        return self._http.get(path)

    def close(self) -> None:
        self._http.close()


def _assert_fresh_requests(client: _FreshClient) -> None:
    """Every request is a GET under /client/, and each mission id came from project.status."""
    known: set[str] = set()
    prefix = f"/v1/workspaces/{client.workspace_id}/client/"
    mission_prefix = prefix + "missions/"
    asked: list[str] = []
    assert client.events, "fresh client made no requests"
    for kind, method, path, body in client.events:
        if kind == "request":
            assert method == "GET", (method, path)
            assert path.startswith(prefix), path
            if path.startswith(mission_prefix):
                mission_id = path.removeprefix(mission_prefix)
                assert mission_id in known, mission_id
                asked.append(mission_id)
        else:
            assert body is not None
            for mission in body["result"]["open_missions"]:
                known.add(str(mission["mission_id"]))
    assert asked, "fresh client never read a mission"


def _where(client: _FreshClient) -> dict[str, dict[str, object]]:
    workspace_id = client.workspace_id
    listed = client.get(f"/v1/workspaces/{workspace_id}/client/projects")
    assert listed.status_code == 200, listed.text
    assert listed.json()["operation"] == "project.list"
    found: dict[str, dict[str, object]] = {}
    for project in listed.json()["result"]["projects"]:
        project_id = project["project_id"]
        status = client.get(
            f"/v1/workspaces/{workspace_id}/client/projects/{project_id}/status"
        )
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["operation"] == "project.status"
        for mission in body["result"]["open_missions"]:
            mission_id = str(mission["mission_id"])
            read = client.get(
                f"/v1/workspaces/{workspace_id}/client/missions/{mission_id}"
            )
            assert read.status_code == 200, read.text
            mission_body = read.json()
            assert mission_body["operation"] == "mission.status"
            found[mission_id] = mission_body
    return found


def _owner_command(client: httpx.Client, workspace_id: UUID, command: dict) -> dict:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(workspace_id),
        "operation_id": str(uuid4()),
        "request_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "command": command,
    }
    response = client.post(
        "/v1/commands",
        headers=_owner_headers(workspace_id),
        json=envelope,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _write_client_capability(capabilities: Path, workspace_id: UUID) -> None:
    path = capabilities / "client.json"
    path.write_text(
        json.dumps(
            {
                "token": _CLIENT_TOKEN,
                "actor_id": str(uuid4()),
                "actor_kind": "client",
                "workspaces": [str(workspace_id)],
                "scopes": list(CLIENT_SCOPES),
            }
        )
    )
    path.chmod(0o600)


def _spawn(
    argv: list[str],
    log_path: Path,
    procs: list[subprocess.Popen],
    logs: list[object],
) -> subprocess.Popen:
    handle = log_path.open("w")
    logs.append(handle)
    proc = subprocess.Popen(
        argv,
        env=_env_for_child(),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    procs.append(proc)
    return proc


def _kill(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.wait(timeout=5)


def _start_service(
    config_json: str,
    capabilities: Path,
    log_path: Path,
    procs: list[subprocess.Popen],
    logs: list[object],
) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    proc = _spawn(
        [
            sys.executable,
            "-c",
            _SERVICE_SCRIPT,
            config_json,
            "{}",
            str(capabilities),
            str(port),
        ],
        log_path,
        procs,
        logs,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    last = ""
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"service exited {proc.returncode}\n{log_path.read_text()[-4000:]}"
            )
        try:
            response = httpx.get(f"{base}/v1/health/live", timeout=1.0)
        except httpx.HTTPError as exc:
            last = str(exc)
        else:
            if response.status_code == 200:
                return proc, base
            last = response.text
        time.sleep(0.05)
    raise AssertionError(f"/v1/health/live not ready: {last}\n{log_path.read_text()[-4000:]}")


def _worker_config(
    native_jobs,
    tmp_path: Path,
    effect_file: Path,
    *,
    component: str,
    worker_id: str,
) -> Path:
    cfg_path = tmp_path / "worker.json"
    cfg_path.write_text(
        json.dumps(
            {
                "workspace_id": str(native_jobs.workspace_id),
                "actor_id": str(native_jobs.actor_id),
                "worker_id": worker_id,
                "component_sha256": component,
                "capacity": 1,
                "idle_sleep": 0.05,
                "work_url": "http://127.0.0.1:54322",
                "bearer_file": str(native_jobs.service.capabilities / "owner.json"),
                "operations": _config_dict(native_jobs.service.config),
                "handlers": {
                    "compute.cpu": {
                        "factory": "jobs_effect_handler:factory",
                        "options": {
                            "file_path": str(effect_file),
                            "sleep_seconds": 60,
                        },
                    }
                },
            }
        )
    )
    return cfg_path


def _start_worker(
    cfg_path: Path,
    log_path: Path,
    procs: list[subprocess.Popen],
    logs: list[object],
    *,
    once: bool = False,
) -> subprocess.Popen:
    argv = [sys.executable, "-m", "omp_work", "jobs", "worker", "--config", str(cfg_path)]
    if once:
        argv.append("--once")
    return _spawn(argv, log_path, procs, logs)


def _wait_effect(proc: subprocess.Popen, effect_file: Path, job_id: str, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if job_id in _effect_lines(effect_file):
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"worker exited {proc.returncode} before the effect\n"
                f"{log_path.read_text()[-4000:]}"
            )
        time.sleep(0.05)
    detail = log_path.read_text()[-4000:] if log_path.exists() else ""
    raise AssertionError(f"effect file never held {job_id}\n{detail}")


def _wait_sealed(proc: subprocess.Popen, config, workspace_id: UUID, job_id: str, log_path: Path) -> None:
    deadline = time.monotonic() + 20
    last = ""
    while time.monotonic() < deadline:
        stored = _stored_job(config, workspace_id, job_id)
        last = str(stored["status"])
        if stored["status"] == "sealed":
            return
        if proc.poll() is not None:
            raise AssertionError(
                f"worker exited {proc.returncode} before seal ({last})\n"
                f"{log_path.read_text()[-4000:]}"
            )
        time.sleep(0.1)
    raise AssertionError(
        f"job stayed {last}\n{log_path.read_text()[-4000:]}"
    )


def test_restart_where_are_we(native_jobs, tmp_path: Path) -> None:
    config = native_jobs.service.config
    workspace_id = native_jobs.workspace_id
    capabilities = native_jobs.service.capabilities
    _write_client_capability(capabilities, workspace_id)
    config_json = json.dumps(_config_dict(config))

    project_id = PostgresWorkStore(config).ensure_project(
        workspace_id,
        native_jobs.actor_id,
        f"p-{uuid4().hex[:8]}",
        "Where are we",
        "surface",
    )
    _seed_budget(
        native_jobs.service,
        workspace_id,
        work_id=native_jobs.item["work_id"],
        revision_id=native_jobs.item["revision_id"],
        budget=_ITEM_A,
    )
    mission_id = uuid4()
    decision_id = uuid4()
    job_id = f"job-where-{uuid4()}"
    effect_file = tmp_path / "effect.log"
    component = _register_component(
        native_jobs.service,
        workspace_id,
        "worker",
        name=f"where-worker-{uuid4()}",
        capabilities=("compute.cpu",),
    )
    worker_id = f"worker-where-{uuid4()}"
    cfg_path = _worker_config(
        native_jobs,
        tmp_path,
        effect_file,
        component=component,
        worker_id=worker_id,
    )

    procs: list[subprocess.Popen] = []
    logs: list[object] = []
    owner: httpx.Client | None = None
    fresh: _FreshClient | None = None
    try:
        service_proc, base = _start_service(
            config_json, capabilities, tmp_path / "service-1.log", procs, logs
        )
        owner = httpx.Client(base_url=base, timeout=30.0)
        submitted = _owner_command(
            owner,
            workspace_id,
            {
                "type": "submit_mission",
                "payload": {
                    "mission_id": str(mission_id),
                    "draft": _draft(project_id),
                },
            },
        )
        assert submitted["result"]["mission"]["status"] == "awaiting_confirmation"
        approved = _owner_command(
            owner,
            workspace_id,
            {
                "type": "approve_mission",
                "payload": {
                    "mission_id": str(mission_id),
                    "revision": submitted["result"]["mission"]["revision"],
                    "basis_kind": "decision",
                    "basis_id": str(uuid4()),
                },
            },
        )
        assert approved["result"]["mission"]["status"] == "approved"
        linked = _owner_command(
            owner,
            workspace_id,
            {
                "type": "link_mission_work",
                "payload": {
                    "mission_id": str(mission_id),
                    "work_id": str(native_jobs.item["work_id"]),
                },
            },
        )
        assert linked["result"]["mission"]["links"][0]["work_id"] == str(
            native_jobs.item["work_id"]
        )
        created = _owner_command(
            owner,
            workspace_id,
            {
                "type": "create_decision",
                "payload": {
                    "decision_id": str(decision_id),
                    "project_id": str(project_id),
                    "mission_id": str(mission_id),
                    "question": "Publish the intake externally?",
                    "why_it_matters": "The mission cannot continue without a ruling.",
                    "risk_of_delay": "The window closes and the mission stalls.",
                    "options": ["publish", "hold"],
                    "evidence_refs": ["receipt:abc"],
                    "default_if_any": "hold",
                    "risk_of_each_choice": {
                        "publish": "Irreversible external exposure.",
                        "hold": "Missed deadline.",
                    },
                    "action_class": None,
                    "resume_state": json.dumps({"step": 3}),
                },
            },
        )
        assert created["result"]["decision_id"] == str(decision_id)
        assert created["result"]["mission_id"] == str(mission_id)

        enqueued = enqueue_job(
            NativeJobStore(config),
            operation_id=str(uuid4()),
            workspace_id=workspace_id,
            actor_id=native_jobs.actor_id,
            job_id=job_id,
            work_id=UUID(str(native_jobs.item["work_id"])),
            kind="compute",
            required_capabilities=["compute.cpu"],
            resources={"cpu": 1, "memory_mib": 0, "gpu": 0, "model_calls": 0},
            lease_seconds=2,
        )
        assert enqueued["status"] == "applied"

        worker = _start_worker(
            cfg_path, tmp_path / "worker-1.log", procs, logs
        )
        _wait_effect(worker, effect_file, job_id, tmp_path / "worker-1.log")

        owner.close()
        owner = None

        _kill(service_proc)
        _kill(worker)
        assert _effect_lines(effect_file) == [job_id]

        _, restarted_base = _start_service(
            config_json, capabilities, tmp_path / "service-2.log", procs, logs
        )
        fresh = _FreshClient(restarted_base, workspace_id, _CLIENT_TOKEN)
        standing = _where(fresh)[str(mission_id)]
        stored = _stored_job(config, workspace_id, job_id)
        assert standing["state"] == _latest_mission_status(config, workspace_id, mission_id)
        assert standing["decisions"] == [str(decision_id)]
        assert standing["result"]["jobs_in_flight"] == [stored]
        assert stored["job_id"] == job_id

        restarted_worker = _start_worker(
            cfg_path, tmp_path / "worker-2.log", procs, logs
        )
        _wait_sealed(
            restarted_worker, config, workspace_id, job_id, tmp_path / "worker-2.log"
        )
        assert _stored_job(config, workspace_id, job_id)["status"] == "sealed"
        assert _job_event_count(config, job_id, kind="settled") == 1
        assert _effect_lines(effect_file) == [job_id]
        _kill(restarted_worker)

        events_before = _job_event_count(config, job_id)
        once = _start_worker(
            cfg_path, tmp_path / "worker-once.log", procs, logs, once=True
        )
        try:
            once.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill(once)
            raise
        assert once.returncode == 0, (tmp_path / "worker-once.log").read_text()[-4000:]
        assert _job_event_count(config, job_id) == events_before
        assert _effect_lines(effect_file) == [job_id]

        again = _where(fresh)[str(mission_id)]
        assert again["result"]["jobs_in_flight"] == []
        assert again["decisions"] == [str(decision_id)]
        assert again["result"]["pending_decisions"][0]["decision_id"] == str(decision_id)
        assert again["result"]["pending_decisions"][0]["status"] == "pending"
        assert again["state"] == _latest_mission_status(config, workspace_id, mission_id)
        _assert_fresh_requests(fresh)
    finally:
        if owner is not None:
            owner.close()
        if fresh is not None:
            fresh.close()
        for proc in procs:
            _kill(proc)
        for handle in logs:
            handle.close()
