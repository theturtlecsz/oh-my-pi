"""Push-destination allowlist and refusal reasons (OMP-415).

``load_allowed_hosts`` reads ``<config_dir>/push-destinations.json``. A missing
or unusable file allows no host. ``check_destination`` refuses a non-https URL,
userinfo, a host outside that set, a name that does not resolve, and any
resolved address ``egress_policy.blocked_address`` refuses.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from .egress_policy import blocked_address

__all__ = ["check_destination", "load_allowed_hosts"]


def _host_key(host: str) -> str:
    text = host.strip().lower().rstrip(".")
    if text.startswith("[") and text.endswith("]") and len(text) > 2:
        text = text[1:-1].strip().lower().rstrip(".")
    return text


def load_allowed_hosts(config_dir: Path | str) -> frozenset[str]:
    """Lower-cased allowlist from ``push-destinations.json``, or empty."""
    path = Path(config_dir) / "push-destinations.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return frozenset()
    if not isinstance(raw, dict):
        return frozenset()
    hosts = raw.get("allowed_hosts")
    if not isinstance(hosts, list):
        return frozenset()
    allowed: set[str] = set()
    for host in hosts:
        if not isinstance(host, str):
            continue
        key = _host_key(host)
        if key:
            allowed.add(key)
    return frozenset(allowed)


def _getaddrinfo_ips(host: str) -> tuple[str, ...]:
    """IP strings ``socket.getaddrinfo`` returns for ``host``, duplicates dropped."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]
        if ip not in ips:
            ips.append(ip)
    return tuple(ips)


def _extract_scheme(url: str) -> str:
    i = url.find(":")
    if i > 0 and url[0].isascii() and url[0].isalpha():
        candidate = url[:i]
        if all(c.isalnum() or c in "+-." for c in candidate):
            return candidate.lower()
    return ""


def check_destination(
    url: str,
    *,
    allowed_hosts: Iterable[str],
    resolve: Callable[[str], Iterable[str]] = _getaddrinfo_ips,
) -> str | None:
    """Return a refusal reason, or None when the URL may be registered.

    Reasons: ``scheme`` (not https), ``userinfo``, ``host_not_allowed``,
    ``unresolvable``, ``blocked:<reason>`` from ``blocked_address`` on any
    resolved IP.
    """
    raw = url.strip()
    if _extract_scheme(raw) != "https":
        return "scheme"

    has_userinfo = False
    host = ""
    try:
        parts = urlsplit(raw)
        has_userinfo = (
            parts.username is not None
            or parts.password is not None
            or "@" in parts.netloc
        )
        if not has_userinfo:
            _ = parts.port
            host = _host_key(parts.hostname or "")
    except ValueError:
        netloc = raw.split("://", 1)[1] if "://" in raw else ""
        for sep in ("/", "?", "#"):
            netloc = netloc.split(sep, 1)[0]
        if "@" in netloc:
            has_userinfo = True

    if has_userinfo:
        return "userinfo"
    allowed = {_host_key(item) for item in allowed_hosts if _host_key(item)}
    if not host or host not in allowed:
        return "host_not_allowed"
    try:
        ips = tuple(resolve(host))
    except (OSError, UnicodeError):
        return "unresolvable"
    if not ips:
        return "unresolvable"
    for ip in ips:
        reason = blocked_address(str(ip).split("%", 1)[0])
        if reason is not None:
            return f"blocked:{reason}"
    return None
