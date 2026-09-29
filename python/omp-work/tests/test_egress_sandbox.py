"""Tests for egress sandbox isolation and packet recording (OMP-431)."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import socket
import subprocess
import tempfile
from typing import Any
from unittest.mock import patch

import pytest

from omp_work.egress_policy import Identity, MemoryRecorder
from omp_work.egress_sandbox import SandboxUnavailable, run_sandboxed


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


def _make_identity(mission_id: str = "m-test-431", worker_id: str = "w-test-431") -> Identity:
    return Identity(
        workspace_id="ws-test-431",
        project_id="proj-test-431",
        mission_id=mission_id,
        worker_id=worker_id,
        stage="repository",
    )


def _get_host_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def test_tcp_egress_fails_and_records() -> None:
    identity = _make_identity(mission_id="m-tcp-4", worker_id="w-tcp-4")
    recorder = MemoryRecorder()

    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.settimeout(0.3)\n"
        "try:\n"
        "    s.connect(('93.184.216.34', 443))\n"
        "except (TimeoutError, OSError):\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )

    rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
    assert rc == 0
    assert len(recorder.records) >= 1
    rec = recorder.records[0]
    assert rec.protocol == "tcp"
    assert rec.ip == "93.184.216.34"
    assert rec.port == 443
    assert rec.channel == "raw"
    assert rec.code == "raw_egress_refused"
    assert rec.outcome == "refused"
    assert rec.stage == "repository"
    assert rec.mission_id == "m-tcp-4"
    assert rec.worker_id == "w-tcp-4"


def test_tcp6_egress_fails_and_records() -> None:
    identity = _make_identity(mission_id="m-tcp-6", worker_id="w-tcp-6")
    recorder = MemoryRecorder()

    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)\n"
        "s.settimeout(0.3)\n"
        "try:\n"
        "    s.connect(('2606:2800:220:1::1', 443))\n"
        "except (TimeoutError, OSError):\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )

    rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
    assert rc == 0
    assert len(recorder.records) >= 1
    rec = recorder.records[0]
    assert rec.protocol == "tcp"
    assert rec.ip == "2606:2800:220:1::1"
    assert rec.port == 443
    assert rec.channel == "raw"
    assert rec.code == "raw_egress_refused"
    assert rec.outcome == "refused"
    assert rec.stage == "repository"
    assert rec.mission_id == "m-tcp-6"
    assert rec.worker_id == "w-tcp-6"


def test_udp_host_lan_gets_nothing_and_records() -> None:
    identity = _make_identity(mission_id="m-udp-lan", worker_id="w-udp-lan")
    recorder = MemoryRecorder()

    lan_ip = _get_host_lan_ip()
    host_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    host_sock.bind((lan_ip, 0))
    host_sock.settimeout(0.3)
    host_port = host_sock.getsockname()[1]

    try:
        code = (
            "import socket\n"
            "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
            f"s.sendto(b'lan-probe', ('{lan_ip}', {host_port}))\n"
        )

        rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
        assert rc == 0

        # Host listener must get nothing
        with pytest.raises((TimeoutError, socket.timeout)):
            host_sock.recvfrom(1024)
    finally:
        host_sock.close()

    assert len(recorder.records) >= 1
    rec = recorder.records[0]
    assert rec.protocol == "udp"
    assert rec.ip == lan_ip
    assert rec.port == host_port
    assert rec.channel == "raw"
    assert rec.code == "raw_egress_refused"
    assert rec.outcome == "refused"
    assert rec.mission_id == "m-udp-lan"
    assert rec.worker_id == "w-udp-lan"


def test_udp_dns_fails_and_records() -> None:
    identity = _make_identity(mission_id="m-udp-dns", worker_id="w-udp-dns")
    recorder = MemoryRecorder()

    code = (
        "import socket\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "s.settimeout(0.3)\n"
        "s.sendto(b'query', ('8.8.8.8', 53))\n"
        "try:\n"
        "    s.recvfrom(512)\n"
        "except (TimeoutError, socket.timeout):\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )

    rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
    assert rc == 0
    assert len(recorder.records) >= 1
    rec = recorder.records[0]
    assert rec.protocol == "udp"
    assert rec.ip == "8.8.8.8"
    assert rec.port == 53
    assert rec.channel == "raw"
    assert rec.code == "raw_egress_refused"
    assert rec.outcome == "refused"
    assert rec.mission_id == "m-udp-dns"
    assert rec.worker_id == "w-udp-dns"


def test_dns_resolution_and_hosts_file() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    code = (
        "import socket\n"
        "# getaddrinfo(example.com) must fail\n"
        "try:\n"
        "    socket.getaddrinfo('example.com', 80)\n"
        "except socket.gaierror:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(1)\n"
        "# getaddrinfo(socket.gethostname()) must fail\n"
        "try:\n"
        "    socket.getaddrinfo(socket.gethostname(), 80)\n"
        "except socket.gaierror:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit(2)\n"
        "# /etc/hosts must name only localhost\n"
        "with open('/etc/hosts', 'r') as f:\n"
        "    for line in f:\n"
        "        line = line.strip()\n"
        "        if line and not line.startswith('#'):\n"
        "            parts = line.split()\n"
        "            if parts[1:] != ['localhost']:\n"
        "                raise SystemExit(3)\n"
    )

    rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
    assert rc == 0


def test_ip_link_set_egress0_down_fails() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    rc = run_sandboxed(["sh", "-c", "ip link set egress0 down"], identity, recorder, None, None, 10.0)
    assert rc != 0


def test_no_proc_cmdline_names_egress_sandbox_helper() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    code = (
        "import glob\n"
        "helper_name = 'egress' + '_sandbox_helper'\n"
        "target = helper_name.encode()\n"
        "found = []\n"
        "for p in glob.glob('/proc/*/cmdline'):\n"
        "    try:\n"
        "        with open(p, 'rb') as f:\n"
        "            if target in f.read():\n"
        "                found.append(p)\n"
        "    except (FileNotFoundError, PermissionError):\n"
        "        pass\n"
        "if found:\n"
        "    raise SystemExit(1)\n"
    )

    rc = run_sandboxed(["sh", "-c", f"python3 -c {shlex.quote(code)}"], identity, recorder, None, None, 10.0)
    assert rc == 0


def test_missing_tools_raise_sandbox_unavailable() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    with patch("shutil.which", return_value=None):
        with pytest.raises(SandboxUnavailable) as exc_info:
            run_sandboxed(["true"], identity, recorder, None, None, 5.0)
        assert "not found on PATH" in str(exc_info.value)


def test_setup_probe_failure_raises_sandbox_unavailable() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1, stderr=b"Denied")):
        with pytest.raises(SandboxUnavailable) as exc_info:
            run_sandboxed(["true"], identity, recorder, None, None, 5.0)
        assert "probe failed" in str(exc_info.value)


def test_custom_sockets_root_directory() -> None:
    identity = _make_identity()
    recorder = MemoryRecorder()

    with tempfile.TemporaryDirectory(prefix="custom-sockets-") as tmp:
        custom_root = Path(tmp) / "custom-root"
        rc = run_sandboxed(
            ["sh", "-c", "echo custom_root"],
            identity,
            recorder,
            None,
            None,
            5.0,
            sockets_root=custom_root,
        )
        assert rc == 0
        assert custom_root.exists()
        assert (custom_root.stat().st_mode & 0o777) == 0o700
