"""Compiled CLI worker, real jail and proxy: edits, refusals, billing and fallback."""

from __future__ import annotations

import http.client
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import HTTPException
from fastapi.exception_handlers import http_exception_handler
from omp_work import egress_sandbox
from omp_work.jobs.store import NativeJobStore
from omp_work.model_proxy import ProxyConfig, create_app
from omp_work.model_proxy_upstream import TransportResponse
from omp_work.orchestrator import model_proxy_client
from omp_work.orchestrator import service as orchestrator
from omp_work.orchestrator import worker_sandbox
from omp_work.orchestrator.service import OrchestratorHandler, submit
from omp_work.v1.server import create_app as work_app
from orchestrator_e2e_support import open_e2e_world, open_worker, tick_until
from test_item_budget_alerts import _seed_budget
from test_model_proxy import serving
from test_model_proxy_budget import _admitting_job, _budget_ctx
from test_model_proxy_upstream import gemini_route, ollama_route
from test_workflow_service import OWNER

pytest_plugins = ("test_workflow_service",)
pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

PROVIDER_KEY = "production-test-provider-secret"
CONTROL_TOKEN = "production-test-control-secret"
CONTENT = "production worker edit\n"


@pytest.fixture(scope="module")
def binary():
    with TemporaryDirectory(prefix="omp-production-worker-") as directory:
        repo = Path(__file__).resolve().parents[3]
        outfile = Path(directory) / "orchestrator-worker"
        script = f"""
    import {{ compileCodingAgent }} from {json.dumps(str(repo / "packages/coding-agent/scripts/compile-binary.ts"))};
    import {{ loadNative, nativeAddonStatus }} from {json.dumps(str(repo / "packages/natives/native/loader-state.js"))};
    import * as path from "node:path";
    process.env.PI_NATIVE_VARIANT = "baseline";
    process.env.PI_COMPILED = "true";
    loadNative();
    const addon = nativeAddonStatus();
    const native = path.join({json.dumps(str(outfile.parent))}, path.basename(addon.path));
    await Bun.write(native, Bun.file(addon.path));
    const manifest = require("@huggingface/transformers/package.json");
    await compileCodingAgent({{
        repoRoot: {json.dumps(str(repo))},
        entrypoint: {json.dumps(str(repo / "packages/coding-agent/test/fixtures/orchestrator-worker-host.ts"))},
        outfile: {json.dumps(str(outfile))},
        transformersVersion: manifest.version,
    }});
    """
        result = subprocess.run(
            ["bun", "--eval", script],
            cwd=repo / "packages/coding-agent",
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        # Restricted Linux runners cannot mount procfs in the worker PID namespace.
        # glibc then cannot discover the main stack for Bun. Supply a test-local
        # pthread_getattr_np fallback; keep every jail and worker boundary intact.
        shim = outfile.parent / "libstackshim.so"
        source = r"""
    #define _GNU_SOURCE
    #include <pthread.h>
    #include <stdint.h>
    #include <sys/resource.h>
    #include <dlfcn.h>
    #include <unistd.h>
    #include <sys/stat.h>
    #include <stdio.h>
    #include <string.h>
    #include <errno.h>
    extern void *__libc_stack_end;
    int pthread_getattr_np(pthread_t thread, pthread_attr_t *attr) {
        int (*real_getattr)(pthread_t, pthread_attr_t *) = dlsym(RTLD_NEXT, "pthread_getattr_np");
        int result = real_getattr(thread, attr);
        if (!result || !pthread_equal(thread, pthread_self())) return result;
        struct rlimit limit;
        if (getrlimit(RLIMIT_STACK, &limit) != 0 || limit.rlim_cur == RLIM_INFINITY) return result;
        uintptr_t top = ((uintptr_t)__libc_stack_end + getpagesize() - 1) & ~(getpagesize() - 1);
        result = pthread_attr_init(attr);
        if (result) return result;
        return pthread_attr_setstack(attr, (void *)(top - limit.rlim_cur), limit.rlim_cur);
    }
    /* Bun canonicalizes /work through /proc/self/fd. Match the real inode, never
     * fabricate a successful lookup for another fd or an untrusted path. */
    ssize_t readlink(const char *path, char *buffer, size_t size) {
        ssize_t (*real_readlink)(const char *, char *, size_t) = dlsym(RTLD_NEXT, "readlink");
        ssize_t result = real_readlink(path, buffer, size);
        if (result >= 0 || errno != ENOENT) return result;
        int fd, consumed = 0;
        struct stat opened, root;
        if (strncmp(path, "/proc/self/fd/", 14) != 0
        || sscanf(path, "/proc/self/fd/%d%n", &fd, &consumed) != 1 || path[consumed] != '\0'
            || fstat(fd, &opened) != 0 || stat("/work", &root) != 0
            || opened.st_dev != root.st_dev || opened.st_ino != root.st_ino) return result;
        size_t length = size < 5 ? size : 5;
        memcpy(buffer, "/work", length);
        return length;
    }
    """
        subprocess.run(
            [
                "cc",
                "-shared",
                "-fPIC",
                "-x",
                "c",
                "-",
                "-o",
                str(shim),
                "-ldl",
                "-pthread",
            ],
            input=source,
            text=True,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "uv",
                "run",
                "--project",
                str(repo / "python/omp-work"),
                "--with",
                "patchelf",
                "patchelf",
                "--add-needed",
                str(shim),
                str(outfile),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        natives = tuple(outfile.parent.glob("*.node"))
        assert len(natives) == 1
        yield outfile, shim, natives[0]


@contextmanager
def upstream(forbidden=False, *, status_code=200, block_all=False):
    calls = []
    entered = threading.Condition()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            held = "hold-capacity" in json.dumps(body)
            gemini = self.path.startswith("/v1beta/")
            with entered:
                calls.append((self.path, body, dict(self.headers), held or block_all))
                entered.notify_all()
            if held:
                assert release.wait(90)
            elif block_all:
                release.wait(2.0)
            if status_code != 200:
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            args = {
                "path": "forbidden.txt" if forbidden else "feature.txt",
                "content": CONTENT,
            }
            if gemini:
                done = held or any(
                    part.get("functionResponse")
                    for row in body["contents"]
                    for part in row.get("parts", [])
                )
                parts = (
                    [{"text": "Done."}]
                    if done
                    else [{"functionCall": {"name": "write", "args": args}}]
                )
                payload = {
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": parts},
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 5,
                        "totalTokenCount": 15,
                    },
                }
                data = ("data: " + json.dumps(payload) + "\n\n").encode()
                content_type = "text/event-stream"
            else:
                done = any(row["role"] == "tool" for row in body["messages"])
                message = {
                    "role": "assistant",
                    "content": "Done." if done else "",
                    **(
                        {}
                        if done
                        else {
                            "tool_calls": [
                                {"function": {"name": "write", "arguments": args}}
                            ]
                        }
                    ),
                }
                data = (
                    json.dumps(
                        {
                            "model": body["model"],
                            "message": message,
                            "done": True,
                            "done_reason": "stop",
                            "prompt_eval_count": 10,
                            "eval_count": 5,
                        }
                    )
                    + "\n"
                ).encode()
                content_type = "application/x-ndjson"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    class Transport:
        def post(self, url, headers, body, *, max_bytes, timeout):
            parsed = urlsplit(url)
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=timeout
            )
            try:
                conn.request(
                    "POST",
                    parsed.path + ("?" + parsed.query if parsed.query else ""),
                    body,
                    headers,
                )
                response = conn.getresponse()
                return TransportResponse(
                    response.status,
                    tuple(response.getheaders()),
                    response.read(max_bytes + 1),
                    url,
                )
            finally:
                conn.close()

    try:
        yield Transport(), calls, entered, release
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(10)
        assert not thread.is_alive()


