"""Egress sandbox: run commands in an isolated network and user namespace (OMP-431)."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
import shutil
import socket
import subprocess  # nosec B404 - argv lists only, no shell
import sys
import tempfile
import threading
from datetime import UTC, datetime
from typing import Mapping, Sequence

from omp_work.egress_policy import EgressRecord, EgressRecorder, Identity


class SandboxUnavailable(RuntimeError):
    """Raised when sandbox tools are missing or sandbox setup fails."""


def _executable(name: str, path: str | None = None) -> str:
    """Absolute path of a fixed tool, or the bare name when it is not on PATH."""
    found = shutil.which(name, path=path)
    return found if found is not None else name


def run_sandboxed(
    argv: Sequence[str],
    identity: Identity,
    recorder: EgressRecorder,
    workdir: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float | int | None,
    sockets_root: str | Path | None = None,
) -> int:
    """Run argv inside an isolated egress sandbox.

    Missing tools or setup failure raises :class:`SandboxUnavailable`.
    Returns the worker's exit status.
    """
    resolved: dict[str, str] = {}
    for tool in ("unshare", "setpriv", "ip"):
        found = shutil.which(tool)
        if found is None:
            raise SandboxUnavailable(f"Required sandbox tool '{tool}' not found on PATH")
        resolved[tool] = found

    # Probe namespace unshare capability
    try:
        probe = subprocess.run(  # nosec B603 - argv list, no shell
            [
                resolved["unshare"],
                "--user",
                "--map-root-user",
                "--net",
                "--mount",
                "--pid",
                "--fork",
                _executable("true"),
            ],
            capture_output=True,
            timeout=5,
        )
        if probe.returncode != 0:
            raise SandboxUnavailable(
                f"Sandbox unshare probe failed (exit code {probe.returncode}): "
                f"{probe.stderr.decode('utf-8', errors='replace')}"
            )
    except Exception as e:
        if isinstance(e, SandboxUnavailable):
            raise
        raise SandboxUnavailable(f"Sandbox unshare probe failed: {e}") from e

    if sockets_root is None:
        sockets_root = Path(tempfile.gettempdir()) / f"omp-egress-{os.getuid()}"
    else:
        sockets_root = Path(sockets_root)

    try:
        sockets_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(sockets_root, 0o700)
        sandbox_dir = Path(tempfile.mkdtemp(prefix="sb-", dir=str(sockets_root)))
        os.chmod(sandbox_dir, 0o700)
    except Exception as e:
        raise SandboxUnavailable(f"Failed to create sandbox directory: {e}") from e

    sock_path = sandbox_dir / "record.sock"
    dgram_sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        dgram_sock.bind(str(sock_path))
        os.chmod(sock_path, 0o700)
        dgram_sock.settimeout(0.1)
    except Exception as e:
        dgram_sock.close()
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        raise SandboxUnavailable(f"Failed to bind record.sock: {e}") from e

    stop_event = threading.Event()

    def _record_datagram(data: bytes) -> None:
        if not data:
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception:
            return

        protocol = str(payload.get("protocol", "raw")).lower()
        ip_val = payload.get("ip")
        ip_str = str(ip_val) if ip_val is not None else None
        port = int(payload.get("port", 0))

        if ip_str:
            url = f"{protocol}://[{ip_str}]:{port}" if ":" in ip_str else f"{protocol}://{ip_str}:{port}"
        else:
            url = ""

        rec = EgressRecord(
            workspace_id=identity.workspace_id,
            project_id=identity.project_id,
            mission_id=identity.mission_id,
            worker_id=identity.worker_id,
            stage=identity.stage,
            channel="raw",
            protocol=protocol,
            host=ip_str or "",
            ip=ip_str,
            port=port,
            method="",
            url=url,
            klass="none",
            outcome="refused",
            code="raw_egress_refused",
            policy_id=None,
            at=datetime.now(UTC),
        )
        with suppress(Exception):
            recorder.record(rec)

    def _listener() -> None:
        while not stop_event.is_set():
            try:
                data, _ = dgram_sock.recvfrom(65536)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            _record_datagram(data)

    listener_thread = threading.Thread(target=_listener, daemon=True)
    listener_thread.start()

    config_path = sandbox_dir / "config.json"
    setup_ok = sandbox_dir / "setup_ok"

    config = {
        "record_sock": str(sock_path),
        "setup_ok": str(setup_ok),
        "argv": list(argv),
        "workdir": str(workdir) if workdir is not None else None,
        "env": dict(env) if env is not None else None,
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")

    helper_cmd = [
        resolved["unshare"],
        "--user",
        "--map-root-user",
        "--net",
        "--mount",
        "--fork",
        sys.executable,
        "-m",
        "omp_work.egress_sandbox_helper",
        str(config_path),
    ]

    try:
        proc = subprocess.Popen(  # nosec B603 - argv list, no shell
            helper_cmd,
            env=os.environ.copy(),
        )
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
    except SandboxUnavailable:
        raise
    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        raise SandboxUnavailable(f"Failed to execute sandbox helper: {e}") from e
    finally:
        stop_event.set()
        listener_thread.join(timeout=1.0)
        try:
            dgram_sock.setblocking(False)
            while True:
                try:
                    data, _ = dgram_sock.recvfrom(65536)
                    _record_datagram(data)
                except (BlockingIOError, OSError):
                    break
        finally:
            dgram_sock.close()
            setup_succeeded = setup_ok.exists()
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    if not setup_succeeded:
        raise SandboxUnavailable(f"Sandbox setup failed inside helper (exit code {returncode})")

    return returncode
