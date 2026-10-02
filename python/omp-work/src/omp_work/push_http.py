"""HTTP client for sending operational alerts and digests (OMP-406, OMP-415)."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["PushDeliveryError", "request_json", "send"]


class PushDeliveryError(Exception):
    """Raised when delivering an alert or digest over HTTP fails."""

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


class _DeniedFileAndData(urllib.request.FileHandler, urllib.request.DataHandler):
    """Stand-ins so the opener rejects file: and data: instead of fetching them."""

    def file_open(self, request: urllib.request.Request) -> None:
        raise urllib.error.URLError(f"file URLs are not permitted: {request.full_url}")

    def data_open(self, request: urllib.request.Request) -> None:
        raise urllib.error.URLError(f"data URLs are not permitted: {request.full_url}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is a failed delivery.

    The default handler follows Location and can replay the POST, bearer, and
    signature to a different URL. A redirect is an error, same as a non-2xx.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


# Same handlers as urlopen, except file: and data: cannot be fetched and a
# redirect is not followed.
_OPENER = urllib.request.build_opener(_DeniedFileAndData, _NoRedirect)


def send(
    url: str,
    token: str | None,
    idempotency_key: str,
    body: Any,
    *,
    timeout: float = 10,
    headers: dict[str, str] | None = None,
) -> None:
    """POST JSON body with authentication and idempotency key.

    URL must be https, or http to loopback; otherwise ValueError is raised
    before connecting. The opener rejects file: and data: schemes and does not
    follow redirects (a 3xx is PushDeliveryError). Non-2xx and network errors raise
    PushDeliveryError (status or reason, never the token).

    ``token=None`` sends no Authorization header (OMP-415 signed pushes carry
    no bearer). ``headers`` are extra request headers, merged without letting
    them displace the bearer/idempotency/content-type the caller relies on.
    """
    _validate_url(url)

    if isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    elif isinstance(body, str):
        data = body.encode("utf-8")
    else:
        data = json.dumps(body).encode("utf-8")

    request_headers = dict(headers or {})
    request_headers["Idempotency-Key"] = str(idempotency_key)
    request_headers["Content-Type"] = "application/json"
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"

    # Redaction source: the configured token when there is one, plus any
    # caller-supplied header values (a signing secret must never leak into an
    # error message).
    secrets_to_hide = [value for value in (token, *((headers or {}).values())) if value]

    req = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method="POST",
    )

    def hide(text: str) -> str:
        for secret in secrets_to_hide:
            text = _sanitize(text, secret)
        return text

    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            if not (200 <= status < 300):
                raise PushDeliveryError(
                    f"HTTP {status}",
                    status_code=status,
                )
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        reason = str(exc.reason)
        sanitized_reason = hide(reason)
        msg = f"HTTP {status_code}: {sanitized_reason}"
        raise PushDeliveryError(
            msg,
            status_code=status_code,
            reason=sanitized_reason,
        ) from None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        sanitized_reason = hide(reason)
        msg = f"Network error: {sanitized_reason}"
        raise PushDeliveryError(msg, reason=sanitized_reason) from None
    except (TimeoutError, OSError) as exc:
        sanitized_msg = hide(str(exc))
        msg = f"Network error: {sanitized_msg}"
        raise PushDeliveryError(msg, reason=sanitized_msg) from None


_ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH"})

_IS_LEGAL_HEADER_NAME = getattr(
    http.client,
    "_is_legal_header_name",
    re.compile(rb"[^:\s][^:\r\n]*").fullmatch,
)
_IS_ILLEGAL_HEADER_VALUE = getattr(
    http.client,
    "_is_illegal_header_value",
    re.compile(rb"\n(?![ \t])|\r(?![ \t\n])").search,
)


def _validate_header(name: Any, value: Any) -> None:
    if not isinstance(name, str) or "\0" in name or "\r" in name or "\n" in name:
        raise ValueError("Invalid header name")
    if not isinstance(value, str) or "\0" in value or "\r" in value or "\n" in value:
        raise ValueError("Invalid header value")
    try:
        raw_name = name.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("Invalid header name") from None
    if not _IS_LEGAL_HEADER_NAME(raw_name):
        raise ValueError("Invalid header name")
    try:
        raw_value = value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError("Invalid header value") from None
    if _IS_ILLEGAL_HEADER_VALUE(raw_value):
        raise ValueError("Invalid header value")


def request_json(
    method: str,
    url: str,
    token: str,
    body: Any = None,
    *,
    timeout: float = 10,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    """Perform an HTTP request returning (status, parsed_json) or (status, None).

    Allowed methods are GET, POST, PUT, PATCH. Any other method raises ValueError
    before connecting. Non-loopback HTTP and invalid schemes raise ValueError.
    Headers and token are strictly validated before connecting; invalid header names
    or values raise ValueError without naming the header or value in the error.
    Extra caller headers never displace Authorization or Content-Type (case-insensitive).
    Content-Type is sent only when body is not None.
    Non-2xx responses raise PushDeliveryError with status_code.
    Network errors, dropped connections, and bad 2xx JSON raise PushDeliveryError
    with status_code=None.
    Error text and reasons redact the token and any caller header values.
    """
    if method not in _ALLOWED_METHODS:
        raise ValueError(
            f"Method must be one of {sorted(_ALLOWED_METHODS)}, got {method!r}"
        )

    if not isinstance(token, str) or not token:
        raise ValueError("Token must be a non-empty string")
    try:
        _validate_header("Authorization", f"Bearer {token}")
    except ValueError:
        raise ValueError("Invalid token") from None

    _validate_url(url)

    data: bytes | None
    if body is None:
        data = None
    else:
        data = json.dumps(body).encode("utf-8")

    request_headers: dict[str, str] = {}
    if headers is not None:
        if not isinstance(headers, dict):
            raise ValueError("headers must be a dict")
        for k, v in headers.items():
            _validate_header(k, v)
            if k.lower() not in {"authorization", "content-type"}:
                request_headers[k] = v

    request_headers["Authorization"] = f"Bearer {token}"
    if data is not None:
        request_headers["Content-Type"] = "application/json"

    secrets_to_hide = sorted(
        [value for value in (token, *((headers or {}).values())) if value],
        key=len,
        reverse=True,
    )

    def hide(text: str) -> str:
        for secret in secrets_to_hide:
            text = _sanitize(text, secret)
        return text

    req = urllib.request.Request(
        url,
        data=data,
        headers=request_headers,
        method=method,
    )

    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            if not (200 <= status < 300):
                msg = getattr(resp, "msg", getattr(resp, "reason", ""))
                sanitized_reason = hide(str(msg)) if msg else None
                err_msg = (
                    f"HTTP {status}: {sanitized_reason}"
                    if sanitized_reason
                    else f"HTTP {status}"
                )
                raise PushDeliveryError(
                    err_msg,
                    status_code=status,
                    reason=sanitized_reason,
                )
            raw = resp.read()
            if not raw:
                return status, None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                sanitized_msg = hide(str(exc))
                raise PushDeliveryError(
                    f"Invalid JSON response: {sanitized_msg}",
                    status_code=None,
                    reason=sanitized_msg,
                ) from None
            return status, payload
    except urllib.error.HTTPError as exc:
        status_code = exc.code
        reason = str(exc.reason)
        sanitized_reason = hide(reason)
        msg = f"HTTP {status_code}: {sanitized_reason}"
        raise PushDeliveryError(
            msg,
            status_code=status_code,
            reason=sanitized_reason,
        ) from None
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        sanitized_reason = hide(reason)
        msg = f"Network error: {sanitized_reason}"
        raise PushDeliveryError(
            msg,
            status_code=None,
            reason=sanitized_reason,
        ) from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        sanitized_msg = hide(str(exc))
        msg = f"Network error: {sanitized_msg}"
        raise PushDeliveryError(
            msg,
            status_code=None,
            reason=sanitized_msg,
        ) from None
