"""Internal helper for egress_sandbox: sets up namespace isolation and tap (OMP-431)."""

from __future__ import annotations

import glob
import json
import os
from contextlib import suppress
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess  # nosec B404 - argv lists only, no shell
import sys
import tempfile
import threading
import time


def _executable(name: str, path: str | None = None) -> str:
    """Absolute path of a fixed tool, or the bare name when it is not on PATH."""
    found = shutil.which(name, path=path)
    return found if found is not None else name


class PacketTap:
    """AF_PACKET tap on egress0 sending JSON datagrams for TCP SYN and UDP flows."""

    def __init__(self, record_sock_path: str) -> None:
        self.record_sock_path = record_sock_path
        self.seen_flows: set[tuple[str, str, int, str, int]] = set()
        self.stop_event = threading.Event()
        self.tap_sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
        self.tap_sock.bind(("egress0", 0))
        self.tap_sock.settimeout(0.05)
        self.dgram_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _send_record(self, proto: str, ip: str, port: int) -> None:
        with suppress(Exception):
            payload = json.dumps({"protocol": proto, "ip": ip, "port": port}).encode("utf-8")
            self.dgram_sock.sendto(payload, self.record_sock_path)

    def _process_packet(self, raw: bytes, addr: tuple) -> None:
        # addr[2] == 4 is PACKET_OUTGOING
        if len(raw) < 14:
            return
        ethertype = struct.unpack("!H", raw[12:14])[0]
        if ethertype == 0x0800:  # IPv4
            if len(raw) < 34:
                return
            ihl = (raw[14] & 0x0F) * 4
            if len(raw) < 14 + ihl + 4:
                return
            proto = raw[23]
            src_ip = socket.inet_ntoa(raw[26:30])
            dst_ip = socket.inet_ntoa(raw[30:34])
            src_port, dst_port = struct.unpack("!HH", raw[14 + ihl : 14 + ihl + 4])
            if proto == 6:  # TCP
                if len(raw) < 14 + ihl + 14:
                    return
                flags = raw[14 + ihl + 13]
                if (flags & 0x02) and not (flags & 0x10):  # SYN only
                    flow = ("tcp", src_ip, src_port, dst_ip, dst_port)
                    if flow not in self.seen_flows:
                        self.seen_flows.add(flow)
                        self._send_record("tcp", dst_ip, dst_port)
            elif proto == 17:  # UDP
                flow = ("udp", src_ip, src_port, dst_ip, dst_port)
                if flow not in self.seen_flows:
                    self.seen_flows.add(flow)
                    self._send_record("udp", dst_ip, dst_port)

        elif ethertype == 0x86DD:  # IPv6
            if len(raw) < 54:
                return
            src_ip = socket.inet_ntop(socket.AF_INET6, raw[22:38])
            dst_ip = socket.inet_ntop(socket.AF_INET6, raw[38:54])
            next_header = raw[20]
            offset = 54
            while next_header in (0, 43, 44, 51, 50, 60) and offset + 8 <= len(raw):
                if next_header == 44:  # Fragment
                    next_header = raw[offset]
                    offset += 8
                elif next_header == 51:  # AH
                    hdr_len = (raw[offset + 1] + 2) * 4
                    next_header = raw[offset]
                    offset += hdr_len
                else:
                    hdr_len = (raw[offset + 1] + 1) * 8
                    next_header = raw[offset]
                    offset += hdr_len

            if offset + 4 <= len(raw):
                src_port, dst_port = struct.unpack("!HH", raw[offset : offset + 4])
                if next_header == 6:  # TCP
                    if offset + 14 <= len(raw):
                        flags = raw[offset + 13]
                        if (flags & 0x02) and not (flags & 0x10):  # SYN
                            flow = ("tcp", src_ip, src_port, dst_ip, dst_port)
                            if flow not in self.seen_flows:
                                self.seen_flows.add(flow)
                                self._send_record("tcp", dst_ip, dst_port)
                elif next_header == 17:  # UDP
                    flow = ("udp", src_ip, src_port, dst_ip, dst_port)
                    if flow not in self.seen_flows:
                        self.seen_flows.add(flow)
                        self._send_record("udp", dst_ip, dst_port)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                raw, addr = self.tap_sock.recvfrom(2048)
                self._process_packet(raw, addr)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break

    def drain(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=0.2)
        try:
            self.tap_sock.setblocking(False)
            while True:
                try:
                    raw, addr = self.tap_sock.recvfrom(2048)
                    self._process_packet(raw, addr)
                except (BlockingIOError, OSError):
                    break
        finally:
            self.tap_sock.close()
            self.dgram_sock.close()


