"""Local /execute integration stand running WorkService and omp without Docker.

Reuses the setup in tests/test_execute_admission.py:
- native postgres, bootstrap, ledger_seed of fixtures/f2/ledger-seed.json, and a uvicorn WorkService;
- a git workspace with its origin set by docker/worker-origin.sh, and `.work-project` containing `The Bookends`;
- HOME from `install.sh --copy` plus client.json;
- docker/worker-gh.sh as `gh` on PATH;
- `scripted_model.ScriptedModelServer` on 127.0.0.1 for a given script;
- `HOME/.omp/agent/models.yml` from `models_yml`, and `HOME/.omp/agent/config.yml` from `harbor_agent.worker_settings_yml` (s02).

The helper yields a way to run an `RpcAdapter` wired like `harbor_agent.run`:
- command `bun packages/coding-agent/src/cli.ts --mode rpc --model scripted/scripted --session-dir <HOME>/omp-sessions`, cwd = workspace;
- `ServiceProbe` with the owner bearer; killer `kill_process_group`; the default host session_reader.
Also exposes the model log path. Stops every process it started.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

from omp_harbor_eval import EvidenceWriter, RpcAdapter, Scenario, ServiceProbe
from omp_harbor_eval.adapter import kill_process_group
from omp_harbor_eval.harbor_agent import worker_settings_yml
from omp_harbor_eval.ledger_seed import main as seed_main
from omp_harbor_eval.scripted_model import ScriptedModelServer, models_yml
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap

HARBOR = Path(__file__).resolve().parents[1]
REPO = HARBOR.parents[1]
ORIGIN_SCRIPT = HARBOR / "docker" / "worker-origin.sh"
GH_SCRIPT = HARBOR / "docker" / "worker-gh.sh"
CLI_PATH = REPO / "packages" / "coding-agent" / "src" / "cli.ts"
INSTALL = REPO / "session-system" / "install.sh"
F2_SEED = HARBOR / "fixtures" / "f2" / "ledger-seed.json"
PROJECT_NAME = "The Bookends"

# Ensure test dependencies from python/omp-work and evals/harbor/tests are importable
_PYTHON_TESTS = REPO / "python" / "omp-work" / "tests"
if str(_PYTHON_TESTS) not in sys.path:
    sys.path.insert(0, str(_PYTHON_TESTS))
_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from installed_runtime_support import ReservedPort  # noqa: E402
from pg_native import native_postgres  # noqa: E402
from test_ledger_seed import ACTOR_ID, WORKSPACE_ID, _credentials  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    run = subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)
    assert run.returncode == 0, f"git {' '.join(args)}: {run.stderr}"
    return run.stdout.strip()


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_live(port: int, process: subprocess.Popen[str], log: Path) -> None:
    deadline = time.monotonic() + 30
    url = f"http://127.0.0.1:{port}/v1/health/live"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(log.read_text(encoding="utf-8")[-4000:])
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(0.1)
    raise AssertionError(f"work service did not become live\n{log.read_text(encoding='utf-8')[-4000:]}")


class ExecuteStand:
    """Handle to a running local execute stand.

    Yields a way to run an ``RpcAdapter`` wired like ``harbor_agent.run``:
    - command ``bun packages/coding-agent/src/cli.ts --mode rpc --model scripted/scripted --session-dir <HOME>/omp-sessions``, cwd = workspace;
    - ``ServiceProbe`` with the owner bearer; killer ``kill_process_group``; the default host session_reader.
    Also exposes the model log path.
    """

    def __init__(
        self,
        *,
        root: Path,
        workspace: Path,
        home: Path,
        model_log: Path,
        probe: ServiceProbe,
        omp_env: dict[str, str],
        http_port: int,
        owner_bearer: str,
        workspace_id: str,
        server_process: subprocess.Popen[str] | None = None,
        model_server: ScriptedModelServer | None = None,
    ) -> None:
        self.root = root
        self._workspace = workspace
        self._home = home
        self._model_log_path = model_log
        self._probe = probe
        self.omp_env = omp_env
        self.http_port = http_port
        self.owner_bearer = owner_bearer
        self.workspace_id = workspace_id
        self._server_process = server_process
        self._model_server = model_server
        self.last_adapter: RpcAdapter | None = None
        self.last_outcome: str | None = None
        self.last_evidence_dir: Path | None = None

    @property
    def model_log(self) -> Path:
        return self._model_log_path

    @property
    def model_log_path(self) -> Path:
        return self._model_log_path

    @property
    def workspace(self) -> Path:
        return self._workspace

    @property
    def home(self) -> Path:
        return self._home

    @property
    def probe(self) -> ServiceProbe:
        return self._probe

    def create_adapter(
        self,
        scenario: Scenario,
        evidence: EvidenceWriter | Path | str | None = None,
        *,
        ui_script: Any = None,
        before_seal: Any = None,
        killer: Any = None,
        ready_timeout_s: float = 30.0,
    ) -> RpcAdapter:
        if evidence is None:
            evidence_dir = self.root / "evidence" / f"run-{uuid.uuid4().hex[:8]}"
            writer = EvidenceWriter(
                evidence_dir,
                run_id=f"run-{uuid.uuid4().hex[:8]}",
                nonce=uuid.uuid4().hex,
                fixture_id="f2",
                fixture_digest="0" * 64,
                variant="known_good",
                experiment="repair",
            )
        elif isinstance(evidence, EvidenceWriter):
            writer = evidence
        else:
            evidence_dir = Path(evidence)
            writer = EvidenceWriter(
                evidence_dir,
                run_id=f"run-{uuid.uuid4().hex[:8]}",
                nonce=uuid.uuid4().hex,
                fixture_id="f2",
                fixture_digest="0" * 64,
                variant="known_good",
                experiment="repair",
            )
        self.last_evidence_dir = writer.directory

        command = [
            "bun",
            str(CLI_PATH),
            "--mode",
            "rpc",
            "--model",
            "scripted/scripted",
            "--session-dir",
            str(self._home / "omp-sessions"),
        ]
        adapter_killer = killer
        if adapter_killer is None:
            def adapter_killer(proc: subprocess.Popen[str]) -> None:
                if (
                    self._model_server is not None
                    and scenario.kill_at
                    and scenario.kill_at.boundary == "enqueue"
                ):
                    deadline = time.monotonic() + 5.0
                    while self._model_server._ordinal == 0 and time.monotonic() < deadline:
                        time.sleep(0.005)
                kill_process_group(proc)

        adapter = RpcAdapter(
            command=command,
            cwd=self._workspace,
            env=self.omp_env,
            probe=self._probe,
            evidence=writer,
            scenario=scenario,
            ui_script=ui_script,
            killer=adapter_killer,
            session_reader=None,
            before_seal=before_seal,
            ready_timeout_s=ready_timeout_s,
        )
        self.last_adapter = adapter
        return adapter

    def run(
        self,
        scenario: Scenario,
        evidence: EvidenceWriter | Path | str | None = None,
        **kwargs: Any,
    ) -> str:
        adapter = self.create_adapter(scenario, evidence, **kwargs)
        outcome = adapter.run()
        self.last_outcome = outcome
        return outcome

    def __call__(
        self,
        scenario: Scenario,
        evidence: EvidenceWriter | Path | str | None = None,
        **kwargs: Any,
    ) -> str:
        return self.run(scenario, evidence, **kwargs)

    def stop(self) -> None:
        if self._server_process is not None:
            _stop(self._server_process)
            self._server_process = None
        if self._model_server is not None:
            self._model_server.close()
            self._model_server = None

    def close(self) -> None:
        self.stop()


@contextlib.contextmanager
def execute_stand(
    root_or_script: Path | str | Sequence[dict[str, Any]] | None = None,
    script_or_root: Sequence[dict[str, Any]] | Path | str | None = None,
    *,
    root: Path | str | None = None,
    script: Sequence[dict[str, Any]] | Path | str | None = None,
    tmp_path: Path | str | None = None,
) -> Generator[ExecuteStand, None, None]:
    """Context manager running the real /execute stack locally without Docker."""

    resolved_root: Path | None = None
    if root is not None:
        resolved_root = Path(root)
    elif tmp_path is not None:
        resolved_root = Path(tmp_path)

    resolved_script: Sequence[dict[str, Any]] | Path | str | None = script

    if resolved_root is None:
        if isinstance(root_or_script, (list, tuple)):
            resolved_script = root_or_script
            if isinstance(script_or_root, (Path, str)):
                resolved_root = Path(script_or_root)
        elif isinstance(root_or_script, (Path, str)):
            if isinstance(script_or_root, (list, tuple)) or resolved_script is not None:
                resolved_root = Path(root_or_script)
                if resolved_script is None and isinstance(script_or_root, (list, tuple, str, Path)):
                    resolved_script = script_or_root
            elif isinstance(script_or_root, (Path, str)):
                resolved_root = Path(root_or_script)
                resolved_script = script_or_root
            else:
                p = Path(root_or_script)
                if p.is_dir() or not str(p).endswith(".json"):
                    resolved_root = p
                else:
                    resolved_script = p

    if resolved_script is None:
        if isinstance(script_or_root, (list, tuple, str, Path)):
            resolved_script = script_or_root
        elif isinstance(root_or_script, (list, tuple)):
            resolved_script = root_or_script

    if resolved_script is None:
        raise ValueError("execute_stand requires a script")

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if resolved_root is None:
        temp_dir = tempfile.TemporaryDirectory()
        root_path = Path(temp_dir.name)
    else:
        root_path = resolved_root

    root_path.mkdir(parents=True, exist_ok=True)

    if isinstance(resolved_script, (list, tuple)):
        script_path = root_path / "model_script.json"
        script_path.write_text(json.dumps(list(resolved_script)), encoding="utf-8")
    elif isinstance(resolved_script, (str, Path)):
        p = Path(resolved_script)
        if p.is_file():
            script_path = p
        else:
            try:
                parsed = json.loads(str(resolved_script))
                script_path = root_path / "model_script.json"
                script_path.write_text(json.dumps(parsed), encoding="utf-8")
            except json.JSONDecodeError as exc:
                raise ValueError(f"script file not found: {resolved_script}") from exc
    else:
        raise ValueError(f"unsupported script type: {type(resolved_script)}")

    # 1. Git workspace with origin set by worker-origin.sh and .work-project
    workspace = root_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _git(workspace, "init", "-q", "-b", "main")
    _git(workspace, "config", "user.email", "omp-harbor@localhost")
    _git(workspace, "config", "user.name", "omp-harbor")
    (workspace / "README").write_text("seeded tree\n", encoding="utf-8")
    _git(workspace, "add", "README")
    _git(workspace, "commit", "-q", "-m", "initial harbor worker tree")

    origin = root_path / "origin.git"
    setup = subprocess.run(
        ["sh", str(ORIGIN_SCRIPT), str(workspace), str(origin)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert setup.returncode == 0, setup.stderr
    (workspace / ".work-project").write_text(f"{PROJECT_NAME}\n", encoding="utf-8")

    # 2. HOME from install.sh --copy
    home = root_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    installed = subprocess.run(
        ["bash", str(INSTALL), "--copy"],
        cwd=str(REPO),
        env={**os.environ, "HOME": str(home)},
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stderr

    # Symlink node_modules into home if available
    node_modules_target = REPO / "node_modules"
    if node_modules_target.is_dir() and not (home / "node_modules").exists():
        (home / "node_modules").symlink_to(node_modules_target)

    # 3. docker/worker-gh.sh as gh on PATH
    gh_dir = root_path / "bin"
    gh_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GH_SCRIPT, gh_dir / "gh")
    (gh_dir / "gh").chmod(0o755)

    # 4. ScriptedModelServer on 127.0.0.1 for given script
    model_log = root_path / "model.jsonl"
    model_server = ScriptedModelServer(script_path, model_log, host="127.0.0.1")
    model_server.start()

    # 5. HOME/.omp/agent/models.yml and HOME/.omp/agent/config.yml
    agent_dir = home / ".omp" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.yml").write_text(models_yml(model_server.base_url), encoding="utf-8")
    (agent_dir / "config.yml").write_text(worker_settings_yml(), encoding="utf-8")

    # 6. Native postgres, bootstrap, ledger_seed of fixtures/f2/ledger-seed.json, and uvicorn WorkService
    pg = ReservedPort()
    http = ReservedPort()
    orig_env = os.environ.copy()
    os.environ["XDG_CONFIG_HOME"] = str(root_path / "xdg")
    os.environ["OMP_WORK_POSTGRES_PORT"] = str(pg.port)

    config = OperationsConfig.defaults()
    owner = _credentials(config)

    client_config = {
        "base_url": f"http://127.0.0.1:{http.port}",
        "workspace_id": str(WORKSPACE_ID),
        "owner_id": str(ACTOR_ID),
        "bearer_file": str(owner),
    }
    client_json_text = json.dumps(client_config, indent=2) + "\n"

    client_dir = home / ".config" / "omp-work"
    client_dir.mkdir(parents=True, exist_ok=True)
    (client_dir / "client.json").write_text(client_json_text, encoding="utf-8")

    # Also place client.json in root/xdg/omp-work so client is found even if XDG_CONFIG_HOME is set
    xdg_client_dir = root_path / "xdg" / "omp-work"
    xdg_client_dir.mkdir(parents=True, exist_ok=True)
    (xdg_client_dir / "client.json").write_text(client_json_text, encoding="utf-8")

    server_log = root_path / "workservice.log"
    server: subprocess.Popen[str] | None = None
    server_env = os.environ.copy()
    server_env["XDG_CONFIG_HOME"] = str(root_path / "xdg")
    server_env["OMP_WORK_POSTGRES_PORT"] = str(pg.port)

    try:
        with native_postgres(root_path / "pg", pg.port, reserve=pg):
            bootstrap(config)
            assert seed_main([str(F2_SEED)]) == 0

            log_handle = server_log.open("w", encoding="utf-8")
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import uvicorn\n"
                    "from pathlib import Path\n"
                    "from omp_work.operations.config import OperationsConfig\n"
                    "from omp_work.v1.server import create_app\n"
                    "app = create_app(OperationsConfig.defaults(), capabilities_dir=Path(r'''"
                    + str(owner.parent)
                    + "'''))\n"
                    f"uvicorn.run(app, host='127.0.0.1', port={http.port}, access_log=False)\n",
                ],
                env=server_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            log_handle.close()
            _wait_live(http.port, server, server_log)
            http.close()

            # Pop XDG_CONFIG_HOME and OMP_WORK_BEARER from os.environ so omp inherits clean env
            os.environ.pop("XDG_CONFIG_HOME", None)
            os.environ.pop("OMP_WORK_BEARER", None)

            omp_env = os.environ.copy()
            omp_env["HOME"] = str(home)
            omp_env["PATH"] = f"{gh_dir}{os.pathsep}{omp_env.get('PATH', '')}"
            omp_env.pop("XDG_CONFIG_HOME", None)
            omp_env.pop("OMP_WORK_BEARER", None)

            probe = ServiceProbe(
                base_url=f"http://127.0.0.1:{http.port}",
                bearer="owner-token",
                workspace_id=str(WORKSPACE_ID),
            )

            stand = ExecuteStand(
                root=root_path,
                workspace=workspace,
                home=home,
                model_log=model_log,
                probe=probe,
                omp_env=omp_env,
                http_port=http.port,
                owner_bearer="owner-token",
                workspace_id=str(WORKSPACE_ID),
                server_process=server,
                model_server=model_server,
            )
            yield stand
    finally:
        if server is not None:
            _stop(server)
        model_server.close()
        http.close()
        os.environ.clear()
        os.environ.update(orig_env)
        if temp_dir is not None:
            temp_dir.cleanup()
