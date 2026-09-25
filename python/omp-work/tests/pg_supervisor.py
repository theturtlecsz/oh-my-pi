"""Run a native postgres server tied to the lifetime of the test process.

``pg_ctl start`` daemonizes the postmaster into its own session, so a SIGKILLed
pytest leaves the server running and holding its port, memory and data
directory. This launcher instead runs ``postgres`` as a direct child in the
caller's process group and stops it when the caller's pipe closes (EOF on
stdin) or when this process is signalled, so the server cannot outlive the test
process. The postmaster additionally carries a parent-death signal relative to
this supervisor, so even a SIGKILLed supervisor takes the server with it.
"""

from __future__ import annotations

import ctypes
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

_PR_SET_PDEATHSIG = 1


def _set_pdeathsig(signum: int) -> None:
    """Ask the kernel to signal this process when its parent dies."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signum, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG)")


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    # SIGINT is postgres's fast shutdown; escalate if it does not exit promptly.
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    data_dir = Path(sys.argv[1])
    port = sys.argv[2]
    log_path = Path(sys.argv[3])
    stopping = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    # Die with the test process even if it is SIGKILLed and never closes the pipe.
    _set_pdeathsig(signal.SIGTERM)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [
                "postgres",
                "-D",
                str(data_dir),
                "-p",
                port,
                # All fixture clients use loopback TCP. Disable unused Unix
                # sockets so long or space-containing pytest paths cannot break
                # startup.
                "-c",
                "unix_socket_directories=",
                "-c",
                "listen_addresses=127.0.0.1",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            # The postmaster dies with this supervisor even if the supervisor is
            # SIGKILLed and never runs its own shutdown path.
            preexec_fn=lambda: _set_pdeathsig(signal.SIGKILL),
        )
        try:
            while not stopping and process.poll() is None:
                readable, _, _ = select.select([0], [], [], 0.5)
                if readable and not os.read(0, 4096):
                    break  # EOF: the test process closed the pipe or died
        finally:
            _stop(process)
    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
