"""Worker model relay exposes one dedicated service, never host networking."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import run_sandboxed
from omp_work.orchestrator.worker_sandbox import WorkerSandboxRefused, run_worker
from test_orchestrator_worker_sandbox import (
    _ALLOWED_ENV,
    _PARENT_SECRETS,
    _PLANTED,
    _bind_loopback,
    _git,
    _json_line,
    _plant,
    _unshare_probe_fails,
)


@pytest.fixture
def job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "worktrees"
    root.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README").write_text("worker repo\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")
    tree = root / "job"
    _git(repo, "worktree", "add", "--quiet", str(tree))
    monkeypatch.setenv("OMP_WORKTREES_DIR", str(root))
    return tree


class _Service:
    """Record exact endpoint traffic; optionally keep streams open until teardown."""

    def __init__(
        self, body: bytes, *, hold: bool = False, host: str = "127.0.0.1"
    ) -> None:
        if host == "::1":
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            self.sock.bind((host, 0))
            self.sock.listen(8)
            self.sock.settimeout(0.2)
        else:
            self.sock = _bind_loopback()
        self.port = self.sock.getsockname()[1]
        self.body = body
        self.hold = hold
        self.received = b""
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(0.1)
                self.connected.set()
                while not self.stop.is_set():
                    try:
                        chunk = conn.recv(4096)
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    self.received += chunk
                    if not self.hold and b"\r\n\r\n" in self.received:
                        conn.sendall(
                            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
                            + str(len(self.body)).encode()
                            + b"\r\n\r\n"
                            + self.body
                        )
                        break
                self.closed.set()

    def close(self) -> None:
        self.stop.set()
        self.sock.close()
        self.thread.join(timeout=2)


_MODEL_PROBE = r"""
import json, os, socket, urllib.request
spec = json.load(open(os.environ["OMP_WORKER_MANIFEST"]))
out = {"env": dict(os.environ)}
for name in ("OMP_WORK_URL", "OMP_MODEL_PROXY_URL"):
    request = urllib.request.Request(os.environ[name], headers={"Authorization": "Bearer " + os.environ["OMP_MODEL_TOKEN"]})
    with urllib.request.urlopen(request, timeout=3) as response:
        out[name] = response.read().decode()
out["blocked"] = []
for host, port in spec["blocked"]:
    try:
        conn = socket.create_connection((host, port), timeout=0.2)
        conn.close()
        out["blocked"].append(False)
    except OSError:
        out["blocked"].append(True)
try:
    socket.getaddrinfo("api.openai.com", 443)
    out["dns"] = "resolved"
except OSError:
    out["dns"] = "denied"
paths = spec["paths"]
out["visible"] = [p for p in paths if os.path.lexists(p)]
out["proc_leaks"] = []
for name in os.listdir("/proc"):
    if name.isdigit():
        out["proc_leaks"] += [p for p in paths if os.path.lexists("/proc/" + name + "/root" + p)]
out["host_proc"] = []
for suffix in ("environ", "mem"):
    try:
        with open("/proc/" + str(spec["pid"]) + "/" + suffix, "rb") as handle:
            handle.read(1)
        out["host_proc"].append("read")
    except OSError:
        out["host_proc"].append("denied")
out["readonly"] = []
for path in (os.environ["OMP_WORKER_MANIFEST"], __file__):
    try:
        with open(path, "a") as handle:
            handle.write("corruption")
        out["readonly"].append(False)
    except OSError:
        out["readonly"].append(True)
