"""OMP-315 R05: the local jail runner and its cancellation."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from omp_work.jobs.runner import (
    Backend,
    BackendIncompatible,
    LocalJailBackend,
    RunRequest,
    check_environment,
    resolve_backend,
)
from omp_work.v1.canonical import sha256


def _unshare_probe_fails() -> bool:
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=5,
        )
        return probe.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        return True


_needs_jail = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")

_HEARTBEAT = """
import os
import time
from pathlib import Path

os.setsid()
heartbeat = Path("/work/heartbeat")
while True:
    with heartbeat.open("a", encoding="utf-8") as handle:
        handle.write("x")
        handle.flush()
        os.fsync(handle.fileno())
    time.sleep(0.05)
"""

_PROBE = """
import json
import os
import pathlib
import socket
import time

spec = json.loads(SPEC)

def can_write(path):
    try:
        pathlib.Path(path).write_text("nope", encoding="utf-8")
    except OSError:
        return False
    return True

def can_read(path):
    try:
        pathlib.Path(path).read_bytes()
    except OSError:
        return False
    return True

report = {}
kept = pathlib.Path("/work/kept.txt")
kept.write_text("kept-from-jail", encoding="utf-8")
report["kept"] = kept.read_text(encoding="utf-8")
report["tmp_write"] = can_write(spec["tmp_dir"] + "/from-jail.txt")
report["usr_write"] = can_write("/usr/from-jail.txt")
report["ssh"] = can_read(spec["ssh"])
report["home_ssh"] = can_read(os.path.expanduser("~/.ssh/id_rsa"))
report["tmp_file"] = can_read(spec["secret"])
report["ro_read"] = can_read(spec["ro"])
report["ro_write"] = can_write(spec["ro"])
unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
unix.settimeout(2)
try:
    unix.connect(spec["sock"])
    report["unix"] = True
except OSError:
    report["unix"] = False
finally:
    unix.close()
started = time.monotonic()
tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
tcp.settimeout(2)
try:
    tcp.connect((spec["host"], spec["port"]))
    report["tcp"] = True
except OSError:
    report["tcp"] = False
finally:
    tcp.close()
