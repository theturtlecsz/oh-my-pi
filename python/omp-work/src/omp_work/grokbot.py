"""Client for sending operational alerts and digests to Grokbot (OMP-406)."""

from __future__ import annotations

import ipaddress
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["GrokbotError", "send"]


class GrokbotError(Exception):
    """Raised when sending an alert or digest to Grokbot fails."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback
    except ValueError:
        return False


def _validate_url(url: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception as exc:
        raise ValueError(f"Invalid URL: {url!r}") from exc

    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Invalid or missing scheme/host in URL: {url!r}")

    if parsed.scheme == "https":
        return

    if parsed.scheme == "http":
        if _is_loopback(parsed.hostname):
            return
        raise ValueError(
            f"http scheme only allowed for loopback hosts, got {parsed.hostname!r}"
        )

    raise ValueError(
        f"Unsupported scheme {parsed.scheme!r}; only https or loopback http allowed"
    )


def _sanitize(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "[REDACTED]")
    return text


def send(
    url: str,
    token: str,
    idempotency_key: str,
    body: Any,
    *,
    timeout: float = 10,
) -> None:
    """POST JSON body to Grokbot with authentication and idempotency key.

    URL must be https, or http to loopback; otherwise ValueError is raised
    before connecting. Non-2xx and network errors raise GrokbotError (status
    or reason, never the token).
    """
    _validate_url(url)

    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": str(idempotency_key),
        "Content-Type": "application/json",
    }

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            if not (200 <= status < 300):
                raise GrokbotError(
                    f"HTTP {status}",
                    status_code=status,
                )
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        reason = str(exc.reason)
        sanitized_reason = _sanitize(reason, token)
        msg = f"HTTP {status_code}: {sanitized_reason}"
        raise GrokbotError(
            msg,
            status_code=status_code,
            reason=sanitized_reason,
        ) from None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        sanitized_reason = _sanitize(reason, token)
        msg = f"Network error: {sanitized_reason}"
        raise GrokbotError(msg, reason=sanitized_reason) from None
    except (TimeoutError, OSError) as exc:
        sanitized_msg = _sanitize(str(exc), token)
        msg = f"Network error: {sanitized_msg}"
        raise GrokbotError(msg, reason=sanitized_msg) from None
