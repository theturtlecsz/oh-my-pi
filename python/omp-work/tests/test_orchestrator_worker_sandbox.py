"""OMP-417: the worker jail sees one linked worktree and the WorkService relay.

Reads of a sibling worktree, the owner's home, the control plane's config,
capabilities and credentials, every planted credential path, and the control
plane's /proc environ and mem fail. The environment is the six allowed names.
A second loopback listener and the Postgres port are unreachable. The
WorkService relay answers, /work is writable, and the live checkout is refused.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from omp_work.operations.config import OperationsConfig
from omp_work.orchestrator.worker_sandbox import WorkerSandboxRefused, run_worker

_ALLOWED_ENV = ("PATH", "LANG", "TERM", "TZ", "OMP_TASK_TOKEN", "OMP_WORK_URL")
_TOKEN = "task-token-417"
_PARENT_SECRETS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "SSH_AUTH_SOCK",
)
_PLANTED = (
    ".ssh/id_ed25519",
    ".ssh/id_rsa",
    ".git-credentials",
    ".gitconfig",
    ".config/git/credentials",
    ".config/omp/work-ledger/capabilities/automation.json",
    ".config/omp/work-ledger/credentials/database",
    ".config/omp/owner-signing",
    ".config/gh/hosts.yml",
    ".aws/credentials",
    ".config/gcloud/application_default_credentials.json",
    ".config/openai/auth.json",
    ".anthropic/api_key",
    ".netrc",
    ".claude.json",
    ".azure/credentials",
    ".kube/config",
    ".docker/config.json",
    ".omp/agent/secret",
    ".config/omp-work/client.json",
)


def _unshare_probe_fails() -> bool:
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=5,
        )
        return probe.returncode != 0
    except Exception:
        return True


pytestmark = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")


def _git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, env=_git_env())


def _bind_loopback() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(0.2)
    return sock


class _WorkServiceStub:
    """Tiny HTTP listener. The jail reaches it only through the relay."""

    def __init__(self) -> None:
        self._sock = _bind_loopback()
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        if self._thread.ident is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            try:
                conn.settimeout(1)
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                body = b"ws-ok"
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\n" + body
                )
            except OSError:
                pass
            finally:
                conn.close()


def _plant(home: Path) -> Path:
    """Credential-shaped files under a temporary home. Returns the agent socket."""
    for relative in _PLANTED:
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("planted-secret\n", encoding="utf-8")
    (home / ".gitconfig").write_text("[credential]\n\thelper = store\n", encoding="utf-8")
    agent = home / ".ssh" / "agent.sock"
    agent.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(agent))
    sock.listen(1)
    sock.close()
    return agent


def _probe_paths(
    plant: Path,
    agent: Path,
    sibling: Path,
    live: Path,
    gitdir: Path,
    real_sock: str | None,
) -> list[str]:
    config = OperationsConfig.defaults()
    home = Path.home()
    paths = [str(plant / relative) for relative in _PLANTED]
    paths.append(str(agent))
    paths.extend(
        [
            str(sibling),
            str(sibling / "SECRET"),
            str(live),
            str(live / "LIVE"),
            str(gitdir),
            str(home),
            str(config.config_dir),
            str(config.config_dir / "capabilities"),
            str(config.credentials_dir),
            str(home / ".config" / "omp" / "owner-signing"),
            str(config.config_dir / "owner_allowed_signers"),
        ]
    )
    paths.extend(str(home / relative) for relative in _PLANTED)
    if real_sock:
        paths.append(real_sock)
    return paths


_PROBE = """
import json, os, socket, time, urllib.request
spec = json.loads(SPEC)
allowed = spec["allowed"]

def readable(path):
    try:
        if os.path.isdir(path):
            os.listdir(path)
            return True
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.read(fd, 1)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True

