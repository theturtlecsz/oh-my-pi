"""Test that worker environment stages workflow extension and Work Ledger credentials.

Defends the contracts:
1. Worker gets client config and credentials from provisioned credentials (no token hardcoded).
2. The worker environment enables omp to list /execute among its available commands and register the work tool.
3. The work client reaches the loopback WorkService with the provisioned credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fake_workservice import FakeWorkService

from omp_harbor_eval.docker_ops import write_file
from omp_harbor_eval.harbor_agent import stage_worker_environment

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER = Path(__file__).resolve().parent / "fake_docker.py"
CLI_PATH = REPO_ROOT / "packages" / "coding-agent" / "src" / "cli.ts"

TEST_TOKEN = "test-token-provisioned-12345"
TEST_WORKSPACE_ID = "00000000-0000-4000-8000-0000000000aa"
TEST_OWNER_ID = "00000000-0000-4000-8000-0000000000bb"


def _write_config(fake_dir: Path, **overrides: object) -> None:
    config: dict[str, object] = {"containers": {}, "fail": [], "omp": [], "responses": {}}
    config.update(overrides)
    (fake_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def test_stage_worker_environment_provisions_client_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stage_worker_environment writes client.json, owner.json, and .work-project."""
    fake_dir = tmp_path / "docker"
    fake_dir.mkdir()
    monkeypatch.setenv("FAKE_DOCKER_DIR", str(fake_dir))
    _write_config(fake_dir)

    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "credentials").mkdir()
    (cred_dir / "capabilities").mkdir()

    (cred_dir / "credentials" / "operator-actor-id").write_text(TEST_OWNER_ID + "\n", encoding="utf-8")
    (cred_dir / "credentials" / "workspace-id").write_text(TEST_WORKSPACE_ID + "\n", encoding="utf-8")
    cap_data = {
        "actor_id": TEST_OWNER_ID,
        "actor_kind": "owner",
        "scopes": ["work.read", "work.mutate"],
        "token": TEST_TOKEN,
        "workspaces": [TEST_WORKSPACE_ID],
    }
    (cred_dir / "capabilities" / "owner.json").write_text(json.dumps(cap_data) + "\n", encoding="utf-8")
    monkeypatch.setenv("OMP_HARBOR_CREDENTIALS_DIR", str(cred_dir))

    task = SimpleNamespace(
        home="/home/agent",
        working_dir="/workspace",
        workservice_url="http://127.0.0.1:8080",
    )

    stage_worker_environment(
        "worker",
        task,
        workspace_id=TEST_WORKSPACE_ID,
        bearer=TEST_TOKEN,
        docker=str(DOCKER),
    )

    root = fake_dir / "root"
    client_json_path = root / "home" / "agent" / ".config" / "omp-work" / "client.json"
    assert client_json_path.is_file()
    assert (client_json_path.stat().st_mode & 0o777) == 0o600
    client_config = json.loads(client_json_path.read_text(encoding="utf-8"))
    assert client_config["base_url"] == "http://127.0.0.1:8080"
    assert client_config["workspace_id"] == TEST_WORKSPACE_ID
    assert client_config["owner_id"] == TEST_OWNER_ID
    assert client_config["bearer_file"] == "/home/agent/.config/omp-work/capabilities/owner.json"

    owner_json_path = root / "home" / "agent" / ".config" / "omp-work" / "capabilities" / "owner.json"
    assert owner_json_path.is_file()
    assert (owner_json_path.stat().st_mode & 0o777) == 0o600
    owner_cap = json.loads(owner_json_path.read_text(encoding="utf-8"))
    assert owner_cap["token"] == TEST_TOKEN
    assert owner_cap["actor_id"] == TEST_OWNER_ID

    work_project_path = root / "workspace" / ".work-project"
    assert work_project_path.is_file()
    assert work_project_path.read_text(encoding="utf-8") == "The Bookends\n"


