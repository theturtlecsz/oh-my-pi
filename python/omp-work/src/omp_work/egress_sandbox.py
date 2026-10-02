"""Egress sandbox: run commands in an isolated network and user namespace (OMP-431).

:func:`run_sandboxed` starts the worker in fresh user, mount, network, and PID
namespaces whose only egress is the AF_PACKET tap on ``egress0``. The tap
records every refused TCP SYN and UDP flow as an :class:`EgressRecord` on
channel ``raw``.

With a ``gateway`` the sandbox also gets a policy-controlled name service and a
loopback proxy: ``<dir>/dns.sock`` answers A/AAAA from
``gateway.resolver`` only for the model proxy host, a named registry or remote
host, or a host an unexpired standing policy grants, and refuses every other
name with ``name_not_allowed`` on channel ``dns``; ``<dir>/ws.sock`` relays to
``workservice`` and nothing else. Both are consumed by
:mod:`omp_work.egress_sandbox_helper`, which exposes them on loopback inside the
worker's network namespace. Without a gateway no name service or WorkService
relay exists and names fail as before.
"""

from __future__ import annotations

import json
import os
import signal
import time
from contextlib import suppress
from pathlib import Path
import shutil
import socket
import struct
import subprocess  # nosec B404 - argv lists only, no shell
import sys
import tempfile
import threading
from datetime import UTC, datetime
from typing import Iterable, Mapping, Protocol, Sequence, runtime_checkable

from omp_work.egress_policy import (
    EgressPolicy,
    EgressRecord,
    EgressRecorder,
    Identity,
    _is_expired,
    _norm_host,
    _parse_destination,
)
from omp_work.egress_sandbox_helper import _die_with_parent

__all__ = [
    "RelayGateway",
    "ResearchStageRefused",
    "SandboxUnavailable",
    "run_research_stage",
    "run_sandboxed",
]

_DNS_PORT = 53
_PROXY_PORT = 3128
_OUTPUT_LIMIT = 1 << 20

_WORKER_ENV_ALLOWED = frozenset({"PATH", "LANG", "TERM", "TZ"})


class SandboxUnavailable(RuntimeError):
    """Raised when sandbox tools are missing or sandbox setup fails."""


class ResearchStageRefused(RuntimeError):
    """Raised when a research-stage launch is refused before any process starts.

    ``code`` is ``worktree_not_allowed``, ``context_not_allowed``, or
    ``credential_not_allowed``.
    """

    code: str

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def _executable(name: str, path: str | None = None) -> str:
    """Absolute path of a fixed tool, or the bare name when it is not on PATH."""
    found = shutil.which(name, path=path)
    return found if found is not None else name


@runtime_checkable
class RelayGateway(Protocol):
    """The egress gateway surface the sandbox binds into the worker namespace."""

    def open_sandbox(self, identity: Identity, directory: str | os.PathLike[str]) -> object:
        ...

    def close_sandbox(self, identity: Identity) -> None:
        ...

    def resolver(self, host: str, port: int) -> Iterable[str]:
        ...

    def policy_source(self, identity: Identity, now: datetime) -> EgressPolicy:
        ...


def _gateway_resolver(gateway: RelayGateway) -> object:
    """The gateway's resolver, however that gateway spells it."""
    resolver = getattr(gateway, "resolver", None)
    if resolver is None:
        resolver = getattr(gateway, "_resolver", None)
    return resolver


def _gateway_policy_source(gateway: RelayGateway):
    """The gateway's policy source, however that gateway spells it."""
    source = getattr(gateway, "policy_source", None)
    if source is None:
        source = getattr(gateway, "_policy_source", None)
    return source


def _dns_question(data: bytes) -> tuple[bytes, str, int] | None:
    """Return ``(question bytes, name, qtype)`` for one query, or None."""
    if len(data) < 12:
        return None
    (qdcount,) = struct.unpack("!H", data[4:6])
    if qdcount < 1:
        return None
    offset = 12
    labels: list[str] = []
    while offset < len(data):
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0:
            return None
        offset += 1
        if offset + length > len(data):
            return None
        labels.append(data[offset : offset + length].decode("ascii", errors="replace"))
        offset += length
    if offset + 4 > len(data):
        return None
    (qtype,) = struct.unpack("!H", data[offset : offset + 2])
    return data[12 : offset + 4], ".".join(labels), qtype


