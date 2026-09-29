"""Worker extension imports resolve @oh-my-pi/pi-coding-agent from /workspace.

Defends the contracts:
1. Dockerfile.agent links /workspace/node_modules into /home/agent/node_modules.
2. In the worker setup, every installed extension under /home/agent/.omp/agent/extensions
   (specifically workflow/audit-tcb.ts) resolves @oh-my-pi/pi-coding-agent and its
   subpaths to the /workspace build without network access.
3. Without the workspace node_modules link, the copied extension fails to resolve
   @oh-my-pi/pi-coding-agent (verifying the failure mode).
4. In the worker container image (as user agent), bun imports
   /home/agent/.omp/agent/extensions/workflow/audit-tcb.ts and executes getExecutorSha().
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARBOR = Path(__file__).resolve().parents[1]
REPO = HARBOR.parents[1]
DOCKERFILE = HARBOR / "docker" / "Dockerfile.agent"
INSTALL = REPO / "session-system" / "install.sh"

EXTENSION_ENTRIES = (
    "work-now.ts",
    "model-bookends.ts",
    "workflow/audit-tcb.ts",
    "workflow/host.ts",
    "workflow/work.ts",
)


def test_dockerfile_agent_links_workspace_node_modules() -> None:
    """Dockerfile.agent links /workspace/node_modules into /home/agent/node_modules."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "install.sh --copy" in text
    assert "ln -s /workspace/node_modules /home/agent/node_modules" in text


def test_copied_extensions_resolve_workspace_coding_agent(tmp_path: Path) -> None:
    """Worker setup with /home/agent/node_modules -> workspace resolves audit-tcb and task."""
    node_modules = REPO / "node_modules"
    if not node_modules.is_dir():
        raise AssertionError("node_modules is required to verify imports")

    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (home / "node_modules").symlink_to(node_modules)

    install = subprocess.run(
        ["bash", str(INSTALL), "--copy"],
        cwd=str(REPO),
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr

    # Probe 1: await import of audit-tcb.ts and getExecutorSha()
    probe_code = """
    import { getExecutorSha } from "./workflow/audit-tcb.ts";
    const sha = getExecutorSha();
    if (!sha || typeof sha !== "string") {
      process.exit(1);
    }
    """
    ext_dir = home / ".omp" / "agent" / "extensions"
    probe = subprocess.run(
        ["bun", "-e", probe_code],
        cwd=str(ext_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr

    # Probe 2: check all extension entries import cleanly
    import_all_code = "\n".join(
        f'await import("{ext_dir / entry}");' for entry in EXTENSION_ENTRIES
    )
    probe_all = subprocess.run(
        ["bun", "-e", import_all_code],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe_all.returncode == 0, probe_all.stderr

    # Negative contract: without the home node_modules link, audit-tcb.ts import fails
    (home / "node_modules").unlink()
    negative_probe = subprocess.run(
        ["bun", "-e", f'await import("{ext_dir / "workflow" / "audit-tcb.ts"}")'],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=False,
    )
    assert negative_probe.returncode != 0
    assert "Cannot find module '@oh-my-pi/pi-coding-agent" in negative_probe.stderr


def test_worker_image_extension_imports_as_agent() -> None:
    """In the built worker image, audit-tcb and extensions import successfully as user agent."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        pytest.skip("docker binary not found")

    image = "omp-agent:dev"
    inspect = subprocess.run(
        [docker_bin, "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        image = "omp-f1-agent:dev"
        inspect = subprocess.run(
            [docker_bin, "image", "inspect", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspect.returncode != 0:
            pytest.skip(f"neither omp-agent:dev nor omp-f1-agent:dev found in docker")

    # Command matching acceptance criteria: bun -e "await import('/home/agent/.omp/agent/extensions/workflow/audit-tcb.ts')"
    cmd = (
        'bun -e "'
        "await import('/home/agent/.omp/agent/extensions/workflow/audit-tcb.ts');"
        '"'
    )
    run_audit = subprocess.run(
        [docker_bin, "run", "--rm", "-u", "agent", image, "sh", "-c", cmd],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert run_audit.returncode == 0, f"audit-tcb import failed:\n{run_audit.stderr}"

    # Verify all extension entries in the image
    entries_check = "; ".join(
        f"await import('/home/agent/.omp/agent/extensions/{entry}')"
        for entry in EXTENSION_ENTRIES
    )
    cmd_all = f'bun -e "{entries_check}"'
    run_all = subprocess.run(
        [docker_bin, "run", "--rm", "-u", "agent", image, "sh", "-c", cmd_all],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert run_all.returncode == 0, f"extension entries import failed:\n{run_all.stderr}"
