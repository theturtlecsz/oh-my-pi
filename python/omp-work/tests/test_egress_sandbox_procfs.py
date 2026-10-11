"""Denied proc mounts must hide host processes or prevent payload execution."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import pytest

from egress_sandbox_procfs_support import intercept_helper
from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import run_sandboxed


@pytest.fixture
def namespaces() -> None:
    for tool in ("unshare", "setpriv", "ip", "gcc"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} unavailable")
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
        capture_output=True,
        timeout=5,
    )
    if probe.returncode:
        pytest.skip("unshare probe failed")


def _interceptor(tmp_path: Path, proc_errno: int, fail_tmpfs: bool) -> tuple[Path, Path]:
    source = tmp_path / "next_mount.c"
    library = tmp_path / "next_mount.so"
    log = tmp_path / "mounts.log"
    source.write_text(
        r'''#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>

int mount(const char *source, const char *target, const char *type,
          unsigned long flags, const void *data) {
    int (*next_mount)(const char *, const char *, const char *, unsigned long, const void *)
        = dlsym(RTLD_NEXT, "mount");
    if (!type || strcmp(target, "/proc") != 0)
        return next_mount(source, target, type, flags, data);
    int result;
    if (strcmp(type, "proc") == 0) {
        errno = PROC_ERRNO;
        result = -1;
    } else if (strcmp(type, "tmpfs") == 0 && FAIL_TMPFS) {
        errno = ENOSPC;
        result = -1;
    } else {
        result = next_mount(source, target, type, flags, data);
    }
    int saved_errno = errno;
    FILE *log = fopen(LOG_PATH, "a");
    if (log) {
        fprintf(log, "%s %s %lu %s %d\n", source, type, flags,
                data ? (const char *)data : "-", result);
        fclose(log);
    }
    errno = saved_errno;
    return result;
}
'''.replace("PROC_ERRNO", str(proc_errno))
        .replace("FAIL_TMPFS", str(int(fail_tmpfs)))
        .replace("LOG_PATH", json.dumps(str(log)))
    )
    subprocess.run(["gcc", "-shared", "-fPIC", "-o", str(library), str(source), "-ldl"], check=True)
    return library, log


@pytest.mark.parametrize(
    ("proc_errno", "fail_tmpfs"),
    [(errno.EPERM, False), (errno.EACCES, False), (errno.EPERM, True), (errno.EIO, False)],
)
def test_proc_mount_failure_is_hidden_or_stops_payload(
    tmp_path: Path, monkeypatch, namespaces: None, proc_errno: int, fail_tmpfs: bool
) -> None:
    interceptor, log = _interceptor(tmp_path, proc_errno, fail_tmpfs)
    intercept_helper(monkeypatch, interceptor)
    marker = tmp_path / "payload.json"
    host_cmdline = Path(f"/proc/{os.getpid()}/cmdline")
    before_cmdline = host_cmdline.read_bytes()
    before_mounts = Path("/proc/self/mountinfo").read_bytes()
    code = f'''
import errno, json, os
from pathlib import Path
entries = os.listdir('/proc')
try:
    Path('/proc/should-not-exist').touch()
except OSError as exc:
    write_errno = exc.errno
else:
    write_errno = 0
Path({str(marker)!r}).write_text(json.dumps({{
    'entries': entries, 'mode': os.stat('/proc').st_mode & 0o777,
    'write_errno': write_errno, 'preload': os.environ.get('LD_PRELOAD'),
}}))
'''
    identity = Identity(
        workspace_id="ws-procfs", project_id="proj-procfs", mission_id="m-procfs",
        worker_id="w-procfs", stage="repository",
    )
    output = bytearray()
    rc = run_sandboxed(
        [sys.executable, "-c", code], identity, MemoryRecorder(), None, None, 10,
        sockets_root=tmp_path / "sockets", output=output,
    )
    assert host_cmdline.read_bytes() == before_cmdline
    assert Path("/proc/self/mountinfo").read_bytes() == before_mounts
    mounts = log.read_text().splitlines()
    assert any(row.startswith("proc proc ") and row.endswith(" -1") for row in mounts)
    tmpfs_mounts = [row for row in mounts if row.startswith("tmpfs tmpfs ")]
    if proc_errno == errno.EIO:
        assert rc != 0
        assert not marker.exists()
        assert tmpfs_mounts == []
    elif fail_tmpfs:
        assert rc != 0
        assert not marker.exists()
        assert tmpfs_mounts == ["tmpfs tmpfs 15 mode=0555 -1"]
    else:
        assert rc == 0, output.decode(errors="replace")
        assert tmpfs_mounts == ["tmpfs tmpfs 15 mode=0555 0"]
        assert json.loads(marker.read_text()) == {
            "entries": [], "mode": 0o555, "write_errno": errno.EROFS, "preload": None,
        }


@pytest.mark.parametrize("proc_errno", [errno.EPERM, errno.EACCES])
def test_shim_drops_caps_without_running_setpriv(
    tmp_path: Path, monkeypatch, namespaces: None, proc_errno: int
) -> None:
    """A masked /proc must never reach setpriv: the shim drops caps and execs the worker.

    The stand-in setpriv fails like a host whose setpriv needs cap_last_cap: with
    /proc masked it writes ``setpriv: activate capabilities: No such file or
    directory`` to stderr and exits 127 without touching its log. The payload can
    only succeed with an empty stand-in log when the shim performs the drop itself
    and execs the worker argv instead of setpriv.
    """
    interceptor, _log = _interceptor(tmp_path, proc_errno, False)
    standin_dir = tmp_path / "bin"
    standin_dir.mkdir()
    standin = standin_dir / "setpriv"
    standin_log = tmp_path / "setpriv.log"
    standin_log.touch()
    standin.write_text(
        "#!/bin/sh\n"
        "if ! cap_last_cap=$(cat /proc/sys/kernel/cap_last_cap 2>/dev/null); then\n"
        "    echo 'setpriv: activate capabilities: No such file or directory' >&2\n"
        "    exit 127\n"
        "fi\n"
        f"echo setpriv >> {json.dumps(str(standin_log))}\n"
        "shift 3\n"
        'exec "$@"\n',
        encoding="utf-8",
    )
    standin.chmod(0o755)
    monkeypatch.setenv("PATH", f"{standin_dir}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("setpriv", path=os.environ["PATH"]) == str(standin)
    intercept_helper(monkeypatch, interceptor)
    marker = tmp_path / "payload.json"
    sys_capget = {"x86_64": 125, "aarch64": 90}[platform.machine()]
    code = f'''
import ctypes, errno, json, os
from pathlib import Path
libc = ctypes.CDLL("libc.so.6", use_errno=True)
class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]
class CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
header = CapHeader(0x20080522, 0)
caps = (CapData * 2)()
assert libc.syscall(ctypes.c_long({sys_capget}), ctypes.byref(header), ctypes.byref(caps)) == 0
entries = os.listdir("/proc")
try:
    Path("/proc/should-not-exist").touch()
except OSError as exc:
    write_errno = exc.errno
else:
    write_errno = 0
Path({str(marker)!r}).write_text(json.dumps({{
    "entries": entries, "mode": os.stat("/proc").st_mode & 0o777,
    "write_errno": write_errno, "preload": os.environ.get("LD_PRELOAD"),
    "no_new_privs": libc.prctl(39, 0, 0, 0, 0),
    "bset_raised": [cap for cap in range(64) if libc.prctl(23, cap, 0, 0, 0) == 1],
    "cap_words": [field for word in caps
                  for field in (word.effective, word.permitted, word.inheritable)],
}}))
'''
    identity = Identity(
        workspace_id="ws-procfs", project_id="proj-procfs", mission_id="m-procfs",
        worker_id="w-procfs", stage="repository",
    )
    output = bytearray()
    rc = run_sandboxed(
        [sys.executable, "-c", code], identity, MemoryRecorder(), None, None, 10,
        sockets_root=tmp_path / "sockets", output=output,
    )
    assert rc == 0, output.decode(errors="replace")
    assert json.loads(marker.read_text()) == {
        "entries": [], "mode": 0o555, "write_errno": errno.EROFS, "preload": None,
        "no_new_privs": 1, "bset_raised": [], "cap_words": [0, 0, 0, 0, 0, 0],
    }
    assert standin_log.read_text(encoding="utf-8") == ""