def _setup_network() -> None:
    ip = _executable("ip")
    commands = (
        ("link", "set", "lo", "up"),
        ("link", "add", "egress0", "type", "dummy"),
        ("addr", "add", "10.255.255.1/32", "dev", "egress0"),
        ("addr", "add", "fd00::1/128", "dev", "egress0", "nodad"),
        ("link", "set", "egress0", "up"),
        ("route", "add", "default", "dev", "egress0"),
        ("-6", "route", "add", "default", "dev", "egress0"),
    )
    for args in commands:
        subprocess.run([ip, *args], check=True)  # nosec B603 - argv list, no shell


def _setup_etc_files(etc_dir: Path) -> None:
    resolv_file = etc_dir / "resolv.conf"
    resolv_file.write_text("nameserver 127.0.0.1\n", encoding="utf-8")

    nsswitch_file = etc_dir / "nsswitch.conf"
    nsswitch_file.write_text("hosts: files dns\n", encoding="utf-8")

    hosts_file = etc_dir / "hosts"
    hosts_file.write_text("127.0.0.1 localhost\n::1 localhost\n", encoding="utf-8")

    mount = _executable("mount")
    for src, dst in [
        (resolv_file, Path("/etc/resolv.conf")),
        (nsswitch_file, Path("/etc/nsswitch.conf")),
        (hosts_file, Path("/etc/hosts")),
    ]:
        if dst.exists() or dst.is_symlink():
            target = dst.resolve() if dst.is_symlink() else dst
            subprocess.run(  # nosec B603 - argv list, no shell
                [mount, "--bind", str(src), str(target)],
                check=True,
            )


def _mask_sockets() -> None:
    socket_paths = [
        "/run/systemd/resolve/io.systemd.Resolve",
        "/run/systemd/resolve/io.systemd.Resolve.Monitor",
        "/run/dbus/system_bus_socket",
        "/var/run/dbus/system_bus_socket",
        "/run/nscd/socket",
        "/var/run/nscd/socket",
    ]
    for pattern in ("/run/dbus/*", "/run/systemd/resolve/*", "/run/user/*/bus", "/var/run/nscd/*"):
        for p in glob.glob(pattern):
            if p not in socket_paths:
                socket_paths.append(p)

    mount = _executable("mount")
    for p in socket_paths:
        with suppress(Exception):
            st = os.stat(p, follow_symlinks=False)
            if stat.S_ISSOCK(st.st_mode) or stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                subprocess.run(  # nosec B603 - argv list, no shell
                    [mount, "--bind", "/dev/null", p],
                    check=False,
                )


def _mask_helper_cmdlines() -> None:
    mount = _executable("mount")
    for p in glob.glob("/proc/*/cmdline"):
        with suppress(Exception):
            with open(p, "rb") as f:
                c = f.read()
            if b"egress_sandbox_helper" in c:
                subprocess.run(  # nosec B603 - argv list, no shell
                    [mount, "--bind", "/dev/null", p],
                    check=False,
                )


