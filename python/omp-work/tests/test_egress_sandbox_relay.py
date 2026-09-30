"""Tests for the egress sandbox relay (OMP-431-s07).

The sandbox is driven exactly as in ``test_egress_sandbox.py`` (same unshare
probe), but with a real :class:`EgressGateway` and :class:`GatewayCA` bound in:
the worker reaches the network only through loopback listeners the helper
exposes, so a model call, an HTTPS registry GET, and a dumb-http ``git clone``
succeed with no standing policy while a push, an unnamed remote, and an unnamed
host are refused and recorded. Upstreams are 127.0.0.1 stubs reached through
the gateway's injected resolver and connector, so a refusal is proven by the
stub being untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
from datetime import datetime, timezone

import pytest

from omp_work.egress_fetch import ResearchFetchService
from omp_work.egress_gateway import EgressGateway
from omp_work.egress_policy import (
    EgressPolicy,
    Identity,
    MemoryRecorder,
    ProjectEgress,
    build_policy,
)
from omp_work.egress_sandbox import run_sandboxed
from omp_work.egress_tls import GatewayCA

GLOBAL = "93.184.216.34"
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
TIMEOUT = 150.0
MODEL_PROXY = "proxy.test:8443"
MODEL_UPSTREAM = ("upstream.test", 9443)
REGISTRY_HOST = "reg.test"
GIT_HOST = "git.test"
GIT_PATH = "/org/repo.git"


def _sandbox_available() -> bool:
    if shutil.which("openssl") is None:
        return False
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--map-root-user", "--net", "--mount", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=5,
        )
        return probe.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _sandbox_available(), reason="unshare probe failed or openssl missing"
)


# -- stubs ----------------------------------------------------------------


def http_response(status: int, reason: str, headers: list[tuple[str, str]], body: bytes) -> bytes:
    pairs = list(headers)
    if not any(name.lower() == "content-length" for name, _ in pairs):
        pairs.append(("Content-Length", str(len(body))))
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines.extend(f"{name}: {value}" for name, value in pairs)
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def _read_http_request(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    head, sep, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip() or b"0")
    while len(rest) < length:
        chunk = conn.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + sep + rest[:length]


class SimpleHttpStub:
    """Threaded 127.0.0.1 HTTP server. Records requests, replies with one body."""

    def __init__(self, body: bytes, status: int = 200, reason: str = "OK") -> None:
        self.body = body
        self.status = status
        self.reason = reason
        self.requests: list[bytes] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, name="http-stub", daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                conn.settimeout(20.0)
                self.requests.append(_read_http_request(conn))
                conn.sendall(http_response(self.status, self.reason, [], self.body))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class TlsHttpStub:
    """Threaded 127.0.0.1 HTTPS server for the inspected registry leg."""

    def __init__(self, cert: str, key: str, body: bytes) -> None:
        self.body = body
        self.requests: list[bytes] = []
        self.snis: list[str | None] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(cert, key)

        def sni_cb(_sock: ssl.SSLSocket, name: str | None, _ctx: ssl.SSLContext) -> None:
            self.snis.append(name)

        self._ctx.sni_callback = sni_cb
        threading.Thread(target=self._serve, name="tls-stub", daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                raw.settimeout(20.0)
                conn = self._ctx.wrap_socket(raw, server_side=True)
                self.requests.append(_read_http_request(conn))
                conn.sendall(http_response(200, "OK", [], self.body))
                conn.close()
            except Exception:
                pass
            finally:
                try:
                    raw.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class DumbHttpStub:
    """Serves a git repository in dumb-http form on 127.0.0.1. No ``?`` queries."""

    def __init__(self, root: Path, prefix: str) -> None:
        self.root = root
        self.prefix = prefix
        self.requests: list[str] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, name="git-stub", daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            try:
                conn.settimeout(20.0)
                raw = _read_http_request(conn)
                request_line = raw.split(b"\r\n", 1)[0].decode("iso-8859-1")
                self.requests.append(request_line)
                target = request_line.split(" ")[1] if " " in request_line else ""
                # A plain file server ignores the query string, so git's smart
                # probe for info/refs sees a text/plain file and falls back to
                # dumb http.
                path = target.split("?", 1)[0]
                rel = path[len(self.prefix) :] if path.startswith(self.prefix) else None
                file = self.root / rel.lstrip("/") if rel is not None else None
                if file is None or not file.is_file():
                    conn.sendall(http_response(404, "Not Found", [], b"not found"))
                else:
                    conn.sendall(
                        http_response(200, "OK", [("Content-Type", "text/plain")], file.read_bytes())
                    )
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class TokenStub:
    """Threaded 127.0.0.1 TCP server that greets each connection with a token."""

    def __init__(self, token: bytes) -> None:
        self.token = token
        self.connections = 0
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, name="token-stub", daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            try:
                conn.sendall(self.token)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class MapResolver:
    """Returns the fake global address for every name."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        return [GLOBAL]