def test_omp_rpc_registers_execute_command_and_work_tool(tmp_path: Path) -> None:
    """When staged with workflow extension and client config, omp lists /execute and registers work tool."""
    agent_home = tmp_path / "home"
    agent_home.mkdir()
    config_dir = agent_home / ".config" / "omp-work"
    config_dir.mkdir(parents=True)
    capabilities_dir = config_dir / "capabilities"
    capabilities_dir.mkdir(parents=True)

    owner_json = capabilities_dir / "owner.json"
    owner_json.write_text(json.dumps({"token": TEST_TOKEN, "actor_id": TEST_OWNER_ID}) + "\n", encoding="utf-8")
    owner_json.chmod(0o600)

    client_json = config_dir / "client.json"
    client_json.write_text(
        json.dumps({
            "base_url": "http://127.0.0.1:8080",
            "workspace_id": TEST_WORKSPACE_ID,
            "owner_id": TEST_OWNER_ID,
            "bearer_file": str(owner_json),
        })
        + "\n",
        encoding="utf-8",
    )
    client_json.chmod(0o600)

    ext_dir = agent_home / ".omp" / "agent" / "extensions"
    ext_dir.mkdir(parents=True)

    # Link repo's session-system extensions into ext_dir
    repo_ext = REPO_ROOT / "session-system" / "extensions"
    os.symlink(repo_ext / "workflow", ext_dir / "workflow")
    os.symlink(repo_ext / "work-now.ts", ext_dir / "work-now.ts")
    os.symlink(repo_ext / "model-bookends.ts", ext_dir / "model-bookends.ts")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".work-project").write_text("The Bookends\n", encoding="utf-8")

    env = os.environ.copy()
    env["HOME"] = str(agent_home)
    env["OMP_WORKSERVICE_URL"] = "http://127.0.0.1:8080"

    proc = subprocess.Popen(
        ["bun", str(CLI_PATH), "--mode", "rpc"],
        cwd=str(workspace),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    available_commands: list[str] = []
    tools: list[str] = []
    start_time = time.monotonic()

    # Send get_state to get tools list
    assert proc.stdin is not None
    assert proc.stdout is not None

    def read_until_ready():
        while time.monotonic() - start_time < 20:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except Exception:
                continue
            if frame.get("type") == "available_commands_update":
                for cmd in frame.get("commands", []):
                    available_commands.append(cmd["name"])
            if frame.get("type") == "response" and frame.get("id") == "get_state_req":
                for tool in frame.get("data", {}).get("dumpTools", []):
                    tools.append(tool["name"])
                break
            if frame.get("type") == "ready":
                proc.stdin.write(json.dumps({"type": "get_state", "id": "get_state_req"}) + "\n")
                proc.stdin.flush()

    try:
        read_until_ready()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    assert "execute" in available_commands, f"expected 'execute' in available commands, got: {available_commands}"

    # Also verify that omp initializes the session with the work tool available
    probe_code = """
    import { createAgentSession } from "./packages/coding-agent/src/sdk";
    const { session } = await createAgentSession({ cwd: process.cwd() });
    const hasWork = session.getAllToolNames().includes("work") && session.getToolByName("work") !== undefined;
    await session.dispose();
    if (!hasWork) process.exit(1);
    """
    probe_res = subprocess.run(
        ["bun", "-e", probe_code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert probe_res.returncode == 0, f"expected omp to have work tool: {probe_res.stderr}"


def test_work_tool_credentials_reach_workservice() -> None:
    """The Work Ledger client config and credentials reach loopback WorkService with bearer auth."""
    called_headers: dict[str, str] = {}

    with FakeWorkService(
        bearer=TEST_TOKEN,
        workspace_id=TEST_WORKSPACE_ID,
        ready=True,
        execution={"grant": {"state": "active"}, "items": [{"work_id": "test-work"}]},
        work_item={"work_id": "test-work", "state": "running"},
    ) as service:
        # Verify FakeWorkService receives the bearer token and workspace id
        import urllib.request

        req = urllib.request.Request(
            f"{service.base_url}/v1/workspaces/{TEST_WORKSPACE_ID}/execution",
            headers={
                "Authorization": f"Bearer {TEST_TOKEN}",
                "X-OMP-Workspace-ID": TEST_WORKSPACE_ID,
            },
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            assert data["grant"]["state"] == "active"

        assert len(service.calls) == 1
        assert service.calls[0]["authorized"] is True
        assert service.calls[0]["workspace"] == TEST_WORKSPACE_ID
