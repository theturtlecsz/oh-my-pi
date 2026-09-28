"""Copied session extensions resolve @oh-my-pi/pi-work-client and register /execute.

Probe, omp-agent:dev, bun 1.4.0, 2026-09-28. Importing @oh-my-pi/pi-work-client
from outside the workspace failed in 0.010s with --network none and in 0.082s
when DNS answered ("Cannot find module"). With a nameserver that does not
answer (--dns 192.0.2.1) that import did not return within 18s, and
`omp --mode rpc` was still in loadExtensions at 10s and at 20s — the same
watchdog line as the f1/f2 trials. install.sh --copy now places the package
next to the copied extension so that lookup never leaves the machine.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_PATH = REPO_ROOT / "packages" / "coding-agent" / "src" / "cli.ts"
INSTALL = REPO_ROOT / "session-system" / "install.sh"

TEST_WORKSPACE_ID = "00000000-0000-4000-8000-0000000000aa"
TEST_OWNER_ID = "00000000-0000-4000-8000-0000000000bb"


def test_copied_extensions_list_execute_and_register_work(tmp_path: Path) -> None:
    """omp started against install.sh --copy (not the checkout) lists /execute and the work tool."""

    node_modules = REPO_ROOT / "node_modules"
    if not node_modules.is_dir():
        raise AssertionError("node_modules is required to start omp")

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".work-project").write_text("The Bookends\n", encoding="utf-8")

    installed = subprocess.run(
        ["bash", str(INSTALL), "--copy"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert installed.returncode == 0, installed.stderr
    copied = home / ".omp" / "agent" / "extensions"
    assert (copied / "work-now.ts").is_file()
    assert not (copied / "work-now.ts").is_symlink()
    assert (copied / "model-bookends.ts").is_file()
    assert not (copied / "model-bookends.ts").is_symlink()
    package = copied / "node_modules" / "@oh-my-pi" / "pi-work-client" / "package.json"
    assert package.is_file()
    assert not package.is_symlink()
    assert json.loads(package.read_text(encoding="utf-8"))["name"] == "@oh-my-pi/pi-work-client"

    config_dir = home / ".config" / "omp-work"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:9",
                "workspace_id": TEST_WORKSPACE_ID,
                "owner_id": TEST_OWNER_ID,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("XDG_CONFIG_HOME", None)
    env.pop("OMP_WORK_BEARER", None)
    proc = subprocess.Popen(
        ["bun", str(CLI_PATH), "--mode", "rpc"],
        cwd=str(workspace),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    commands: list[str] = []
    stderr_parts: list[str] = []

    def drain_stderr() -> None:
        if proc.stderr is None:
            return
        stderr_parts.append(proc.stderr.read())

    stderr_thread = threading.Thread(target=drain_stderr)
    stderr_thread.start()
    deadline = time.monotonic() + 30

    def read_until_tools() -> None:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                break
            line = proc.stdout.readline()
            if not line:
                break
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if frame.get("type") == "available_commands_update":
                for cmd in frame.get("commands", []):
                    name = cmd.get("name")
                    if isinstance(name, str):
                        commands.append(name)
            if frame.get("type") == "response" and frame.get("id") == "get_state_req":
                break
            if frame.get("type") == "ready":
                proc.stdin.write(json.dumps({"type": "get_state", "id": "get_state_req"}) + "\n")
                proc.stdin.flush()

    try:
        read_until_tools()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        stderr_thread.join(timeout=5)
    stderr = "".join(stderr_parts)

    assert "Failed to load extension" not in stderr, stderr
    assert "execute" in commands, f"commands={commands} stderr={stderr}"

    probe = subprocess.run(
        ["bun", "-e", PROBE],
        cwd=str(REPO_ROOT),
        env={**env, "WORKSPACE": str(workspace)},
        capture_output=True,
        text=True,
        check=False,
        timeout=40,
    )
    assert probe.returncode == 0, probe.stderr


PROBE = """
import { createAgentSession } from "./packages/coding-agent/src/sdk.ts";
const cwd = process.env.WORKSPACE;
const { session } = await createAgentSession({ cwd });
const names = session.getAllToolNames();
const registered = names.includes("work") && session.getToolByName("work") !== undefined;
await session.dispose();
if (!registered) {
  console.error(names.join(","));
  process.exit(1);
}
"""
