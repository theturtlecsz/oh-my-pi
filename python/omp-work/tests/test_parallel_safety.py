"""Contract: parallel_safety keeps uuid-bearing parametrize ids stable across xdist workers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
_PROBE = '''\
import uuid

import pytest


@pytest.mark.parametrize("method, path", [("GET", f"/v1/{uuid.uuid4()}/items")])
def test_probe(method, path):
    assert path.startswith("/v1/")
'''
_DIVERGED = "Different tests were collected"
_SOCKET_PROBE = '''\
import socket
import tempfile
from pathlib import Path


def test_unix_socket_bind_fits_sun_path_limit(tmp_path):
    sockroot = tmp_path / "sockroot"
    sockroot.mkdir()
    path = Path(tempfile.mkdtemp(prefix="sb-", dir=sockroot)) / "record.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(str(path))
    finally:
        sock.close()
    assert len(str(path).encode()) <= 107
'''
_BASETEMP_BYTES = 55
_AF_UNIX_TOO_LONG = "AF_UNIX path too long"


def _run_probe(*extra: str) -> subprocess.CompletedProcess[str]:
    probe_dir = Path(tempfile.mkdtemp(prefix="omp-parallel-safety-"))
    base_dir = Path(tempfile.mkdtemp(prefix="omp-parallel-safety-base-"))
    try:
        (probe_dir / "test_probe.py").write_text(_PROBE)
        env = {**os.environ, "PYTHONPATH": str(_TESTS_DIR)}
        return subprocess.run(
            [
                sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider",
                "--noconftest", f"--basetemp={base_dir / 'bt'}", *extra, "test_probe.py",
            ],
            cwd=probe_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        shutil.rmtree(base_dir, ignore_errors=True)


def _run_socket_probe(*extra: str) -> subprocess.CompletedProcess[str]:
    probe_dir = Path(tempfile.mkdtemp(prefix="omp-ps-sock-"))
    base_parent = Path(tempfile.mkdtemp(prefix="omp-ps-sockbase-"))
    try:
        pad = _BASETEMP_BYTES - len(str(base_parent).encode()) - 1
        assert pad > 0, base_parent
        basetemp = base_parent / ("b" * pad)
        assert len(str(basetemp).encode()) == _BASETEMP_BYTES
        (probe_dir / "test_probe.py").write_text(_SOCKET_PROBE)
        env = {**os.environ, "PYTHONPATH": str(_TESTS_DIR)}
        return subprocess.run(
            [
                sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider",
                "--noconftest", f"--basetemp={basetemp}", *extra, "test_probe.py",
            ],
            cwd=probe_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
        shutil.rmtree(base_parent, ignore_errors=True)


def test_uuid_parametrize_ids_collect_identically_across_workers() -> None:
    result = _run_probe("-p", "parallel_safety")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _DIVERGED not in out


def test_probe_without_plugin_diverges_across_workers() -> None:
    result = _run_probe()
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert _DIVERGED in out


def test_unix_socket_bind_fits_sun_path_limit_with_plugin() -> None:
    result = _run_socket_probe("-p", "parallel_safety")
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert _AF_UNIX_TOO_LONG not in out


def test_unix_socket_bind_overflows_without_plugin() -> None:
    result = _run_socket_probe()
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert _AF_UNIX_TOO_LONG in out


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/a/123E4567-E89B-12D3-A456-426614174000/b", "/a/uuid/b"),
        (7, None),
    ],
)
def test_parametrize_id_hook(value: object, expected: str | None) -> None:
    from parallel_safety import pytest_make_parametrize_id

    assert pytest_make_parametrize_id(None, value, "x") == expected  # type: ignore[arg-type]