print(json.dumps(out))
"""


@pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")
@pytest.mark.parametrize("model_host", ["127.0.0.1", "::1"])
def test_model_and_workservice_only_inside_real_jail(
    job: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
    model_host: str,
) -> None:
    model = _Service(b"model-ok", host=model_host)
    work = _Service(b"work-ok")
    provider = _Service(b"provider-secret")
    other = _Service(b"host-secret")
    plant = tmp_path / "provider-home"
    _plant(plant)
    for name in _PARENT_SECRETS:
        monkeypatch.setenv(name, "provider-key-never-in-worker")
    monkeypatch.setenv("HOME", str(plant))
    sibling = job.parent / "sibling"
    _git(tmp_path / "repo", "worktree", "add", "--quiet", str(sibling))
    (sibling / "SECRET").write_text("sibling-secret", encoding="utf-8")
    executable = tmp_path / "worker-executable"
    executable.write_text("#!/usr/bin/python3\n" + _MODEL_PROBE, encoding="utf-8")
    executable.chmod(0o755)
    manifest = tmp_path / "worker-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "blocked": [
                    ("127.0.0.1", provider.port),
                    ("127.0.0.1", other.port),
                    ("127.0.0.1", 5432),
                    ("10.0.0.1", 5432),
                    ("93.184.216.34", 443),
                ],
                "paths": [str(plant / p) for p in _PLANTED]
                + [str(sibling), str(sibling / "SECRET")],
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )
    original = manifest.read_bytes(), executable.read_bytes()
    try:
        rc = run_worker(
            [str(executable)],
            worktree=job,
            token="task-token",
            workservice_socket=("127.0.0.1", work.port),
            identity="worker",
            model_proxy=(model_host, model.port),
            model_token="model-token",
            manifest_path=manifest,
            ro_binds=(executable,),
            timeout=15,
        )
        out, err = capfd.readouterr()
        assert rc == 0, err
        result = _json_line(out)
        assert set(result["env"]) == {
            *_ALLOWED_ENV,
            "OMP_MODEL_PROXY_URL",
            "OMP_MODEL_TOKEN",
            "OMP_WORKER_MANIFEST",
        }
        assert result["OMP_MODEL_PROXY_URL"] == "model-ok"
        assert result["OMP_WORK_URL"] == "work-ok"
        assert b"Authorization: Bearer model-token" in model.received
        assert result["blocked"] == [True] * 5
        assert result["dns"] == "denied"
        assert result["visible"] == result["proc_leaks"] == []
        assert result["host_proc"] == ["denied", "denied"]
        assert result["readonly"] == [True, True]
        assert (manifest.read_bytes(), executable.read_bytes()) == original
        assert not provider.connected.is_set()
        assert not other.connected.is_set()
    finally:
        for service in (model, work, provider, other):
            service.close()


@pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")
def test_timeout_closes_both_active_relays(job: Path, tmp_path: Path) -> None:
    model = _Service(b"", hold=True)
    work = _Service(b"", hold=True)
    sockets = tmp_path / "sockets"
    try:
        code = (
            "import socket,time; "
            f"m=socket.create_connection(('127.0.0.1',{model.port})); m.sendall(b'model'); "
            f"w=socket.create_connection(('127.0.0.1',{work.port})); w.sendall(b'work'); "
            "time.sleep(30)"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            run_sandboxed(
                ["/usr/bin/python3", "-c", code],
                Identity("worker", "worker", "worker", "worker", "repository"),
                MemoryRecorder(),
                None,
                {"PATH": "/usr/bin:/bin"},
                3,
                sockets_root=sockets,
                root="worker",
                worktree=job,
                workservice=("127.0.0.1", work.port),
                model_proxy=("127.0.0.1", model.port),
            )
        for service, expected in ((model, b"model"), (work, b"work")):
            assert service.connected.is_set()
            assert service.received == expected
            assert service.closed.wait(2), "upstream stream survived timeout"
        assert list(sockets.iterdir()) == []
    finally:
        model.close()
        work.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        ("0.0.0.0", 8000),
        ("8.8.8.8", 8000),
        ("api.openai.com", 443),
        ("127.0.0.1", 0),
        ("127.0.0.1", 65536),
        ("127.0.0.1", True),
        ("127.0.0.1", 9000),
        ("127.0.0.2", 9000),
    ],
)
def test_invalid_model_endpoint_refused_before_launch(
    job: Path,
    endpoint: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launched(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid endpoint launched a sandbox")

    monkeypatch.setattr("omp_work.orchestrator.worker_sandbox.run_sandboxed", launched)
    with pytest.raises(WorkerSandboxRefused, match="model_proxy_not_allowed"):
        run_worker(
            ["/usr/bin/true"],
            worktree=job,
            token="t",
            identity="worker",
            workservice_socket=("127.0.0.1", 9000),
            model_proxy=endpoint,
            timeout=5,
        )


@pytest.mark.parametrize("field", ["model_proxy", "model_token", "manifest_path"])
def test_verifier_refuses_each_worker_only_input(job: Path, field: str) -> None:
    inputs = {
        "model_proxy": ("127.0.0.1", 9001),
        "model_token": "model-token",
        "manifest_path": job / "README",
    }
    with pytest.raises(WorkerSandboxRefused, match="identity_not_allowed"):
        run_worker(
            ["/usr/bin/true"],
            worktree=job,
            token="t",
            identity="verifier",
            workservice_socket=("127.0.0.1", 9000),
            timeout=5,
            **{field: inputs[field]},
        )


@pytest.mark.parametrize("manifest", ["relative.json", "missing", "directory"])
def test_manifest_must_be_existing_absolute_file(
    job: Path, tmp_path: Path, manifest: str
) -> None:
    path = manifest if manifest == "relative.json" else tmp_path / manifest
    if manifest == "directory":
        path.mkdir()
    with pytest.raises(WorkerSandboxRefused, match="ro_bind_not_allowed"):
        run_worker(
            ["/usr/bin/true"],
            worktree=job,
            token="t",
            identity="worker",
            workservice_socket=("127.0.0.1", 9000),
            manifest_path=path,
            timeout=5,
        )


def test_model_token_requires_dedicated_proxy(job: Path) -> None:
    with pytest.raises(WorkerSandboxRefused, match="model_proxy_not_allowed"):
        run_worker(
            ["/usr/bin/true"],
            worktree=job,
            token="t",
            identity="worker",
            workservice_socket=("127.0.0.1", 9000),
            model_token="model-token",
            timeout=5,
        )


def test_shared_launcher_refuses_gateway_with_model_before_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launched(*args: object, **kwargs: object) -> None:
        pytest.fail("gateway plus model proxy started a process")

    monkeypatch.setattr(subprocess, "run", launched)
    with pytest.raises(ValueError, match="separate from WorkService and gateway"):
        run_sandboxed(
            ["/usr/bin/true"],
            Identity("w", "p", "m", "worker", "repository"),
            MemoryRecorder(),
            None,
            {},
            5,
            gateway=object(),
            model_proxy=("127.0.0.1", 9001),
        )