def _dns_message(query_id: int, question: bytes, answers: list[bytes]) -> bytes:
    """Frame a DNS reply. No answers means RCODE 5 (REFUSED)."""
    flags = 0x8180 if answers else 0x8185
    header = struct.pack("!HHHHHH", query_id, flags, 1, len(answers), 0, 0)
    return header + question + b"".join(answers)


def _dns_answer(qtype: int, address: str) -> bytes | None:
    """An answer record for the query type, or None when the family differs."""
    family = socket.AF_INET if qtype == 1 else socket.AF_INET6
    try:
        packed = socket.inet_pton(family, address)
    except OSError:
        return None
    return b"\xc0\x0c" + struct.pack("!HHIH", qtype, 1, 60, len(packed)) + packed


def _policy_hosts(policy: EgressPolicy, now: datetime) -> set[str]:
    """Names the policy controls: model proxy, registries, remotes, standing."""
    hosts = {_norm_host(str(policy.model_proxy[0]))}
    hosts |= {_norm_host(host) for host, _port in policy.registries}
    hosts |= {_norm_host(host) for _scheme, host, _port, _repo in policy.remotes}
    for entry in policy.standing:
        if _is_expired(entry, now):
            continue
        for raw in entry.destinations:
            parsed = _parse_destination(raw)
            if parsed is not None:
                hosts.add(parsed[1])
    return hosts


class _DnsResponder:
    """Answers A/AAAA on ``sock_path`` from the gateway resolver, or refuses."""

    def __init__(
        self,
        identity: Identity,
        recorder: EgressRecorder,
        gateway: RelayGateway,
        sock_path: Path,
    ) -> None:
        self._identity = identity
        self._recorder = recorder
        self._gateway = gateway
        self._path = sock_path
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._sock.bind(str(self._path))
        os.chmod(self._path, 0o600)
        self._sock.settimeout(0.1)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()
        with suppress(OSError):
            self._path.unlink()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65536)
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            with suppress(Exception):
                self._handle(data, addr)

    def _handle(self, data: bytes, addr: object) -> None:
        parsed = _dns_question(data)
        if parsed is None or len(data) < 2:
            return
        question, name, qtype = parsed
        (query_id,) = struct.unpack("!H", data[:2])
        now = datetime.now(UTC)
        normalized = _norm_host(name)
        policy = _gateway_policy_source(self._gateway)(self._identity, now)
        allowed = _policy_hosts(policy, now)
        if normalized not in allowed or qtype not in (1, 28):
            self._sock.sendto(_dns_message(query_id, question, []), addr)
            self._record_refusal(name, now)
            return
        resolver = _gateway_resolver(self._gateway)
        try:
            addresses = [str(item) for item in resolver(normalized, 443)] if resolver else []
        except Exception:
            addresses = []
        answers: list[bytes] = []
        for address in addresses:
            entry = _dns_answer(qtype, address)
            if entry is not None:
                answers.append(entry)
        self._sock.sendto(_dns_message(query_id, question, answers), addr)

    def _record_refusal(self, name: str, now: datetime) -> None:
        identity = self._identity
        record = EgressRecord(
            workspace_id=identity.workspace_id,
            project_id=identity.project_id,
            mission_id=identity.mission_id,
            worker_id=identity.worker_id,
            stage=identity.stage,
            channel="dns",
            protocol="dns",
            host=name,
            ip=None,
            port=_DNS_PORT,
            method="",
            url="",
            klass="none",
            outcome="refused",
            code="name_not_allowed",
            policy_id=None,
            at=now,
        )
        with suppress(Exception):
            self._recorder.record(record)


class _TcpOverUnixRelay:
    """Accepts AF_UNIX connections on ``sock_path`` and relays each to TCP."""

    def __init__(self, sock_path: Path, tcp_host: str, tcp_port: int) -> None:
        self._path = sock_path
        self._host = tcp_host
        self._port = tcp_port
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._sock.bind(str(self._path))
        os.chmod(self._path, 0o600)
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._sock.close()
        with suppress(OSError):
            self._path.unlink()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            threading.Thread(target=self._relay, args=(conn,), daemon=True).start()

    def _relay(self, conn: socket.socket) -> None:
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            upstream.connect((self._host, self._port))
        except OSError:
            conn.close()
            upstream.close()
            return
        _pump(conn, upstream)