class PinConnector:
    """Maps the fake global address and port onto a local stub."""

    def __init__(self) -> None:
        self.by_port: dict[int, object] = {}
        self.calls: list[tuple[str, int, float]] = []

    def __call__(self, ip: str, port: int, timeout: float) -> socket.socket:
        self.calls.append((ip, port, timeout))
        stub = self.by_port.get(port)
        if ip != GLOBAL or stub is None:
            raise OSError(f"no stub for {ip}:{port}")
        return socket.create_connection(("127.0.0.1", stub.port), timeout)


def write_cert(tmp: Path, hostname: str, name: str = "") -> tuple[Path, Path, Path]:
    """Return ``(ca cert, leaf cert, leaf key)`` for a test-owned upstream CA."""
    tmp.mkdir(parents=True, exist_ok=True)
    prefix = tmp / name if name else tmp
    ca_key = Path(f"{prefix}-ca.key")
    ca_crt = Path(f"{prefix}-ca.crt")
    leaf_key = Path(f"{prefix}-leaf.key")
    leaf_csr = Path(f"{prefix}-leaf.csr")
    leaf_crt = Path(f"{prefix}-leaf.crt")
    ext = Path(f"{prefix}-leaf.ext")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(ca_key), "-out", str(ca_crt),
            "-days", "2", "-subj", "/CN=Upstream CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-nodes", "-keyout", str(leaf_key), "-out", str(leaf_csr),
            "-subj", f"/CN={hostname}",
        ],
        check=True,
        capture_output=True,
    )
    ext.write_text(
        f"subjectAltName=DNS:{hostname}\n"
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n",
        encoding="ascii",
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(leaf_csr),
            "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(leaf_crt), "-days", "2", "-extfile", str(ext),
        ],
        check=True,
        capture_output=True,
    )
    return ca_crt, leaf_crt, leaf_key