def _ensure_worker_unshare_supported(temp_dir: Path) -> dict[str, str]:
    probe = subprocess.run(  # nosec B603 - argv list, no shell
        [
            _executable("unshare"),
            "--pid",
            "--fork",
            "--mount-proc",
            _executable("setpriv"),
            "--bounding-set=-all",
            "--inh-caps=-all",
            "--no-new-privs",
            _executable("true"),
        ],
        capture_output=True,
    )
    if probe.returncode == 0:
        return {}

    shim_dir = temp_dir / "shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_c = shim_dir / "shim.c"
    shim_so = shim_dir / "shim.so"
    shim_c.write_text(
        """#define _GNU_SOURCE
#include <dlfcn.h>
#include <string.h>
#include <stdlib.h>

static int (*real_mount)(const char *, const char *, const char *, unsigned long, const void *) = NULL;

int mount(const char *source, const char *target, const char *filesystemtype, unsigned long mountflags, const void *data) {
    if (!real_mount) real_mount = dlsym(RTLD_NEXT, "mount");
    unsetenv("LD_PRELOAD");
    int res = real_mount ? real_mount(source, target, filesystemtype, mountflags, data) : -1;
    if (res != 0 && filesystemtype && strcmp(filesystemtype, "proc") == 0) return 0;
    return res;
}
""",
        encoding="utf-8",
    )
    subprocess.run(  # nosec B603 - argv list, no shell
        [_executable("gcc"), "-shared", "-fPIC", "-o", str(shim_so), str(shim_c), "-ldl"],
        check=True,
    )
    unshare_wrapper = shim_dir / "unshare"
    real_unshare = shutil.which("unshare") or "/usr/bin/unshare"
    unshare_wrapper.write_text(
        f"""#!/bin/sh
export LD_PRELOAD="{shim_so}"
exec "{real_unshare}" "$@"
""",
        encoding="utf-8",
    )
    unshare_wrapper.chmod(0o755)
    return {"PATH": str(shim_dir) + os.pathsep + os.environ.get("PATH", "")}


def main() -> None:
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python3 -m omp_work.egress_sandbox_helper <config.json>\n")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text(encoding="utf-8"))

    record_sock = config["record_sock"]
    setup_ok_path = config.get("setup_ok")
    argv = config["argv"]
    workdir = config.get("workdir")
    env = config.get("env")

    # 1. lo up, dummy egress0
    _setup_network()

    # 2. AF_PACKET tap on egress0
    tap = PacketTap(record_sock)
    tap.start()

    # 3. Bind-mount generated /etc/ files
    temp_dir = Path(tempfile.mkdtemp(prefix="omp-helper-"))
    etc_dir = temp_dir / "etc"
    etc_dir.mkdir(parents=True, exist_ok=True)
    _setup_etc_files(etc_dir)

    # 4. Mask systemd-resolved, nscd, dbus sockets
    _mask_sockets()

    # 5. Mask cmdline of helper
    _mask_helper_cmdlines()

    # 6. Ensure unshare with mount-proc works
    env_update = _ensure_worker_unshare_supported(temp_dir)

    worker_env = os.environ.copy() if env is None else dict(env)
    if "PATH" in env_update:
        worker_env["PATH"] = env_update["PATH"]
    # Match subprocess: a provided env searches its PATH, or os.defpath when PATH is absent.
    # The shim directory, when present, is first, so unshare resolves to that wrapper.
    search_path = worker_env["PATH"] if "PATH" in worker_env else os.defpath

    # Signal that setup is complete before launching the worker
    if setup_ok_path:
        Path(setup_ok_path).touch()

    # 7. Run worker
    worker_cmd = [
        _executable("unshare", search_path),
        "--pid",
        "--fork",
        "--mount-proc",
        _executable("setpriv", search_path),
        "--bounding-set=-all",
        "--inh-caps=-all",
        "--no-new-privs",
        *argv,
    ]

    worker = subprocess.Popen(  # nosec B603 - argv list, no shell
        worker_cmd,
        cwd=workdir,
        env=worker_env,
    )
    returncode = worker.wait()

    # Drain tap before exit
    time.sleep(0.05)
    tap.drain()

    shutil.rmtree(temp_dir, ignore_errors=True)
    sys.exit(returncode)


if __name__ == "__main__":
    main()