def _pump(left: socket.socket, right: socket.socket) -> None:
    """Relay bytes both ways until either side closes."""

    def forward(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            with suppress(OSError):
                dst.shutdown(socket.SHUT_WR)

    other = threading.Thread(target=forward, args=(right, left), daemon=True)
    other.start()
    forward(left, right)
    other.join(timeout=5.0)
    with suppress(OSError):
        left.close()
    with suppress(OSError):
        right.close()


def _capture_output(stream: object, output: bytearray) -> None:
    """Copy ``stream`` into ``output`` up to 1 MiB. Further bytes are discarded."""
    read = getattr(stream, "read")
    try:
        while True:
            chunk = read(65536)
            if not chunk:
                break
            room = _OUTPUT_LIMIT - len(output)
            if room > 0:
                output.extend(chunk[:room])
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            with suppress(OSError):
                close()


def _kill_session(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the sandbox session. ``start_new_session`` makes ``pid`` the group."""
    pid = proc.pid
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        proc.wait(timeout=5)


def _wait_sandboxed(
    proc: subprocess.Popen[bytes],
    timeout: float | int | None,
    cancel: threading.Event | None,
) -> tuple[int, bool]:
    """Wait for ``proc``. Cancel and timeout both SIGKILL the session.

    Cancel returns ``(-9, True)``. A timeout raises ``subprocess.TimeoutExpired``
    after the session is killed. A normal exit returns ``(code, False)``.
    """
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    while True:
        if cancel is not None and cancel.is_set():
            _kill_session(proc)
            return -9, True
        slice_s = 0.05
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_session(proc)
                raise subprocess.TimeoutExpired(getattr(proc, "args", ()), timeout if timeout is not None else 0)
            slice_s = min(slice_s, remaining)
        try:
            return proc.wait(timeout=slice_s), False
        except subprocess.TimeoutExpired:
            continue


def _run_sandbox_body(
    argv: Sequence[str],
    identity: Identity,
    recorder: EgressRecorder,
    workdir: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float | int | None,
    sockets_root: str | Path | None = None,
    gateway: RelayGateway | None = None,
    workservice: tuple[str, int] | None = None,
    ca_cert_path: str | Path | None = None,
    root: str | None = None,
    ro_binds: Sequence[str | Path] = (),
    worktree: str | Path | None = None,
    cancel: threading.Event | None = None,
    output: bytearray | None = None,
) -> int:
    """Shared sandbox body. ``root`` names the worker profile in config.json."""
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

    dns_responder: _DnsResponder | None = None
    ws_relay: _TcpOverUnixRelay | None = None
    proxy_sock_path: Path | None = None
    mask_sockets_root = gateway is not None or workservice is not None
    returncode = -1
    canceled = False
    drain: threading.Thread | None = None

    try:
        if gateway is not None:
            proxy_sock_path = Path(str(gateway.open_sandbox(identity, sandbox_dir)))
            dns_responder = _DnsResponder(identity, recorder, gateway, sandbox_dir / "dns.sock")
            dns_responder.start()
        if workservice is not None:
            ws_relay = _TcpOverUnixRelay(sandbox_dir / "ws.sock", str(workservice[0]), int(workservice[1]))
            ws_relay.start()

        config = {
            "record_sock": str(sock_path),
            "setup_ok": str(setup_ok),
            "root": root,
            "argv": list(argv),
            "workdir": str(workdir) if workdir is not None else None,
            "env": dict(env) if env is not None else None,
            "proxy_sock": str(proxy_sock_path) if proxy_sock_path is not None else None,
            "dns_sock": str(sandbox_dir / "dns.sock") if gateway is not None else None,
            "ws_sock": str(sandbox_dir / "ws.sock") if ws_relay is not None else None,
            "workservice_port": int(workservice[1]) if workservice is not None else None,
            "ca_cert_path": str(ca_cert_path) if ca_cert_path is not None else None,
            "sockets_root": str(sockets_root) if mask_sockets_root else None,
            "sandbox_dir": str(sandbox_dir) if mask_sockets_root else None,
        }
        if worktree is not None:
            config["worktree"] = str(worktree)
        if ro_binds:
            config["ro_binds"] = [str(item) for item in ro_binds]
        config_path.write_text(json.dumps(config), encoding="utf-8")

        helper_cmd = [
            resolved["unshare"],
            "--user",
            "--map-root-user",
            "--net",
            "--mount",
            "--fork",
            "--kill-child",
            sys.executable,
            "-m",
            "omp_work.egress_sandbox_helper",
            str(config_path),
        ]

        proc = subprocess.Popen(  # nosec B603 - argv list, no shell
            helper_cmd,
            env=os.environ.copy(),
            start_new_session=True,
            preexec_fn=_die_with_parent,
            stdout=subprocess.PIPE if output is not None else None,
            stderr=subprocess.STDOUT if output is not None else None,
        )
        if output is not None and proc.stdout is not None:
            drain = threading.Thread(
                target=_capture_output,
                args=(proc.stdout, output),
                daemon=True,
            )
            drain.start()
        try:
            returncode, canceled = _wait_sandboxed(proc, timeout, cancel)
        except subprocess.TimeoutExpired:
            raise
    except SandboxUnavailable:
        raise
    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        raise SandboxUnavailable(f"Failed to execute sandbox helper: {e}") from e
    finally:
        if drain is not None:
            drain.join(timeout=2)
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
            for relay in (ws_relay, dns_responder):
                if relay is not None:
                    with suppress(Exception):
                        relay.stop()
            if gateway is not None:
                with suppress(Exception):
                    gateway.close_sandbox(identity)
            setup_succeeded = setup_ok.exists()
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    if canceled:
        return -9
    if not setup_succeeded:
        raise SandboxUnavailable(f"Sandbox setup failed inside helper (exit code {returncode})")

    return returncode


def run_sandboxed(
    argv: Sequence[str],
    identity: Identity,
    recorder: EgressRecorder,
    workdir: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float | int | None,
    sockets_root: str | Path | None = None,
    gateway: RelayGateway | None = None,
    workservice: tuple[str, int] | None = None,
    ca_cert_path: str | Path | None = None,
    root: str | None = None,
    ro_binds: Sequence[str | Path] = (),
    worktree: str | Path | None = None,
    cancel: threading.Event | None = None,
    output: bytearray | None = None,
) -> int:
    """Run argv inside an isolated egress sandbox.

    Missing tools or setup failure raises :class:`SandboxUnavailable`, and a
    research identity raises :class:`ResearchStageRefused` before any tool
    lookup, probe, or socket. ``root`` selects the jail profile (``research``
    is launched only by :func:`run_research_stage`; ``worker`` is the worktree
    jail). Returns the worker's exit status.

    The outer process is a new session and dies with its parent. Both unshare
    calls use ``--kill-child``. ``cancel``, when set, SIGKILLs that session and
    returns -9. A timeout SIGKILLs the same session and raises
    ``subprocess.TimeoutExpired``. ``output``, when given, receives at most
    1 MiB of combined worker output.
    """
    if identity.stage == "research":
        raise ResearchStageRefused("research_stage_launcher")
    return _run_sandbox_body(
        argv,
        identity,
        recorder,
        workdir,
        env,
        timeout,
        sockets_root,
        gateway,
        workservice,
        ca_cert_path,
        root=root,
        ro_binds=ro_binds,
        worktree=worktree,
        cancel=cancel,
        output=output,
    )


def run_research_stage(
    argv: Sequence[str],
    identity: Identity,
    recorder: EgressRecorder,
    workdir: str | Path | None,
    env: Mapping[str, str] | None,
    timeout: float | int | None,
    sockets_root: str | Path | None = None,
    gateway: RelayGateway | None = None,
    workservice: tuple[str, int] | None = None,
    ca_cert_path: str | Path | None = None,
    cwd: str | Path | None = None,
    context_paths: Sequence[str | Path] = (),
) -> int:
    """Run a research-stage worker in the sandbox.

    Refuses before any process starts: a ``workdir`` or ``cwd`` with
    ``worktree_not_allowed``, non-empty ``context_paths`` with
    ``context_not_allowed``, and an ``env`` naming anything but ``PATH``,
    ``LANG``, ``TERM``, ``TZ``, or ``LC_*`` with ``credential_not_allowed``.
    Otherwise runs the same body as :func:`run_sandboxed` with config root
    ``research``.
    """
    if workdir is not None or cwd is not None:
        raise ResearchStageRefused("worktree_not_allowed")
    if tuple(context_paths):
        raise ResearchStageRefused("context_not_allowed")
    if env is not None:
        for name in env:
            upper = name.upper()
            if upper in _WORKER_ENV_ALLOWED or upper.startswith("LC_"):
                continue
            raise ResearchStageRefused("credential_not_allowed")
    return _run_sandbox_body(
        argv,
        identity,
        recorder,
        None,
        env,
        timeout,
        sockets_root,
        gateway,
        workservice,
        ca_cert_path,
        root="research",
    )