def make_git_repo(base: Path) -> Path:
    """Create a committed repo and return the ``.git`` directory a dumb server serves."""
    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "README").write_text("hello\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=work, check=True, env=env)
    subprocess.run(["git", "add", "."], cwd=work, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=work, check=True, env=env)
    subprocess.run(["git", "update-server-info"], cwd=work, check=True, env=env)
    return work / ".git"


# -- harness --------------------------------------------------------------


class Harness:
    """A gateway with stubs, a GatewayCA, and a socket root for one sandbox."""

    def __init__(self, tmp_path: Path, registries=(), remotes=(), upstream_ca_bundle=None) -> None:
        self.tmp_path = tmp_path
        self.identity = Identity(
            workspace_id="ws-relay",
            project_id="proj-relay",
            mission_id="mission-relay",
            worker_id="worker-relay",
            stage="repository",
        )
        self.policy: EgressPolicy = build_policy(
            ProjectEgress(
                registries=tuple(registries), remotes=tuple(remotes), decision_id="dec-egress"
            ),
            (),
            MODEL_PROXY,
        )
        self.recorder = MemoryRecorder()
        self.resolver = MapResolver()
        self.connector = PinConnector()
        self.ca = GatewayCA(tmp_path / "tls")
        self.fetch = ResearchFetchService(self.recorder, self.resolver, self.connector, None, 30.0)
        self.gateway = EgressGateway(
            policy_source=lambda identity, now: self.policy,
            recorder=self.recorder,
            fetch=self.fetch,
            resolver=self.resolver,
            connector=self.connector,
            model_proxy_upstream=MODEL_UPSTREAM,
            clock=lambda: NOW,
            tls=self.ca,
            upstream_ca_bundle=str(upstream_ca_bundle) if upstream_ca_bundle is not None else None,
        )
        self.sockets_root = tmp_path / "sockroot"
        self.sockets_root.mkdir(parents=True, exist_ok=True)
        self.home = tmp_path / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        gitconfig = tmp_path / "gitconfig"
        gitconfig.write_text("[safe]\n\tdirectory = *\n", encoding="utf-8")
        self.gitconfig = gitconfig
        self.stubs: list[object] = []
        self._result_seq = 0

    def worker_env(self) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.home), "GIT_CONFIG_GLOBAL": str(self.gitconfig)}

    def run_code(self, code: str, *, workservice: tuple[str, int] | None = None) -> int:
        """Run worker code in the sandbox, with ``__RESULT__`` bound to a JSON path."""
        result_path = self.tmp_path / f"result-{self._result_seq}.json"
        self._result_seq += 1
        source = code.replace("__RESULT__", str(result_path))
        rc = run_sandboxed(
            ["python3", "-c", source],
            self.identity,
            self.recorder,
            str(self.home),
            self.worker_env(),
            TIMEOUT,
            sockets_root=self.sockets_root,
            gateway=self.gateway,
            workservice=workservice,
            ca_cert_path=self.ca.ca_cert_path,
        )
        assert rc == 0, f"worker exited {rc}"
        return result_path

    def close(self) -> None:
        for stub in self.stubs:
            stub.close()


def read_result(result_path: Path) -> dict:
    return json.loads(result_path.read_text())


def records(recorder: MemoryRecorder, channel: str | None = None):
    if channel is None:
        return list(recorder.records)
    return [rec for rec in recorder.records if rec.channel == channel]


# -- tier 1 ---------------------------------------------------------------


def test_model_registry_and_dumb_clone_succeed(tmp_path: Path) -> None:
    upstream_ca, leaf, leaf_key = write_cert(tmp_path / "up", REGISTRY_HOST)
    harness = Harness(
        tmp_path,
        registries=(f"https://{REGISTRY_HOST}",),
        remotes=(f"http://{GIT_HOST}{GIT_PATH}",),
        upstream_ca_bundle=upstream_ca,
    )
    model_stub = SimpleHttpStub(b'{"ok":true}')
    git_stub = DumbHttpStub(make_git_repo(tmp_path / "repo"), GIT_PATH)
    registry_stub = TlsHttpStub(str(leaf), str(leaf_key), b"pkg")
    harness.connector.by_port = {9443: model_stub, 80: git_stub, 443: registry_stub}
    harness.stubs += [model_stub, git_stub, registry_stub]
    try:
        result = harness.run_code(
            r'''
import json, os, subprocess, urllib.request
out = {}
out["proxy_env"] = {k: os.environ.get(k) for k in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy")}
out["ca_env"] = {k: os.environ.get(k) for k in (
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO")}
req = urllib.request.Request("http://proxy.test:8443/v1/chat", data=b"{}", method="POST")
with urllib.request.urlopen(req, timeout=40) as r:
    out["model"] = r.read().decode()
with urllib.request.urlopen("https://reg.test/v2/lib", timeout=40) as r:
    out["registry"] = r.read().decode()
clone = subprocess.run(
    ["git", "clone", "http://git.test/org/repo.git", os.path.join(os.environ["HOME"], "clone")],
    capture_output=True, text=True, timeout=120, cwd=os.environ["HOME"],
)
out["clone_rc"] = clone.returncode
out["clone_err"] = clone.stderr[-500:]
with open("__RESULT__", "w") as f:
    json.dump(out, f)
''',
        )
        out = read_result(result)
        assert out["model"] == '{"ok":true}'
        assert out["registry"] == "pkg"
        assert out["clone_rc"] == 0, out["clone_err"]
        assert (harness.home / "clone" / "README").is_file()
        assert registry_stub.snis == [REGISTRY_HOST]
        assert set(out["proxy_env"].values()) <= {"http://127.0.0.1:3128", ""}
        assert out["proxy_env"]["HTTP_PROXY"] == "http://127.0.0.1:3128"
        assert out["proxy_env"]["NO_PROXY"] == ""
        assert set(out["ca_env"].values()) == {str(harness.ca.ca_cert_path)}
        http_records = records(harness.recorder, "http")
        assert http_records and all(rec.outcome == "allowed" for rec in http_records)
        assert {"model", "registry", "remote"} <= {rec.klass for rec in http_records}
    finally:
        harness.close()


