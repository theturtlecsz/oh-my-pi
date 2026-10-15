"""The helper removes its omp-helper-* directory on every exit (OMP-624)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from omp_work import egress_sandbox_helper
from omp_work.egress_sandbox_helper import HOSTS_TEXT, NSSWITCH_CONF_TEXT, RESOLV_CONF_TEXT


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


pytestmark = pytest.mark.skipif(_unshare_probe_fails(), reason="unshare probe failed")

def _env(temp_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TMPDIR"] = str(temp_root)
    src = str(Path(__file__).resolve().parents[1] / "src")
    previous = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + previous if previous else "")
    return env


def _write_config(temp_root: Path, argv: list[str], **extra: object) -> Path:
    config: dict[str, object] = {
        "record_sock": str(temp_root / "record.sock"),
        "setup_ok": str(temp_root / "setup_ok"),
        "argv": argv,
        "workdir": None,
        "env": None,
    }
    config.update(extra)
    path = temp_root / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _helper_cmd(config: Path) -> list[str]:
    return [
        "unshare",
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "--fork",
        "--kill-child",
        sys.executable,
        "-m",
        "omp_work.egress_sandbox_helper",
        str(config),
    ]


def _leftovers(temp_root: Path) -> list[Path]:
    return sorted(temp_root.glob("omp-helper-*"))


def _children(pid: int) -> list[int]:
    try:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(part) for part in text.split()]


def _cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode(errors="replace")


def _kill_pid_tree(pid: int) -> None:
    for child in _children(pid):
        _kill_pid_tree(child)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _worker_check() -> str:
    """Check the generated resolver files, then stay up long enough to be observed."""
    return (
        "from pathlib import Path\n"
        "import time\n"
        f"assert Path('/etc/hosts').read_text(encoding='utf-8') == {HOSTS_TEXT!r}\n"
        f"assert Path('/etc/resolv.conf').read_text(encoding='utf-8') == {RESOLV_CONF_TEXT!r}\n"
        f"assert Path('/etc/nsswitch.conf').read_text(encoding='utf-8') == {NSSWITCH_CONF_TEXT!r}\n"
        "time.sleep(1)\n"
    )


def _start(temp_root: Path, config: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _helper_cmd(config),
        env=_env(temp_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _finish(proc: subprocess.Popen[str], timeout: float) -> tuple[int, str, str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_pid_tree(proc.pid)
        stdout, stderr = proc.communicate(timeout=5)
        raise AssertionError(f"helper timed out\nstdout:\n{stdout}\nstderr:\n{stderr}") from None
    assert proc.returncode is not None
    return proc.returncode, stdout, stderr


def test_helper_removes_temp_dir_on_success(tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    config = _write_config(temp_root, [sys.executable, "-c", _worker_check()])
    proc = _start(temp_root, config)
    try:
        seen: list[Path] = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            seen = _leftovers(temp_root)
            if seen or proc.poll() is not None:
                break
            time.sleep(0.05)
        returncode, _stdout, stderr = _finish(proc, 30)
    finally:
        if proc.poll() is None:
            _kill_pid_tree(proc.pid)
            proc.wait(timeout=5)

    assert returncode == 0, stderr
    assert (temp_root / "setup_ok").exists()
    assert seen, "helper did not create omp-helper-* under TMPDIR"
    assert all(path.parent == temp_root for path in seen)
    assert _leftovers(temp_root) == []


def test_helper_removes_temp_dir_on_setup_failure(tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    sockets = temp_root / "sockets"
    sockets.mkdir()
    missing = temp_root / "missing-sandbox-dir"
    config = _write_config(
        temp_root,
        ["true"],
        sockets_root=str(sockets),
        sandbox_dir=str(missing),
    )
    proc = _start(temp_root, config)
    try:
        returncode, _stdout, stderr = _finish(proc, 30)
    finally:
        if proc.poll() is None:
            _kill_pid_tree(proc.pid)
            proc.wait(timeout=5)

    assert returncode != 0
    assert "_hide_other_sandboxes" in stderr
    assert str(missing) in stderr
    assert not (temp_root / "setup_ok").exists()
    assert _leftovers(temp_root) == []


def test_helper_removes_temp_dir_on_sigterm(tmp_path: Path) -> None:
    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    sleep = "/bin/sleep" if Path("/bin/sleep").exists() else "sleep"
    config = _write_config(temp_root, [sleep, "60"])
    proc = _start(temp_root, config)
    try:
        helper_pid = _wait_for_helper(proc.pid, temp_root)
        os.kill(helper_pid, signal.SIGTERM)
        returncode, _stdout, stderr = _finish(proc, 20)
    finally:
        if proc.poll() is None:
            _kill_pid_tree(proc.pid)
            proc.wait(timeout=5)

    assert returncode == 128 + signal.SIGTERM, stderr
    assert (temp_root / "setup_ok").exists()
    assert _leftovers(temp_root) == []


def _wait_for_helper(unshare_pid: int, temp_root: Path) -> int:
    """Helper pid once its worker child and omp-helper-* directory both exist."""
    deadline = time.monotonic() + 20
    helper_pid = 0
    while time.monotonic() < deadline:
        if helper_pid == 0:
            for pid in _children(unshare_pid):
                if "egress_sandbox_helper" in _cmdline(pid):
                    helper_pid = pid
                    break
        if helper_pid and _children(helper_pid) and _leftovers(temp_root):
            # Past Popen's return: the helper is blocked in wait, handler installed.
            time.sleep(0.5)
            if Path(f"/proc/{helper_pid}").exists() and _leftovers(temp_root):
                return helper_pid
        if not Path(f"/proc/{unshare_pid}").exists():
            break
        time.sleep(0.05)
    raise AssertionError("helper did not reach the running worker")


def test_mask_sockets_skips_resolv_conf_bind_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """systemd-resolved: /etc/resolv.conf -> resolve/stub-resolv.conf stays the bound file."""
    resolve = tmp_path / "resolve"
    resolve.mkdir()
    stub = resolve / "stub-resolv.conf"
    stub.write_text("nameserver 127.0.0.53\n", encoding="utf-8")
    sock_path = resolve / "io.systemd.Resolve"
    srv = socket.socket(socket.AF_UNIX)
    monkeypatch.chdir(resolve)  # relative bind: tmp_path can exceed the AF_UNIX path limit
    srv.bind(sock_path.name)
    etc_link = tmp_path / "resolv.conf"
    etc_link.symlink_to(stub)

    real_glob = egress_sandbox_helper.glob.glob
    monkeypatch.setattr(
        egress_sandbox_helper.glob,
        "glob",
        lambda pat: [str(p) for p in resolve.iterdir()]
        if pat == "/run/systemd/resolve/*"
        else real_glob(pat),
    )
    masked: list[str] = []
    monkeypatch.setattr(egress_sandbox_helper, "_executable", lambda name: name)
    monkeypatch.setattr(
        egress_sandbox_helper.subprocess,
        "run",
        lambda argv, **_kw: masked.append(argv[-1]),
    )
    try:
        egress_sandbox_helper._mask_sockets([etc_link.resolve()])
    finally:
        srv.close()

    assert str(sock_path) in masked
    assert str(stub) not in masked