report["tcp_elapsed"] = time.monotonic() - started
report["env_url"] = os.environ.get("OMP_WORK_URL")
report["env_key"] = os.environ.get("ANTHROPIC_API_KEY")
pathlib.Path("/work/report.json").write_text(json.dumps(report), encoding="utf-8")
print("probe-ok")
"""


def _forbid_popen(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    started: list[object] = []

    def boom(*args: object, **kwargs: object) -> object:
        started.append(args)
        raise AssertionError("process started")

    monkeypatch.setattr("omp_work.egress_sandbox.subprocess.Popen", boom)
    monkeypatch.setattr("omp_work.egress_sandbox_helper.subprocess.Popen", boom)
    return started


def _descendants(pid: int) -> list[int]:
    try:
        out = subprocess.check_output(["ps", "-o", "pid=", "--ppid", str(pid)], text=True)
    except (OSError, subprocess.CalledProcessError):
        return []
    found: list[int] = []
    for line in out.split():
        if not line.isdigit():
            continue
        child = int(line)
        found.append(child)
        found.extend(_descendants(child))
    return found


def _kill_recorded(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def test_env_not_allowed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="^env_not_allowed$"):
        RunRequest(["/bin/true"], tmp_path, env={"ANTHROPIC_API_KEY": "x"})
    with pytest.raises(ValueError, match="^env_not_allowed$"):
        RunRequest(["/bin/true"], tmp_path, env={"OMP_WORK_URL": "http://127.0.0.1:9"})
    request = RunRequest(
        ["/bin/true"],
        tmp_path,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "TERM": "dumb", "TZ": "UTC"},
    )
    assert request.env == {"PATH": "/usr/bin:/bin", "LANG": "C", "TERM": "dumb", "TZ": "UTC"}
    assert request.capabilities == frozenset({"cpu"})


def test_incompatible_starts_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _forbid_popen(monkeypatch)
    backend = LocalJailBackend()
    assert isinstance(backend, Backend)
    assert backend.name == "local-jail.v1"
    assert backend.capabilities == frozenset({"cpu"})
    for capabilities in ({"gpu"}, {"instrument"}, {"cpu", "gpu"}):
        request = RunRequest(["/bin/true"], tmp_path, timeout=5, capabilities=capabilities)
        with pytest.raises(BackendIncompatible) as exc:
            backend.run(request)
        assert exc.value.code == "backend_incompatible"
    request = RunRequest(["/bin/true"], tmp_path, timeout=5, requires=["no-such-tool"])
    with pytest.raises(BackendIncompatible) as exc:
        backend.run(request)
    assert exc.value.code == "missing_dependency:no-such-tool"
    with pytest.raises(BackendIncompatible) as exc:
        resolve_backend("remote")
    assert exc.value.code == "backend_incompatible"
    assert resolve_backend("local-jail.v1").name == "local-jail.v1"
    assert started == []


def test_requires_outside_bin_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _forbid_popen(monkeypatch)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "custom-tool"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    request = RunRequest(["/bin/true"], tmp_path, timeout=5, requires=["custom-tool"])
    with pytest.raises(BackendIncompatible) as exc:
        LocalJailBackend().run(request)
    assert exc.value.code == "missing_dependency:custom-tool"
    assert started == []


def test_check_environment_names_differences() -> None:
    uname = os.uname()
    machine = {
        "backend": "local-jail.v1",
        "kernel": uname.release,
        "machine": "other-machine",
        "executables": {},
    }
    assert check_environment(machine) == ["machine"]
    executable = {
        "backend": "local-jail.v1",
        "kernel": uname.release,
        "machine": uname.machine,
        "executables": {"true": "0" * 64},
    }
    assert check_environment(executable) == ["executables:true"]


@_needs_jail
def test_jail_keeps_work_and_hides_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMP_WORK_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("host-secret", encoding="utf-8")
    # The read-only bind materializes its parent directories inside the jail.
    # Keep that chain off tmp_path so a write to tmp_path has nowhere to land.
    ro_dir = Path(tempfile.mkdtemp(prefix="omp-ro-"))
    ro_file = ro_dir / "ro.txt"
    ro_file.write_text("read-only", encoding="utf-8")
    sock_path = tmp_path / "host.sock"
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix.bind(str(sock_path))
    unix.listen(1)
    host = _host_ip()
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp.bind((host, 0))
    tcp.listen(1)
    port = int(tcp.getsockname()[1])
    spec = {
        "tmp_dir": str(tmp_path),
        "secret": str(secret),
        "sock": str(sock_path),
        "ro": str(ro_file),
        "host": host,
        "port": port,
        "ssh": str(Path.home() / ".ssh" / "id_rsa"),
    }
    script = work / "probe.py"
    script.write_text(_PROBE.replace("SPEC", json.dumps(json.dumps(spec))), encoding="utf-8")
    try:
        result = LocalJailBackend().run(
            RunRequest(
                ["/usr/bin/python3", "/work/probe.py"],
                work,
                ro_files=(ro_file,),
                timeout=30,
            )
        )
        assert result.status == "completed", (result.status, result.exit_code, result.output)
        assert b"probe-ok" in result.output
        report = json.loads((work / "report.json").read_text(encoding="utf-8"))
        assert (work / "kept.txt").read_text(encoding="utf-8") == "kept-from-jail"
        assert report["kept"] == "kept-from-jail"
        assert report["tmp_write"] is False
        assert report["usr_write"] is False
        assert report["ssh"] is False
        assert report["home_ssh"] is False
        assert report["tmp_file"] is False
        assert report["unix"] is False
        assert report["tcp"] is False
        assert report["tcp_elapsed"] < 3
        assert report["ro_read"] is True
        assert report["ro_write"] is False
        assert report["env_url"] is None
        assert report["env_key"] is None
        assert ro_file.read_text(encoding="utf-8") == "read-only"
        assert not (tmp_path / "from-jail.txt").exists()
    finally:
        unix.close()
        tcp.close()
        ro_file.unlink(missing_ok=True)
        ro_dir.rmdir()


def _host_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def _heartbeat_request(work: Path, timeout: float) -> RunRequest:
    script = work / "beat.py"
    if not script.exists():
        script.write_text(_HEARTBEAT, encoding="utf-8")
    return RunRequest(["/usr/bin/python3", "/work/beat.py"], work, timeout=timeout)


@_needs_jail
def test_cancel_stops_setsid_heartbeat(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    heartbeat = work / "heartbeat"
    cancel = threading.Event()
    armed: dict[str, float] = {}

    def fire() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not heartbeat.exists():
            time.sleep(0.02)
        time.sleep(0.5)
        armed["at"] = time.monotonic()
        cancel.set()

    threading.Thread(target=fire, daemon=True).start()
    result = LocalJailBackend().run(_heartbeat_request(work, 30), cancel)
    assert result.status == "canceled", (result.status, result.exit_code, result.output)
    assert result.exit_code == -9
    assert "at" in armed
    assert time.monotonic() - armed["at"] < 5
    assert heartbeat.exists()
    size = heartbeat.stat().st_size
    time.sleep(1)
    assert heartbeat.stat().st_size == size
    assert size > 0


@_needs_jail
def test_timeout_stops_setsid_heartbeat(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = LocalJailBackend().run(_heartbeat_request(work, 8))
    assert result.status == "timed_out", (result.status, result.exit_code, result.output)
    heartbeat = work / "heartbeat"
    assert heartbeat.exists()
    size = heartbeat.stat().st_size
    time.sleep(1)
    assert heartbeat.stat().st_size == size
    assert size > 0


@_needs_jail
def test_sigkill_parent_stops_heartbeat(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "beat.py").write_text(_HEARTBEAT, encoding="utf-8")
    runner = tmp_path / "runner.py"
    runner.write_text(
        "from omp_work.jobs.runner import LocalJailBackend, RunRequest\n"
        "LocalJailBackend().run(RunRequest("
        f"['/usr/bin/python3', '/work/beat.py'], {str(work)!r}, timeout=60))\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(runner)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    heartbeat = work / "heartbeat"
    leftovers: list[int] = []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not heartbeat.exists():
            if proc.poll() is not None:
                err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise AssertionError(err)
            time.sleep(0.05)
        assert heartbeat.exists()
        time.sleep(0.2)
        leftovers = _descendants(proc.pid)
        os.kill(proc.pid, signal.SIGKILL)
        killed = time.monotonic()
        proc.wait(timeout=5)
        last = heartbeat.stat().st_size
        last_change = killed
        while time.monotonic() - killed < 2:
            time.sleep(0.05)
            size = heartbeat.stat().st_size
            if size != last:
                last = size
                last_change = time.monotonic()
        assert last_change - killed < 2
        time.sleep(0.4)
        assert heartbeat.stat().st_size == last
    finally:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        _kill_recorded(leftovers)


@_needs_jail
def test_manifest_is_stable_and_machine_differs(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    request = RunRequest(["/bin/true"], work, timeout=30, requires=["true"])
    backend = LocalJailBackend()
    first = backend.run(request)
    second = backend.run(request)
    assert first.status == "completed", (first.status, first.exit_code, first.output)
    assert second.status == "completed"
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.manifest_sha256 == sha256(first.manifest)
    assert first.manifest["backend"] == "local-jail.v1"
    assert first.manifest["machine"] == os.uname().machine
    assert "true" in first.manifest["executables"]
    other = dict(first.manifest)
    other["machine"] = "other-machine"
    other["executables"] = dict(first.manifest["executables"])  # type: ignore[arg-type]
    assert check_environment(other) == ["machine"]
    crashed = backend.run(RunRequest(["/bin/false"], work, timeout=30))
    assert crashed.status == "crashed"
    assert crashed.exit_code != 0


@_needs_jail
def test_output_is_capped_at_one_mibibyte(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    script = work / "big.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(b'x' * (2 * 1024 * 1024))\n",
        encoding="utf-8",
    )
    result = LocalJailBackend().run(
        RunRequest(["/usr/bin/python3", "/work/big.py"], work, timeout=30)
    )
    assert result.status == "completed", (result.status, result.exit_code, result.output[:200])
    assert len(result.output) == 1 << 20
