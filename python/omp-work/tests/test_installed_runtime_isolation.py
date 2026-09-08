"""Qualify installed process boundaries against disposable state, without model calls.

Run explicitly with OMP_INSTALLED_RELEASE and OMP_INSTALLED_MANIFEST_SHA256.
These tests do not establish execution-cycle recovery or live-provider readiness.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from omp_work.operations.config import OperationsConfig
from pg_native import native_postgres, seed_authority


@dataclass(frozen=True)
class InstalledRelease:
    root: Path
    digest: str

    def command(self, state: Path, workspace: Path, *arguments: str) -> list[str]:
        return [
            str(self.root / "bin/omp"),
            str(state),
            str(workspace),
            self.digest,
            *arguments,
        ]


@pytest.fixture
def installed_release() -> InstalledRelease:
    root = os.environ.get("OMP_INSTALLED_RELEASE")
    digest = os.environ.get("OMP_INSTALLED_MANIFEST_SHA256")
    if not root or not digest:
        pytest.skip(
            "installed-process qualification requires OMP_INSTALLED_RELEASE "
            "and OMP_INSTALLED_MANIFEST_SHA256"
        )
    release = InstalledRelease(Path(root).resolve(strict=True), digest)
    assert (release.root / "bin/omp").is_file(), "selected release lacks its launcher"
    return release


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(arguments: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        arguments, cwd=cwd, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stderr[-6000:]
    return result.stdout


@contextlib.contextmanager
def _process(
    arguments: list[str], cwd: Path, env: dict[str, str], log: Path
) -> Generator[subprocess.Popen[str]]:
    with log.open("w") as stderr:
        child = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            yield child
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=10)
            if child.stdin:
                child.stdin.close()
            if child.stdout:
                child.stdout.close()


class RpcProcess:
    def __init__(self, child: subprocess.Popen[str], log: Path):
        self.child = child
        self.log = log
        self.events: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.child.stdout is not None
        try:
            for line in self.child.stdout:
                self.events.put(line)
        except (OSError, ValueError):
            # Process cleanup may close stdout while this reader observes EOF.
            pass
        finally:
            self.events.put(None)

    def send(self, command: str, **fields: object) -> str:
        request_id = str(uuid4())
        assert self.child.stdin is not None
        self.child.stdin.write(
            json.dumps({"id": request_id, "type": command, **fields}) + "\n"
        )
        self.child.stdin.flush()
        return request_id

    def until(
        self, predicate: Callable[[dict[str, object]], bool]
    ) -> dict[str, object]:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                line = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                pytest.fail(
                    f"installed RPC process exited: {self.log.read_text()[-6000:]}"
                )
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                pytest.fail(f"installed RPC emitted non-JSON stdout: {line[:400]}")
            assert isinstance(event, dict)
            if event.get("type") == "response" and event.get("success") is False:
                pytest.fail(f"installed RPC refused request: {event}")
            if predicate(event):
                return event
        pytest.fail(f"installed RPC response timed out: {self.log.read_text()[-6000:]}")

    def request(self, command: str) -> dict[str, object]:
        request_id = self.send(command)
        response = self.until(
            lambda event: (
                event.get("type") == "response" and event.get("id") == request_id
            )
        )
        assert response["success"] is True
        return cast(dict[str, object], response.get("data", {}))

    def work_status(self) -> None:
        request_id = self.send("prompt", message="/work status")
        notice = self.until(
            lambda event: (
                event.get("type") == "extension_ui_request"
                and event.get("method") == "notify"
                and "service:" in str(event.get("message", ""))
            )
        )
        message = str(notice["message"])
        assert "service: ready" in message, message
        assert "authority: work" in message, message
        result = self.until(
            lambda event: (
                event.get("type") == "prompt_result" and event.get("id") == request_id
            )
        )
        assert result["agentInvoked"] is False, "read-only work command invoked a model"


def _assert_work_routes(rpc: RpcProcess) -> dict[str, object]:
    commands = cast(
        list[dict[str, object]], rpc.request("get_available_commands")["commands"]
    )
    assert {"work", "execute", "done"}.issubset(
        {command["name"] for command in commands}
    )
    assert "candidate-poison" not in {command["name"] for command in commands}
    state = rpc.request("get_state")
    # Extension tools may be mounted under xd:// rather than published as
    # top-level model tools. Exercise the service-backed command below.
    assert cast(dict[str, object], state["model"])["provider"] == "qualification"
    assert state["isStreaming"] is False
    rpc.work_status()
    return state


def _health(
    base_url: str, child: subprocess.Popen[str], log: Path
) -> dict[str, object]:
    deadline = time.monotonic() + 90
    with httpx.Client(timeout=2, trust_env=False) as client:
        while time.monotonic() < deadline:
            assert child.poll() is None, log.read_text()[-6000:]
            try:
                response = client.get(f"{base_url}/v1/health/ready")
                response.raise_for_status()
                result = response.json()
                if result["ready"]:
                    assert result["live"] is True
                    # Fresh disposable databases have no backup or restore history.
                    # Those operational notices are expected; runtime/schema alerts are not.
                    assert set(result["alerts"]) <= {
                        "BACKUP_MISSING",
                        "RESTORE_DRILL_MISSING",
                    }
                    return result
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.1)
    pytest.fail(f"installed service did not become ready: {log.read_text()[-6000:]}")


def test_candidate_edits_and_inherited_config_do_not_change_installed_processes(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """Editing controller/service candidates must not stale serving code or change new workers."""
    release = installed_release
    state = tmp_path / "runtime"
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(foreign),
        "XDG_CONFIG_HOME": str(foreign / "config"),
        "PI_CODING_AGENT_DIR": str(foreign / ".omp/agent"),
        "OMP_DISCOVERY_CWD": str(candidate),
        "OMP_WORK_BEARER": "qualification-hostile-inherited-token",
        "PYTHONPATH": str(candidate / "python"),
        "NODE_OPTIONS": "--require=/does-not-exist/qualification-poison.js",
    }
    pg_port, http_port = _free_port(), _free_port()
    service_args = ("--service", "--postgres-port", str(pg_port))

    def run_service(*arguments: str) -> str:
        return _run(
            release.command(state, candidate, *service_args, *arguments), candidate, env
        )

    smoke = _run(release.command(state, candidate, "--smoke-test"), candidate, env)
    assert "smoke-test: ok" in smoke
    # The next launcher invocation revalidates every file, catching any worker
    # that wrote generated assets back into the supposedly immutable install.
    identity = json.loads(run_service("ops", "credentials", "init"))
    python_identity = json.loads(
        _run(
            [
                str(release.root / "python/bin/python"),
                "-I",
                "-B",
                str(release.root / "source/session-system/runtime/python-identity.py"),
            ],
            candidate,
            env,
        )
    )
    assert python_identity["editable"] is False
    assert Path(python_identity["serviceModule"]).is_relative_to(
        release.root / "python"
    )
    config = OperationsConfig(
        config_dir=state / "config/omp/work-ledger",
        state_dir=state / "state/omp/work-ledger",
        data_dir=state / "data/omp/work-ledger",
        port=pg_port,
    )
    base_url = f"http://127.0.0.1:{http_port}"
    with native_postgres(tmp_path / "postgres", pg_port):
        run_service("ops", "bootstrap")
        seed_authority(
            config.connection_kwargs("postgres"),
            UUID(identity["workspace_id"]),
            UUID(identity["owner_id"]),
        )
        run_service(
            "ops",
            "capabilities",
            "init",
            "--workspace-id",
            identity["workspace_id"],
            "--owner-id",
            identity["owner_id"],
            "--base-url",
            base_url,
        )
        # A local-only route supplies model metadata; no reasoning request is sent.
        models = state / "home/.omp/agent/models.yml"
        models.write_text(
            json.dumps(
                {
                    "providers": {
                        "qualification": {
                            "baseUrl": "http://127.0.0.1:9/v1",
                            "api": "openai-completions",
                            "apiKey": "qualification-only",
                            "models": [
                                {
                                    "id": "no-reasoning",
                                    "name": "No reasoning",
                                    "reasoning": False,
                                    "input": ["text"],
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                    "contextWindow": 8192,
                                    "maxTokens": 1024,
                                }
                            ],
                        },
                    },
                }
            )
        )
        service_log = tmp_path / "service.stderr"
        cli_log = tmp_path / "cli-before.stderr"
        command = release.command(
            state,
            candidate,
            "--mode",
            "rpc",
            "--no-session",
            "--provider",
            "qualification",
            "--model",
            "no-reasoning",
        )
        service_command = release.command(
            state,
            candidate,
            *service_args,
            "serve",
            "--port",
            str(http_port),
            "--capabilities-dir",
            str(config.config_dir / "capabilities"),
        )
        with _process(service_command, candidate, env, service_log) as service:
            before = _health(base_url, service, service_log)
            assert (
                before["service_fingerprint"] == python_identity["serviceFingerprint"]
            )
            manifest = json.loads((release.root / "manifest.json").read_text())
            assert (
                before["service_fingerprint"]
                == manifest["python"]["serviceFingerprint"]
            )
            with _process(command, candidate, env, cli_log) as cli:
                rpc = RpcProcess(cli, cli_log)
                prior_state = _assert_work_routes(rpc)
                # Candidate files are independently copied, then changed while
                # the admitted service and controller remain alive.
                manifest = json.loads((release.root / "manifest.json").read_text())
                module = manifest["python"]["serviceModule"]
                assert module == python_identity["serviceModule"]
                candidate_module = candidate / "python/omp_work"
                shutil.copytree(
                    Path(module).parent,
                    candidate_module,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                server_file = candidate_module / "v1/server.py"
                server_file.write_text(
                    server_file.read_text() + "\n# candidate service revision\n"
                )
                probe = (
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "from omp_work.operations.fingerprints import service_runtime_fingerprint; "
                    "print(service_runtime_fingerprint())"
                )
                candidate_fingerprint = _run(
                    [
                        str(release.root / "python/bin/python"),
                        "-I",
                        "-B",
                        "-c",
                        probe,
                        str(candidate / "python"),
                    ],
                    candidate,
                    env,
                ).strip()
                assert candidate_fingerprint != before["service_fingerprint"]
                shutil.copytree(
                    release.root / "source/session-system/extensions",
                    candidate / "session-system/extensions",
                )
                (candidate / "node_modules").symlink_to(
                    release.root / "source/node_modules"
                )
                for relative in (
                    "packages/coding-agent/src/cli.ts",
                    "session-system/extensions/workflow/host.ts",
                ):
                    target = candidate / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(
                        (release.root / "source" / relative).read_text()
                        + "\nthrow new Error('candidate controller revision');\n"
                    )
                candidate_child = subprocess.run(
                    [
                        str(release.root / "bin/bun"),
                        str(candidate / "session-system/extensions/workflow/host.ts"),
                    ],
                    cwd=candidate,
                    env={"PATH": env["PATH"], "HOME": str(foreign)},
                    capture_output=True,
                    text=True,
                    timeout=90,
                )
                assert candidate_child.returncode != 0
                assert "candidate controller revision" in candidate_child.stderr
                for root in (candidate / ".omp", foreign / ".omp/agent"):
                    (root / "extensions").mkdir(parents=True)
                    (root / "extensions/poison.ts").write_text(
                        "throw new Error('candidate-poison extension loaded');\n"
                    )
                    (root / "config.yml").write_text(
                        "modelRoles: [invalid candidate configuration\n"
                    )
                (candidate / ".env").write_text(
                    "OMP_WORK_BEARER=wrong-candidate-token\n"
                )
                assert _health(base_url, service, service_log) == before
                assert _assert_work_routes(rpc)["sessionId"] == prior_state["sessionId"]
                second_log = tmp_path / "cli-after.stderr"
                with _process(command, candidate, env, second_log) as replacement:
                    fresh = _assert_work_routes(RpcProcess(replacement, second_log))
                    assert fresh["sessionId"] != prior_state["sessionId"]
                assert _health(base_url, service, service_log) == before
                assert service.poll() is None


def test_corrupted_disposable_release_is_refused_before_runtime_launch(
    installed_release: InstalledRelease, tmp_path: Path
) -> None:
    """A valid relocated fixture launches; changing an admitted extension then refuses launch."""
    selected = installed_release
    copied = tmp_path / "release"
    shutil.copytree(selected.root, copied, symlinks=True)
    manifest_file = copied / "manifest.json"
    manifest = json.loads(manifest_file.read_text())
    old_prefix, new_prefix = str(selected.root), str(copied)
    manifest["releaseRoot"] = new_prefix
    manifest["python"]["serviceModule"] = (
        new_prefix + manifest["python"]["serviceModule"][len(old_prefix) :]
    )
    for item in manifest["files"]:
        if item["kind"] == "symlink" and item["target"].startswith(old_prefix + "/"):
            target = new_prefix + item["target"][len(old_prefix) :]
            link = copied / item["path"]
            link.unlink()
            link.symlink_to(target)
            item["target"] = target
            item["sha256"] = hashlib.sha256(target.encode()).hexdigest()
    manifest_file.chmod(stat.S_IMODE(manifest_file.stat().st_mode) | stat.S_IWUSR)
    manifest_file.write_text(json.dumps(manifest) + "\n")
    copied_release = InstalledRelease(
        copied, hashlib.sha256(manifest_file.read_bytes()).hexdigest()
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    state = tmp_path / "runtime"
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    command = copied_release.command(state, workspace, "--service", "hash")
    expected_contract = _run(command, workspace, env).strip()
    changed = copied / "source/session-system/extensions/work-now.ts"
    original_mode = stat.S_IMODE(changed.stat().st_mode)
    changed.chmod(original_mode | stat.S_IWUSR)
    changed.write_text(
        changed.read_text() + "\n// deliberately corrupted qualification copy\n"
    )
    changed.chmod(original_mode)
    refused = subprocess.run(
        command, cwd=workspace, env=env, capture_output=True, text=True, timeout=120
    )
    assert refused.returncode != 0
    assert "inventory changed" in refused.stderr.lower(), refused.stderr[-6000:]
    assert "work-now.ts" in refused.stderr
    assert expected_contract not in refused.stdout, (
        "service started despite changed extension bytes"
    )
    # Import-time transpilation must not create cache-only runtime directories
    # before the selected installation has passed admission (Bun 1.4 regression).
    fresh_state = tmp_path / "refused-fresh-runtime"
    refused_fresh = subprocess.run(
        copied_release.command(fresh_state, workspace, "--service", "hash"),
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert refused_fresh.returncode != 0
    assert "inventory changed" in refused_fresh.stderr.lower()
    assert not fresh_state.exists(), "bootstrap wrote runtime state before admission"
    assert (
        hashlib.sha256((selected.root / "manifest.json").read_bytes()).hexdigest()
        == selected.digest
    )
