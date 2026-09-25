"""A SIGKILLed test process must not leave its native postgres server behind."""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CHILD = """
import sys
import time
from pathlib import Path

from pg_native import native_postgres

root = Path(sys.argv[1])
port = int(sys.argv[2])
ready = Path(sys.argv[3])
with native_postgres(root, port):
    ready.write_text("ready")
    time.sleep(3600)
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _process_alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_sigkill_of_test_process_stops_postgres(tmp_path: Path) -> None:
    root = tmp_path / "postgres"
    ready = tmp_path / "ready"
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(root), str(port), str(ready)],
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert child.poll() is None, "fixture child exited before becoming ready"
            assert time.monotonic() < deadline, "fixture child never became ready"
            time.sleep(0.1)
        postgres_pid = int((root / "pgdata" / "postmaster.pid").read_text().splitlines()[0])
        assert _process_alive(postgres_pid)

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)

        deadline = time.monotonic() + 10
        while _process_alive(postgres_pid):
            assert time.monotonic() < deadline, (
                f"postgres {postgres_pid} survived SIGKILL of the test process"
            )
            time.sleep(0.1)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