@pytest.mark.parametrize("mode", ["gemini", "forbidden", "capped"])
def test_production_worker(service, tmp_path, monkeypatch, binary, capfd, mode):
    """Real implement step writes only its envelope and bills every upstream request."""
    binary, shim, native = binary
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    monkeypatch.setenv("GEMINI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OMP_CONTROL_TOKEN", CONTROL_TOKEN)
    world = open_e2e_world(
        service,
        tmp_path / mode,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    world.request = replace(
        world.request,
        worker_argv=(str(binary), "orchestrator-worker"),
        scope=world.request.scope.model_copy(
            update={
                "budget_policy": world.request.scope.budget_policy.model_copy(
                    update={"tokens": 200_000}
                )
            }
        ),
    )
    # The same automation identity controls the orchestrator and proxy.
    capability = world.config.automation_capability_path
    data = json.loads(capability.read_text())
    data["token"] = CONTROL_TOKEN
    holders = [_budget_ctx(service) for _ in range(6)] if mode == "capped" else []
    data["workspaces"] += [str(ctx.workspace_id) for ctx in holders]
    capability.write_text(json.dumps(data))
    capability.chmod(0o600)
    for name in ("gemini-key", "ollama-key"):
        key = service.config.credentials_dir / name
        key.write_text(PROVIDER_KEY)
        key.chmod(0o600)

    launches, peers = [], []
    real_sandbox = worker_sandbox.run_sandboxed
    real_pump = egress_sandbox._pump

    def observe_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs):
        launches.append(dict(env))
        kwargs["ro_binds"] = (*kwargs.get("ro_binds", ()), str(shim), str(native))
        return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)

    def observe_peer(left, right):
        if right.family in (2, 10):
            peers.append(right.getpeername())
        return real_pump(left, right)

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", observe_sandbox)
    monkeypatch.setattr(egress_sandbox, "_pump", observe_peer)
    with upstream(mode == "forbidden") as (transport, calls, entered, release):
        config = ProxyConfig(
            service.config,
            capability,
            {
                "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
                "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
            },
        )
        app = create_app(config, transport=transport)
        refusals = []

        @app.exception_handler(HTTPException)
        async def record_refusal(request, exc):
            refusals.append(exc.detail)
            return await http_exception_handler(request, exc)

        with (
            serving(app) as proxy_url,
            serving(
                work_app(service.config, capabilities_dir=service.capabilities)
            ) as work_url,
        ):
            world.config = replace(
                world.config,
                model_proxy_url=proxy_url,
                workservice_url=work_url,
                lease_seconds=180,
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
            headers = {"Authorization": "Bearer " + CONTROL_TOKEN}
            with (
                ThreadPoolExecutor(max_workers=6) as pool,
                httpx.Client(base_url=proxy_url, timeout=100) as client,
            ):
                futures, grants = [], []
                try:
                    for ctx in holders:
                        task = dict(
                            workspace_id=str(ctx.workspace_id),
                            project_id=str(ctx.project_id),
                            work_id=str(ctx.work_id),
                            job_id=_admitting_job(ctx, lease_seconds=180),
                            worker_id="worker-1",
                            fence=1,
                            stage="implement",
                            expires_at=(
                                datetime.now(UTC) + timedelta(seconds=120)
                            ).isoformat(),
                            ceiling_usd="10",
                        )
                        response = client.post("/tasks", json=task, headers=headers)
                        assert response.status_code == 200, response.text
                        grant = response.json()
                        assert grant["descriptor"]["id"] == "gemini-3.8-flash-high"
                        grants.append(grant)
                        futures.append(
                            pool.submit(
                                client.post,
                                "/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
                                json={
                                    "contents": [{"parts": [{"text": "hold-capacity"}]}]
                                },
                                headers={"x-goog-api-key": grant["model_token"]},
                            )
                        )
                    if holders:
                        with entered:
                            assert entered.wait_for(
                                lambda: sum(call[3] for call in calls) == 6,
                                timeout=30,
                            ), (
                                len(calls),
                                [call[3] for call in calls],
                                refusals,
                                [f.done() for f in futures],
                            )

                    def implemented(view):
                        return any(
                            step.get("stage") == "implement"
                            and step.get("kind") == "outcome"
                            for step in view["steps"]
                        )

                    tick_until(worker, world, implemented, limit=30)
                    handler = OrchestratorHandler(world.config)
                    handler.mission_id = world.mission_id
                    outcome = next(
                        step
                        for step in handler.steps()
                        if step.get("stage") == "implement"
                        and step.get("kind") == "outcome"
                    )
                finally:
                    release.set()
                    for future in futures:
                        response = future.result(timeout=30)
                        assert response.status_code == 200, response.text
                    for grant in grants:
                        assert (
                            client.delete(
                                "/tasks/" + grant["id"], headers=headers
                            ).status_code
                            == 200
                        )
            worker_calls = [call for call in calls if not call[3]]
            expected = (
                "deepseek-v4.1-flash:cloud" if holders else "gemini-3.8-flash-high"
            )
            assert all(
                expected in (call[0] if not holders else call[1]["model"])
                for call in worker_calls
            )
            assert len(worker_calls) == (1 if mode == "forbidden" else 2), (
                outcome,
                refusals,
            )
            tree = Path(outcome["data"]["worktree"])
            if mode == "forbidden":
                assert (
                    outcome["outcome"] == "crashed"
                    and outcome["data"]["exit_code"] != 0
                )
                assert not (tree / "forbidden.txt").exists()
                assert not (tree / "feature.txt").exists()
            else:
                assert outcome["outcome"] == "succeeded", outcome
                assert (tree / "feature.txt").read_text() == CONTENT
            stdout, stderr = capfd.readouterr()
            events = [
                json.loads(line) for line in stdout.splitlines() if line.startswith("{")
            ]
            child_env = next(
                event["env"]
                for event in events
                if event["type"] == "worker_environment"
            )
            assert (
                "GEMINI_API_KEY" not in child_env
                and "OMP_CONTROL_TOKEN" not in child_env
            )
            assert launches and peers
            assert set(peers) == {("127.0.0.1", urlsplit(proxy_url).port)}
            for secret in (PROVIDER_KEY, CONTROL_TOKEN):
                assert secret not in json.dumps(launches)
                assert secret not in stdout and secret not in stderr
                assert all(
                    secret.encode() not in path.read_bytes()
                    for path in tree.rglob("*")
                    if path.is_file()
                )
            workspace_ids = [
                world.workspace_id,
                *(ctx.workspace_id for ctx in holders),
            ]
            with psycopg.connect(
                **service.config.connection_kwargs("postgres")
            ) as conn:
                rows = conn.execute(
                    "SELECT price_usd FROM omp_jobs.usage_events WHERE workspace_id = ANY(%s)",
                    (workspace_ids,),
                ).fetchall()
            assert len(rows) == len(calls)
            assert all(row[0] > 0 for row in rows)


@pytest.mark.parametrize("scenario", ["success", "provider_500", "timeout"])
def test_revocation_after_success_failure_timeout(
    service, tmp_path, monkeypatch, binary, capfd, scenario
):
    binary, shim, native = binary
    monkeypatch.setattr(orchestrator, "_REGISTRY", dict(orchestrator._REGISTRY))
    monkeypatch.setenv("GEMINI_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("OMP_CONTROL_TOKEN", CONTROL_TOKEN)
    world = open_e2e_world(
        service,
        tmp_path / scenario,
        monkeypatch,
        verifier_mode="pass",
        worker_mode="ok",
        extra_capability=None,
        operations=("omp_work.orchestrator.operations:register",),
    )
    world.request = replace(
        world.request,
        worker_argv=(str(binary), "orchestrator-worker"),
        scope=world.request.scope.model_copy(
            update={
                "budget_policy": world.request.scope.budget_policy.model_copy(
                    update={"tokens": 200_000}
                )
            }
        ),
    )
    capability = world.config.automation_capability_path
    data = json.loads(capability.read_text())
    data["token"] = CONTROL_TOKEN
    holder = _budget_ctx(service) if scenario == "success" else None
    if holder is not None:
        data["workspaces"] += [str(holder.workspace_id)]
    capability.write_text(json.dumps(data))
    capability.chmod(0o600)
    for name in ("gemini-key", "ollama-key"):
        key = service.config.credentials_dir / name
        key.write_text(PROVIDER_KEY)
        key.chmod(0o600)

    real_sandbox = worker_sandbox.run_sandboxed

    def observe_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs):
        kwargs["ro_binds"] = (*kwargs.get("ro_binds", ()), str(shim), str(native))
        return real_sandbox(argv, identity, recorder, workdir, env, timeout, **kwargs)

    monkeypatch.setattr(worker_sandbox, "run_sandboxed", observe_sandbox)

    registered_grants: list[dict[str, str]] = []
    real_register = model_proxy_client.register

    def spy_register(capability_path, base_url, payload, **kwargs):
        result = real_register(capability_path, base_url, payload, **kwargs)
        registered_grants.append(
            {"id": result["id"], "model_token": result["model_token"]}
        )
        return result

    revoked_tasks: list[tuple[str, str]] = []
    real_revoke_task = model_proxy_client.revoke_task

    def spy_revoke_task(capability_path, base_url, task_id, **kwargs):
        revoked_tasks.append((base_url, task_id))
        return real_revoke_task(capability_path, base_url, task_id, **kwargs)

    monkeypatch.setattr(
        "omp_work.orchestrator.model_proxy_client.register", spy_register
    )
    monkeypatch.setattr(
        "omp_work.orchestrator.model_proxy_client.revoke_task", spy_revoke_task
    )

    lease_seconds = 1 if scenario == "timeout" else 180
    status_code = 500 if scenario == "provider_500" else 200
    block_all = scenario == "timeout"

    with upstream(False, status_code=status_code, block_all=block_all) as (
        transport,
        calls,
        entered,
        release,
    ):
        config = ProxyConfig(
            service.config,
            capability,
            {
                "gemini_flash": gemini_route(model="gemini-3.8-flash-high"),
                "ollama_cloud": ollama_route(model="deepseek-v4.1-flash:cloud"),
            },
        )
        app = create_app(config, transport=transport)
        with (
            serving(app) as proxy_url,
            serving(
                work_app(service.config, capabilities_dir=service.capabilities)
            ) as work_url,
        ):
            world.config = replace(
                world.config,
                model_proxy_url=proxy_url,
                workservice_url=work_url,
                lease_seconds=lease_seconds,
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

            def implemented(view):
                return any(
                    step.get("stage") == "implement"
                    and step.get("kind") == "outcome"
                    for step in view["steps"]
                )

            try:
                tick_until(worker, world, implemented, limit=30)
            finally:
                release.set()

            handler = OrchestratorHandler(world.config)
            handler.mission_id = world.mission_id
            outcome = next(
                step
                for step in handler.steps()
                if step.get("stage") == "implement"
                and step.get("kind") == "outcome"
            )

            assert len(registered_grants) == 1
            grant_id = registered_grants[0]["id"]
            model_token = registered_grants[0]["model_token"]

            assert len(revoked_tasks) == 1
            assert revoked_tasks[0] == (proxy_url.rstrip("/"), grant_id)

            headers = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
            with httpx.Client(base_url=proxy_url, timeout=30) as client:
                if scenario == "success":
                    assert outcome["outcome"] == "succeeded"
                    assert outcome["data"]["exit_code"] == 0

                    revoked_resp = client.post(
                        "/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
                        json={"contents": [{"parts": [{"text": "probe"}]}]},
                        headers={"Authorization": f"Bearer {model_token}"},
                    )
                    assert revoked_resp.status_code == 403
                    assert revoked_resp.json()["detail"] == "grant_refused"

                    assert holder is not None
                    live_job_id = _admitting_job(holder, lease_seconds=180)
                    live_task = dict(
                        workspace_id=str(holder.workspace_id),
                        project_id=str(holder.project_id),
                        work_id=str(holder.work_id),
                        job_id=live_job_id,
                        worker_id="worker-1",
                        fence=1,
                        stage="implement",
                        expires_at=(
                            datetime.now(UTC) + timedelta(seconds=120)
                        ).isoformat(),
                        ceiling_usd="10",
                    )
                    reg_resp = client.post("/tasks", json=live_task, headers=headers)
                    assert reg_resp.status_code == 200, reg_resp.text
                    live_grant = reg_resp.json()
                    try:
                        live_resp = client.post(
                            "/v1beta/models/gemini-3.8-flash-high:streamGenerateContent?alt=sse",
                            json={"contents": [{"parts": [{"text": "probe"}]}]},
                            headers={"Authorization": f"Bearer {live_grant['model_token']}"},
                        )
                        assert live_resp.status_code == 200, live_resp.text
                    finally:
                        client.delete(
                            f"/tasks/{live_grant['id']}", headers=headers
                        )

                elif scenario == "provider_500":
                    assert outcome["outcome"] == "crashed"

                elif scenario == "timeout":
                    assert outcome["outcome"] == "crashed"
                    assert outcome["data"].get("code") == "lease_dead"

