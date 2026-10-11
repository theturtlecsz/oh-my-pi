"""OMP-590-s06: research run CLI over HTTP against live WorkService."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import uvicorn

from omp_work.jobs.research_trial import trial_job_id
from omp_work.jobs.store import NativeJobStore
from omp_work.operations.jobs_migrate import apply_jobs_migrations
from omp_work.v1.client import WorkClient
from omp_work.v1.server import create_app
from test_workflow_service import _create, _grant

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
pytest_plugins = ("test_workflow_service",)


@contextmanager
def _serve_app(service: SimpleNamespace) -> Iterator[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = create_app(service.config, capabilities_dir=service.capabilities)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn service failed to start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()


def test_research_campaign_cli_http(service: SimpleNamespace, tmp_path: Path) -> None:
    apply_jobs_migrations(service.config)
    workspace_id = uuid4()
    actor_id = uuid4()
    token = f"token-research-{actor_id}"

    _grant(service, workspace_id)
    item = _create(service, workspace_id, "research cli http item")

    capability_path = service.capabilities / f"research-cli-{actor_id}.json"
    capability_path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": "automation",
                "workspaces": [str(workspace_id)],
                "scopes": ["work.read", "work.execute", "work.approve"],
            }
        ),
        encoding="utf-8",
    )
    capability_path.chmod(0o600)

    # Validate that app and migrator role files exist in service.config
    assert (service.config.config_dir / "credentials" / "omp_work_app").is_file()
    assert (service.config.config_dir / "credentials" / "omp_work_migrator").is_file()

    with _serve_app(service) as base_url:
        client_config_path = tmp_path / "client.json"
        client_config_path.write_text(
            json.dumps(
                {
                    "base_url": base_url,
                    "workspace_id": str(workspace_id),
                    "bearer_file": str(capability_path),
                }
            ),
            encoding="utf-8",
        )

        harness_supported = tmp_path / "trial_supported.sh"
        harness_supported.write_text('echo "METRIC score=1"\nexit 0\n', encoding="utf-8")
        harness_supported.chmod(0o755)

        harness_refuted = tmp_path / "trial_refuted.sh"
        harness_refuted.write_text('echo "METRIC score=0"\nexit 0\n', encoding="utf-8")
        harness_refuted.chmod(0o755)

        def_supported = tmp_path / "def_supported.json"
        def_supported.write_text(
            json.dumps(
                {
                    "objective": "Verify trial supported",
                    "domain": "machine_learning",
                    "timeout_seconds": 15,
                    "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
                    "trials": [{"harness": str(harness_supported)}],
                }
            ),
            encoding="utf-8",
        )

        def_refuted = tmp_path / "def_refuted.json"
        def_refuted.write_text(
            json.dumps(
                {
                    "objective": "Verify trial refuted",
                    "domain": "machine_learning",
                    "timeout_seconds": 15,
                    "criteria": [{"metric": "score", "op": "gte", "threshold": 1}],
                    "trials": [{"harness": str(harness_refuted)}],
                }
            ),
            encoding="utf-8",
        )

        # Isolated environment: no host credentials or ambient configs
        isolated_home = tmp_path / "empty_home"
        isolated_xdg = tmp_path / "empty_xdg"
        isolated_home.mkdir(parents=True, exist_ok=True)
        isolated_xdg.mkdir(parents=True, exist_ok=True)
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("PG")
            and not k.startswith("POSTGRES")
            and k not in ("XDG_CONFIG_HOME", "HOME")
        }
        env["HOME"] = str(isolated_home)
        env["XDG_CONFIG_HOME"] = str(isolated_xdg)

        base_args = [
            sys.executable,
            "-m",
            "omp_work",
            "research",
            "run",
            "--key",
            item["key"],
            "--client-config",
            str(client_config_path),
            "--config-dir",
            str(service.config.config_dir),
            "--state-dir",
            str(service.config.state_dir),
            "--data-dir",
            str(service.config.data_dir),
            "--db-port",
            str(service.config.port),
        ]

        # Run 1: supported
        proc_sup = subprocess.run(
            [*base_args, "--definition", str(def_supported)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_sup.returncode == 0, f"stdout: {proc_sup.stdout}\nstderr: {proc_sup.stderr}"
        res_sup = json.loads(proc_sup.stdout)
        assert res_sup["state"] == "concluded"
        assert res_sup["outcome"] == "supported"
        assert res_sup["reason"]
        assert len(res_sup["trials"]) == 1
        assert res_sup["trials"][0]["execution_status"] == "completed"
        assert res_sup["trials"][0]["metrics"] == {"score": 1.0}

        # Run 2: refuted on the SAME item
        proc_ref = subprocess.run(
            [*base_args, "--definition", str(def_refuted)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_ref.returncode == 0, f"stdout: {proc_ref.stdout}\nstderr: {proc_ref.stderr}"
        res_ref = json.loads(proc_ref.stdout)
        assert res_ref["state"] == "concluded"
        assert res_ref["outcome"] == "refuted"
        assert res_ref["reason"]
        assert len(res_ref["trials"]) == 1
        assert res_ref["trials"][0]["execution_status"] == "completed"
        assert res_ref["trials"][0]["metrics"] == {"score": 0.0}

        assert res_sup["campaign_id"] != res_ref["campaign_id"]

        # Verification via WorkClient.research GET against base_url
        client = WorkClient(base_url, workspace_id, capability_path)
        try:
            view = client.research(item["key"])
            job_store = NativeJobStore(service.config)

            for result in (res_sup, res_ref):
                campaign_id = UUID(result["campaign_id"])
                # WorkClient.research GET selects returned campaign_id (never campaigns[0])
                matching_campaigns = [c for c in view.campaigns if c.campaign_id == campaign_id]
                assert len(matching_campaigns) == 1
                campaign = matching_campaigns[0]

                assert campaign.state == result["state"] == "concluded"
                assert campaign.outcome == result["outcome"]
                assert campaign.outcome_reason == result["reason"]

                # matching observations contain scores; requested jobs sealed
                for trial_info in result["trials"]:
                    trial_id = trial_info["trial_id"]
                    matching_obs = [o for o in view.observations if str(o.trial_id) == str(trial_id)]
                    assert len(matching_obs) == 1
                    obs = matching_obs[0]
                    assert obs.campaign_id == campaign_id
                    assert obs.payload["metrics"] == trial_info["metrics"]

                    with job_store.transaction(workspace_id, actor_id) as cur:
                        job = job_store.job_view(cur, workspace_id, trial_job_id(trial_id))
                    assert job["status"] == "sealed"
                    assert job["settlement"]["outcome"] == "succeeded"
        finally:
            client.close()
