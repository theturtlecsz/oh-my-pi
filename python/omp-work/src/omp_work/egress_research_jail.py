"""Research-stage filesystem jail for the egress sandbox (OMP-431-s08-s02).

Started by the helper as ``unshare --mount --pid --fork python -m
omp_work.egress_research_jail <config>``. The CA certificate is read before any
mount. ``/`` is made rprivate, a tmpfs becomes the new root, and ``pivot_root``
switches to it. setup_ok is created through the old root, that old root is
detached, and the worker is exec'd with the s07 setpriv flags. chroot is not
used. Every import is at module load, before the pivot.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import sys

from omp_work.egress_sandbox_helper import (
    HOSTS_TEXT,
    JAIL_CA_CERT,
    NSSWITCH_CONF_TEXT,
    RESEARCH_HOME,
    RESOLV_CONF_TEXT,
    _executable,
    _mount,
    _worker_env,
)

_MS_RDONLY = 1
_MS_BIND = 0x1000
_MS_REC = 0x4000
_MS_REMOUNT = 32
_MS_PRIVATE = 1 << 18
_MNT_DETACH = 2

_HOST_TREES = ("bin", "lib", "lib64", "sbin")
_DEV_NODES = ("null", "zero", "random", "urandom")

_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.pivot_root.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_libc.pivot_root.restype = ctypes.c_int
_libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
_libc.umount2.restype = ctypes.c_int


def _syscall_ok(result: int, label: str) -> None:
    if result != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), label)


def _ca_bytes(config: dict) -> bytes:
    """Read the CA now. A missing or empty file exits before any cert is written."""
    path = config.get("ca_cert_path")
    if not isinstance(path, str) or path == "":
        sys.stderr.write("research jail: ca_cert_path is missing\n")
        sys.exit(1)
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        sys.stderr.write(f"research jail: cannot read ca cert: {exc}\n")
        sys.exit(1)
    if not data:
        sys.stderr.write("research jail: ca cert is empty\n")
        sys.exit(1)
    return data


def _rbind_readonly(source: str, target: Path) -> None:
    target.mkdir(parents=True)
    _mount(source, str(target), None, _MS_BIND | _MS_REC)
    _mount("none", str(target), None, _MS_BIND | _MS_REMOUNT | _MS_RDONLY)


def _place_host_tree(new: Path, name: str) -> None:
    """Recreate a host top-level path: the same symlink, or a read-only rbind."""
    host = "/" + name
    dest = new / name
    if os.path.islink(host):
        os.symlink(os.readlink(host), dest)
    elif os.path.isdir(host):
        _rbind_readonly(host, dest)


def _write_etc(new: Path, ca: bytes) -> None:
    etc = new / "etc"
    etc.mkdir(mode=0o755)
    for name in ("passwd", "group"):
        (etc / name).write_bytes(Path("/etc", name).read_bytes())
    (etc / "resolv.conf").write_text(RESOLV_CONF_TEXT, encoding="utf-8")
    (etc / "nsswitch.conf").write_text(NSSWITCH_CONF_TEXT, encoding="utf-8")
    (etc / "hosts").write_text(HOSTS_TEXT, encoding="utf-8")
    cert = new / JAIL_CA_CERT.lstrip("/")
    cert.parent.mkdir(parents=True)
    cert.write_bytes(ca)
    os.chmod(cert, 0o644)


def _bind_devices(new: Path) -> None:
    dev = new / "dev"
    dev.mkdir(mode=0o755)
    for name in _DEV_NODES:
        dest = dev / name
        dest.touch()
        _mount("/dev/" + name, str(dest), None, _MS_BIND)


def _mount_proc(new: Path) -> None:
    """A fresh procfs, or an empty directory when the kernel refuses the mount.

    The host ``/proc`` is never bind-mounted: that would expose host process
    roots after the pivot.
    """
    proc = new / "proc"
    proc.mkdir(mode=0o555)
    try:
        _mount("proc", str(proc), "proc", 0)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EACCES):
            raise


def _build_root(config_path: Path, ca: bytes) -> Path:
    _mount("none", "/", None, _MS_REC | _MS_PRIVATE)
    new = config_path.parent / "research-root"
    new.mkdir(mode=0o755)
    _mount("tmpfs", str(new), "tmpfs", 0)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(new, 0o755)  # nosec B103 - the unprivileged worker must traverse the jail root
    _rbind_readonly("/usr", new / "usr")
    for name in _HOST_TREES:
        _place_host_tree(new, name)
    _write_etc(new, ca)
    _bind_devices(new)
    _mount_proc(new)
    tmp = new / "tmp"
    tmp.mkdir()
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(tmp, 0o1777)  # nosec B103 - sticky world-writable /tmp is the POSIX contract inside the jail
    home = new / "home" / "research"
    home.mkdir(parents=True)
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(new / "home", 0o755)  # nosec B103 - the unprivileged worker must traverse /home to its HOME
    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    os.chmod(home, 0o755)  # nosec B103 - the unprivileged worker's own HOME directory
    return new


def _mark_setup_ok(setup_ok: object) -> None:
    if not isinstance(setup_ok, str) or not setup_ok.startswith("/"):
        sys.exit(1)
    # setup_ok is absolute on the host. Join would drop "/.old" because the
    # second argument is absolute, so concatenate.
    fd = os.open("/.old" + setup_ok, os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)


def _pivot_and_exec(
    new: Path,
    setup_ok: object,
    setpriv: str,
    argv: list[str],
    env: dict[str, str],
) -> None:
    old = new / ".old"
    old.mkdir(mode=0o755)
    os.chdir(new)
    _syscall_ok(_libc.pivot_root(str(new).encode(), str(old).encode()), "pivot_root")
    os.chdir(RESEARCH_HOME)
    _mark_setup_ok(setup_ok)
    _syscall_ok(_libc.umount2(b"/.old", _MNT_DETACH), "umount2")
    os.rmdir("/.old")
    try:
        # nosemgrep: python.lang.security.audit.dangerous-os-exec-tainted-env-args.dangerous-os-exec-tainted-env-args
        os.execve(  # nosec B606 - absolute setpriv, fixed argv list, no shell, no user-controlled executable
            setpriv,
            [setpriv, "--bounding-set=-all", "--inh-caps=-all", "--no-new-privs", *argv],
            env,
        )
    except OSError as exc:
        sys.stderr.write(f"research jail: execve failed: {exc}\n")
        sys.exit(127)


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("Usage: python3 -m omp_work.egress_research_jail <config.json>\n")
        sys.exit(2)
    config_path = Path(sys.argv[1])
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"research jail: cannot read config: {exc}\n")
        sys.exit(1)
    ca = _ca_bytes(config)
    env = _worker_env(config, config.get("workdir"))
    setpriv = _executable("setpriv")
    argv = [str(item) for item in config["argv"]]
    new = _build_root(config_path, ca)
    _pivot_and_exec(new, config.get("setup_ok"), setpriv, argv, env)


if __name__ == "__main__":
    main()
