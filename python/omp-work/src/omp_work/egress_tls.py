"""Gateway certificate authority for TLS-inspected CONNECT tunnels (OMP-431).

:class:`GatewayCA` owns one certificate authority under a state directory: a
private key written ``0600`` and a self-signed certificate, created once by
shelling out to ``openssl`` (the same fixed-argv style as
:mod:`omp_work.v1.owner_signature`). Only the certificate path is ever handed
to a sandbox; the key stays with the gateway.

:meth:`GatewayCA.leaf_context` returns a server :class:`ssl.SSLContext` for a
CA-signed leaf whose subject alternative name is the requested host, minted on
first use and cached per host. A client that trusts only
:attr:`GatewayCA.ca_cert_path` verifies the gateway's leaf, and a host the CA
did not sign fails verification.

When ``openssl`` is absent, constructing :class:`GatewayCA` raises
:class:`TlsUnavailable` and the caller keeps ``tls=None`` (fail closed).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import ssl
import subprocess  # nosec B404 - openssl only, resolved via shutil.which, fixed argv, no shell
import threading
from pathlib import Path

__all__ = ["GatewayCA", "TlsUnavailable"]

_OPENSSL_TIMEOUT = 30.0
_CA_DAYS = "365"
_LEAF_DAYS = "30"
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.-_")


class TlsUnavailable(RuntimeError):
    """Raised when ``openssl`` is missing or a call fails, so TLS inspection cannot run."""


def _openssl() -> str:
    path = shutil.which("openssl")
    if path is None:
        raise TlsUnavailable("openssl not found on PATH")
    return path


def _san_entry(host: str) -> str:
    """A ``DNS:`` or ``IP:`` subjectAltName entry for ``host``."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return f"DNS:{host}"
    return f"IP:{host}"


def _safe_name(host: str) -> str:
    """A filesystem-safe leaf directory name derived from ``host``."""
    cleaned = "".join(ch if ch in _SAFE_CHARS else "_" for ch in host.lower())
    if cleaned in ("", ".", ".."):
        return "host"
    return cleaned


class GatewayCA:
    """One CA under a state directory. Mints and caches CA-signed leaf contexts."""

    def __init__(self, state_dir: str | os.PathLike[str]) -> None:
        self._openssl = _openssl()
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        self._ca_cert = self._dir / "ca.crt"
        self._ca_key = self._dir / "ca.key"
        self._leaves = self._dir / "leaves"
        self._contexts: dict[str, ssl.SSLContext] = {}
        self._lock = threading.Lock()
        if not (self._ca_cert.is_file() and self._ca_key.is_file()):
            self._create_ca()

    @property
    def ca_cert_path(self) -> Path:
        """The CA certificate. This is the only file a sandbox is given."""
        return self._ca_cert

    def leaf_context(self, host: str) -> ssl.SSLContext:
        """Return a cached server context for a CA-signed leaf naming ``host``."""
        with self._lock:
            cached = self._contexts.get(host)
            if cached is not None:
                return cached
            context = self._mint(host)
            self._contexts[host] = context
            return context

    def _create_ca(self) -> None:
        self._run(
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(self._ca_key),
            "-out",
            str(self._ca_cert),
            "-days",
            _CA_DAYS,
            "-subj",
            "/CN=OMP Egress Gateway CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
        )
        os.chmod(self._ca_key, 0o600)
        os.chmod(self._ca_cert, 0o644)

    def _mint(self, host: str) -> ssl.SSLContext:
        leaf = self._leaves / _safe_name(host)
        leaf.mkdir(parents=True, exist_ok=True)
        key = leaf / "leaf.key"
        csr = leaf / "leaf.csr"
        ext = leaf / "leaf.ext"
        cert = leaf / "leaf.crt"
        self._run(
            "req",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(csr),
            "-subj",
            f"/CN={host}",
        )
        ext.write_text(
            f"subjectAltName={_san_entry(host)}\n"
            "basicConstraints=CA:FALSE\n"
            "keyUsage=digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n",
            encoding="ascii",
        )
        self._run(
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(self._ca_cert),
            "-CAkey",
            str(self._ca_key),
            "-CAcreateserial",
            "-out",
            str(cert),
            "-days",
            _LEAF_DAYS,
            "-extfile",
            str(ext),
        )
        os.chmod(key, 0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context

    def _run(self, *args: str) -> None:
        try:
            completed = subprocess.run(  # nosec B603 - absolute openssl from shutil.which, fixed argv, no shell, no user input
                [self._openssl, *args],
                capture_output=True,
                timeout=_OPENSSL_TIMEOUT,
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TlsUnavailable(str(exc)) from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
            raise TlsUnavailable(detail or f"openssl exited {completed.returncode}")