def test_push_and_unnamed_remote_refused(tmp_path: Path) -> None:
    harness = Harness(tmp_path, remotes=(f"http://{GIT_HOST}{GIT_PATH}",))
    git_stub = DumbHttpStub(make_git_repo(tmp_path / "repo"), GIT_PATH)
    harness.connector.by_port = {80: git_stub}
    harness.stubs.append(git_stub)
    try:
        result = harness.run_code(
            r'''
import json, os, subprocess
out = {}
home = os.environ["HOME"]
clone = subprocess.run(["git", "clone", "http://git.test/org/repo.git", os.path.join(home, "clone")],
                       capture_output=True, text=True, timeout=120, cwd=home)
out["clone_rc"] = clone.returncode
cwd = os.path.join(home, "clone")
push = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/x"],
                      capture_output=True, text=True, timeout=90, cwd=cwd)
out["push_rc"] = push.returncode
subprocess.run(["git", "remote", "add", "evil", "http://evil.test/r.git"],
               capture_output=True, text=True, timeout=30, cwd=cwd)
fetch = subprocess.run(["git", "fetch", "evil"], capture_output=True, text=True, timeout=90, cwd=cwd)
out["evil_rc"] = fetch.returncode
with open("__RESULT__", "w") as f:
    json.dump(out, f)
''',
        )
        out = read_result(result)
        assert out["clone_rc"] == 0
        assert out["push_rc"] != 0
        assert out["evil_rc"] != 0
        http_records = records(harness.recorder, "http")
        assert any(
            rec.code == "git_receive_pack" and rec.outcome == "refused" and rec.host == GIT_HOST
            for rec in http_records
        )
        assert any(
            rec.code == "destination_not_allowed" and rec.host == "evil.test" for rec in http_records
        )
    finally:
        harness.close()


# -- names ----------------------------------------------------------------


def test_names_resolve_only_through_policy(tmp_path: Path) -> None:
    harness = Harness(tmp_path, registries=(f"https://{REGISTRY_HOST}",))
    try:
        result = harness.run_code(
            r'''
import json, subprocess
out = {}
reg = subprocess.run(["getent", "hosts", "reg.test"], capture_output=True, text=True, timeout=30)
out["reg_rc"] = reg.returncode
out["reg_out"] = reg.stdout.strip()
evil = subprocess.run(["getent", "hosts", "evil.test"], capture_output=True, text=True, timeout=30)
out["evil_rc"] = evil.returncode
with open("__RESULT__", "w") as f:
    json.dump(out, f)
''',
        )
        out = read_result(result)
        assert out["reg_rc"] == 0
        assert GLOBAL in out["reg_out"]
        assert out["evil_rc"] != 0
        dns_records = records(harness.recorder, "dns")
        assert any(rec.host == "evil.test" and rec.code == "name_not_allowed" for rec in dns_records)
        assert all(rec.host != REGISTRY_HOST for rec in dns_records)
    finally:
        harness.close()