def proc_read(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return "denied"
    try:
        os.read(fd, 1)
    except OSError:
        return "denied"
    finally:
        os.close(fd)
    return "read"

def reach(port):
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()

leaks = []
proc = "/proc"
if os.path.isdir(proc):
    for name in os.listdir(proc):
        if not name.isdigit():
            continue
        root = "/proc/" + name + "/root"
        for path in spec["paths"]:
            if os.path.lexists(root + path):
                leaks.append(path)

body = ""
for _ in range(5):
    try:
        with urllib.request.urlopen(os.environ["OMP_WORK_URL"], timeout=2) as resp:
            body = resp.read().decode()
        break
    except Exception as exc:
        body = "error:" + type(exc).__name__
        time.sleep(0.2)

wrote = ""
try:
    with open("/work/worker-wrote.txt", "w", encoding="utf-8") as handle:
        handle.write("written" + chr(10))
    with open("/work/worker-wrote.txt", encoding="utf-8") as handle:
        wrote = handle.read()
except OSError as exc:
    wrote = "error:" + type(exc).__name__

try:
    readme = open("/work/README", encoding="utf-8").read()
except OSError as exc:
    readme = "error:" + type(exc).__name__

ro_path = spec["ro"]
try:
    ro_text = open(ro_path, encoding="utf-8").read()
except OSError as exc:
    ro_text = "error:" + type(exc).__name__
try:
    open(ro_path, "a", encoding="utf-8").write("x")
    ro_write = "wrote"
except OSError:
    ro_write = "denied"

pid = str(spec["pid"])
print(json.dumps({
    "env": {name: os.environ.get(name) for name in allowed},
    "env_keys": sorted(os.environ),
    "readable": [path for path in spec["paths"] if os.path.lexists(path) or readable(path)],
    "leaks": leaks,
    "ws": body,
    "reachable": {str(port): reach(port) for port in spec["ports"]},
    "wrote": wrote,
    "readme": readme,
    "ro_text": ro_text,
    "ro_write": ro_write,
    "proc_environ": proc_read("/proc/" + pid + "/environ"),
    "proc_mem": proc_read("/proc/" + pid + "/mem"),
}))
"""


def _json_line(text: str) -> dict:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            return json.loads(stripped)
    raise AssertionError(f"worker printed no JSON: {text!r}")


def test_live_checkout_and_outside_worktree_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    outside = tmp_path / "outside"
    _git(repo, "worktree", "add", "--quiet", str(outside))

    with pytest.raises(WorkerSandboxRefused) as live:
        run_worker(
            ["/usr/bin/true"],
            worktree=repo,
            token=_TOKEN,
            workservice_socket=("127.0.0.1", 9),
            identity="worker",
            timeout=5,
        )
    assert live.value.code == "live_checkout"

    with pytest.raises(WorkerSandboxRefused) as escaped:
        run_worker(
            ["/usr/bin/true"],
            worktree=outside,
            token=_TOKEN,
            workservice_socket=("127.0.0.1", 9),
            identity="worker",
            timeout=5,
        )
    assert escaped.value.code == "worktree_not_allowed"

    with pytest.raises(WorkerSandboxRefused) as who:
        run_worker(
            ["/usr/bin/true"],
            worktree=outside,
            token=_TOKEN,
            workservice_socket=("127.0.0.1", 9),
            identity="owner",
            timeout=5,
        )
    assert who.value.code == "identity_not_allowed"

    monkeypatch.delenv("OMP_WORKTREES_DIR")
    with pytest.raises(WorkerSandboxRefused) as missing:
        run_worker(
            ["/usr/bin/true"],
            worktree=outside,
            token=_TOKEN,
            workservice_socket=("127.0.0.1", 9),
            identity="worker",
            timeout=5,
        )
    assert missing.value.code == "worktrees_dir_unconfigured"


def test_worker_sandbox_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    real_sock = os.environ.get("SSH_AUTH_SOCK")
    for name in _PARENT_SECRETS:
        monkeypatch.setenv(name, "planted-secret")

    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("readme\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    job = root / "job"
    sibling = root / "sibling"
    _git(repo, "worktree", "add", "--quiet", str(job))
    _git(repo, "worktree", "add", "--quiet", str(sibling))
    (sibling / "SECRET").write_text("sibling-secret\n", encoding="utf-8")
    (repo / "LIVE").write_text("live-secret\n", encoding="utf-8")
    gitdir_line = (job / ".git").read_text(encoding="utf-8")
    gitdir = Path(gitdir_line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (job / gitdir).resolve()

    plant = tmp_path / "plant-home"
    agent = _plant(plant)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(agent))
    key = tmp_path / "verifier.key"
    key.write_text("verifier-key\n", encoding="utf-8")

    postgres = OperationsConfig.defaults().port
    service = _WorkServiceStub()
    service.start()
    blocked = _bind_loopback()
    try:
        reserved = {service.port, int(blocked.getsockname()[1])}
        ports = [int(blocked.getsockname()[1]), 5432]
        if postgres not in reserved:
            ports.append(postgres)
        spec = {
            "paths": _probe_paths(plant, agent, sibling, repo, gitdir, real_sock),
            "allowed": list(_ALLOWED_ENV),
            "ports": ports,
            "ro": str(key),
            "pid": os.getpid(),
        }
        code = _PROBE.replace("SPEC", json.dumps(json.dumps(spec)))
        rc = run_worker(
            ["/usr/bin/python3", "-c", code],
            worktree=job,
            token=_TOKEN,
            workservice_socket=("127.0.0.1", service.port),
            identity="worker",
            ro_binds=(key,),
            timeout=45,
        )
    finally:
        service.close()
        blocked.close()

    out, err = capfd.readouterr()
    assert rc == 0, err
    payload = _json_line(out)
    assert payload["env_keys"] == sorted(_ALLOWED_ENV)
    assert payload["env"] == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TZ": "UTC",
        "OMP_TASK_TOKEN": _TOKEN,
        "OMP_WORK_URL": f"http://127.0.0.1:{service.port}",
    }
    assert payload["readable"] == []
    assert payload["leaks"] == []
    assert payload["proc_environ"] == "denied"
    assert payload["proc_mem"] == "denied"
    assert payload["ws"] == "ws-ok"
    assert payload["reachable"] == {str(port): False for port in ports}
    assert payload["readme"] == "readme\n"
    assert payload["wrote"] == "written\n"
    assert (job / "worker-wrote.txt").read_text(encoding="utf-8") == "written\n"
    assert payload["ro_text"] == "verifier-key\n"
    assert payload["ro_write"] == "denied"