def test_without_gateway_names_fail(tmp_path: Path) -> None:
    harness = Harness(tmp_path, registries=(f"https://{REGISTRY_HOST}",))
    result_path = tmp_path / "result-nogw.json"
    try:
        rc = run_sandboxed(
            [
                "python3",
                "-c",
                (
                    "import json, socket\n"
                    "out = {}\n"
                    "try:\n"
                    "    socket.getaddrinfo('reg.test', 80)\n"
                    "except socket.gaierror:\n"
                    "    out['reg'] = 'failed'\n"
                    "else:\n"
                    "    out['reg'] = 'resolved'\n"
                    f"with open({str(result_path)!r}, 'w') as f:\n"
                    "    json.dump(out, f)\n"
                ),
            ],
            harness.identity,
            harness.recorder,
            str(harness.home),
            harness.worker_env(),
            TIMEOUT,
            sockets_root=harness.sockets_root,
        )
        assert rc == 0
        assert json.loads(result_path.read_text())["reg"] == "failed"
    finally:
        harness.close()


# -- WorkService and isolation --------------------------------------------


def test_workservice_reachable_other_sandboxes_hidden(tmp_path: Path) -> None:
    harness = Harness(tmp_path, registries=(f"https://{REGISTRY_HOST}",))
    workservice_stub = TokenStub(b"WORKSERVICE-OK\n")
    host_listener = TokenStub(b"HOST-LISTENER\n")
    other = harness.sockets_root / "sb-other"
    other.mkdir(parents=True, exist_ok=True)
    (other / "proxy.sock").write_text("other sandbox", encoding="utf-8")
    harness.stubs += [workservice_stub, host_listener]
    try:
        code = r'''
import glob, json, os, socket
out = {}
s = socket.create_connection(("127.0.0.1", __WS_PORT__), timeout=10)
out["workservice"] = s.recv(64).decode(errors="replace")
s.close()
try:
    h = socket.create_connection(("127.0.0.1", __HOST_PORT__), timeout=3)
    h.close()
    out["host_listener"] = "connected"
except OSError:
    out["host_listener"] = "refused"
out["sockroot_entries"] = sorted(os.path.basename(p) for p in glob.glob("__SOCKROOT__/*"))
try:
    with open(os.path.join("__SOCKROOT__", "sb-other", "proxy.sock"), "rb") as f:
        out["other_sock"] = f.read().decode(errors="replace")
except OSError:
    out["other_sock"] = "unreachable"
with open("__RESULT__", "w") as f:
    json.dump(out, f)
'''
        code = (
            code.replace("__WS_PORT__", str(workservice_stub.port))
            .replace("__HOST_PORT__", str(host_listener.port))
            .replace("__SOCKROOT__", str(harness.sockets_root))
        )
        result = harness.run_code(code, workservice=("127.0.0.1", workservice_stub.port))
        out = read_result(result)
        assert out["workservice"] == "WORKSERVICE-OK\n"
        assert out["host_listener"] == "refused"
        assert out["other_sock"] == "unreachable"
        assert "sb-other" not in out["sockroot_entries"]
        assert workservice_stub.connections >= 1
        assert host_listener.connections == 0
    finally:
        harness.close()


def test_raw_records_still_arrive_with_relays(tmp_path: Path) -> None:
    harness = Harness(tmp_path, registries=(f"https://{REGISTRY_HOST}",))
    try:
        result = harness.run_code(
            r'''
import json, socket
out = {}
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(2.0)
try:
    s.connect(("93.184.216.34", 443))
    out["tcp"] = "connected"
except OSError:
    out["tcp"] = "refused"
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
u.sendto(b"probe", ("8.8.8.8", 53))
out["udp"] = "sent"
with open("__RESULT__", "w") as f:
    json.dump(out, f)
''',
        )
        out = read_result(result)
        assert out["tcp"] == "refused"
        raw = records(harness.recorder, "raw")
        assert any(rec.protocol == "tcp" and rec.ip == "93.184.216.34" for rec in raw)
        assert any(rec.protocol == "udp" and rec.ip == "8.8.8.8" for rec in raw)
    finally:
        harness.close()
