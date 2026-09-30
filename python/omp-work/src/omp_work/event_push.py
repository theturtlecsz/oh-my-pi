"""Push-destination allowlist, refusal reasons, and the signed event push runner
(OMP-415).

``load_allowed_hosts`` reads ``<config_dir>/push-destinations.json``. A missing
or unusable file allows no host. ``check_destination`` refuses a non-https URL,
userinfo, a host outside that set, a name that does not resolve, and any
resolved address ``egress_policy.blocked_address`` refuses.

``run_push`` walks every client's subscriptions. A row with no push_url is a
pull subscription and is skipped, as is an ops.* stream. A push_url that
``check_destination`` refuses returns ``{"refused": reason}`` and nothing is
sent. Otherwise it pages mission events from that subscription's cursor,
sending the subscribed types and advancing the cursor only past what was
delivered. Delivery is a signed, bearer-less POST (``send_signed``) whose
Idempotency-Key is the mission_event_id. A redirect is a failed delivery:
the POST is not replayed to the Location.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import socket
import stat
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from .egress_policy import blocked_address
from .grokbot import GrokbotError
from .grokbot import send as grokbot_send
from .v1.models import (
    AdvanceEventCursor,
    AdvanceEventCursorCommand,
    CommandEnvelope,
)

__all__ = [
    "check_destination",
    "load_allowed_hosts",
    "load_master_key",
    "run_push",
    "send_signed",
    "signature",
    "subscription_key",
    "verify",
]

_SIGNATURE_HEADER = "X-OMP-Signature"
_SUBSCRIPTION_INFO = b"omp-push-subscription\0"


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


def body_bytes(body: Any) -> bytes:
    """Canonical push body: compact, sorted-key JSON, UTF-8 encoded.

    The bytes the signature covers are exactly the bytes sent, so both sides
    recompute them the same way.
    """
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def signature(key: bytes, idempotency_key: str, body: Any) -> str:
    """``v1=<hex HMAC-SHA256(key, idempotency_key + "\\n" + body)>``."""
    canonical = body_bytes(body)
    message = idempotency_key.encode("utf-8") + b"\n" + canonical
    digest = hmac.new(key, message, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def verify(key: bytes, idempotency_key: str, body: Any, header: str | None) -> bool:
    """Constant-time check of ``X-OMP-Signature`` against ``signature``.

    A missing or non-ASCII header is a mismatch. ``compare_digest`` raises
    TypeError on a non-ASCII ``str``, so both sides are compared as ASCII bytes.
    """
    if not isinstance(header, str):
        return False
    try:
        presented = header.encode("ascii")
    except UnicodeEncodeError:
        return False
    expected = signature(key, idempotency_key, body).encode("ascii")
    return hmac.compare_digest(presented, expected)


def subscription_key(master_key: bytes, subscription_id: UUID | str) -> bytes:
    """Per-subscription key: HMAC-SHA256(master, "omp-push-subscription\\0" + id)."""
    message = _SUBSCRIPTION_INFO + str(subscription_id).encode("utf-8")
    return hmac.new(master_key, message, hashlib.sha256).digest()


def load_master_key(config_dir: Path | str) -> bytes:
    """Read ``<config_dir>/push-signing.key`` as the push master key.

    ValueError when the file is missing, shorter than 32 bytes, or readable by
    group or other (mode & 0o077). The bytes are the raw file contents.
    """
    path = Path(config_dir) / "push-signing.key"
    try:
        metadata = path.stat()
    except OSError as error:
        raise ValueError(f"missing push-signing key: {path}") from error
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise ValueError(f"unsafe push-signing key permissions: {path}")
    try:
        key = path.read_bytes()
    except OSError as error:
        raise ValueError(f"unreadable push-signing key: {path}") from error
    if len(key) < 32:
        raise ValueError(f"push-signing key must be at least 32 bytes: {path}")
    return key


def send_signed(
    url: str,
    idempotency_key: str,
    body: Any,
    *,
    key: bytes,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Signed, bearer-less POST of one push body.

    Sends ``body`` through :func:`grokbot.send` with ``token=None`` (no
    Authorization header) and ``X-OMP-Signature`` for ``key``. ``grokbot.send``
    does not follow redirects, so a 3xx is a GrokbotError rather than a delivery
    to the Location. A GrokbotError is retried after ``sleep(1)`` then
    ``sleep(2)``; the third failure is raised.
    """
    canonical = body_bytes(body)
    header = signature(key, idempotency_key, canonical)
    for attempt in range(1, max(1, attempts) + 1):
        try:
            grokbot_send(
                url,
                None,
                idempotency_key,
                canonical,
                headers={_SIGNATURE_HEADER: header},
            )
            return
        except GrokbotError:
            if attempt >= attempts:
                raise
            sleep(float(attempt))


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return []


def _cursor_operation_id(subscription_id: str, next_after: int) -> UUID:
    return uuid5(NAMESPACE_URL, f"omp-push-cursor:{subscription_id}:{next_after}")


def _event_body(event: Any) -> Any:
    """JSON-safe view of a mission event for signing and delivery.

    Events arrive either as plain mappings (a fake client) or as pydantic views
    (``WorkClient.mission_events``), whose UUID/datetime fields ``json.dumps``
    refuses. ``model_dump(mode="json")`` normalizes the latter to JSON scalars.
    """
    dump = getattr(event, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return dict(event)


def run_push(
    client: Any,
    *,
    workspace_id: UUID,
    master_key: bytes,
    allowed_hosts: Iterable[str],
    resolve: Callable[[str], Iterable[str]] = _getaddrinfo_ips,
    send: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Deliver every client's subscribed mission events to its push_url.

    Each ``client.event_subscriptions()`` row is handled in turn. A row with no
    push_url is a pull subscription and is skipped, as is an ``ops.*`` stream.
    A push_url ``check_destination`` refuses returns ``{"refused": reason}``
    and nothing is sent. Otherwise mission events page from the row's cursor;
    each event whose type is subscribed is sent once (Idempotency-Key is its
    mission_event_id) and the cursor advances to ``next_after_sequence`` with
    ``operation_id = uuid5(NAMESPACE_URL, f"omp-push-cursor:{id}:{seq}")``. A
    send failure returns ``{"failed": reason}`` for the row and stops, after
    advancing the cursor to the last delivered event when that is past the
    current cursor. Returns ``{"pushed": n}``.

    ``send`` defaults to the module-level :func:`send_signed` and is looked up
    at call time so a monkeypatched ``event_push.send_signed`` takes effect.
    """
    if send is None:
        send = send_signed
    allowed = frozenset(allowed_hosts)
    pushed = 0
    page = client.event_subscriptions()
    for subscription in _items(_field(page, "subscriptions", ())):
        sub_id = str(_field(subscription, "subscription_id", ""))
        event_types = set(_items(_field(subscription, "event_types", ())))
        if event_types and all(
            isinstance(item, str) and item.startswith("ops.") for item in event_types
        ):
            continue
        destination = _field(subscription, "push_url")
        if not isinstance(destination, str) or not destination.strip():
            continue
        reason = check_destination(
            destination, allowed_hosts=allowed, resolve=resolve
        )
        if reason is not None:
            return {"refused": reason}
        cursor = int(_field(subscription, "cursor_sequence", 0) or 0)
        key = subscription_key(master_key, sub_id)
        while True:
            events_page = client.mission_events(after_sequence=cursor, limit=500)
            events = _items(_field(events_page, "events", ()))
            next_after = int(
                _field(events_page, "next_after_sequence", cursor) or cursor
            )
            last_delivered: int | None = None
            try:
                for event in events:
                    if _field(event, "type") not in event_types:
                        continue
                    event_id = str(_field(event, "mission_event_id", ""))
                    send(
                        destination,
                        event_id,
                        _event_body(event),
                        key=key,
                    )
                    pushed += 1
                    sequence = _field(event, "sequence")
                    if sequence is not None:
                        last_delivered = int(sequence)
            except Exception as error:  # noqa: BLE001 - surfaced as the failed reason
                if last_delivered is not None and last_delivered > cursor:
                    _advance(client, workspace_id, sub_id, last_delivered)
                return {"failed": str(error)}
            if next_after > cursor:
                _advance(client, workspace_id, sub_id, next_after)
            if not bool(_field(events_page, "has_more", False)):
                break
            cursor = next_after
    return {"pushed": pushed}


def _advance(
    client: Any,
    workspace_id: UUID,
    subscription_id: str,
    after_sequence: int,
) -> None:
    """Persist a cursor advance, keyed deterministically by subscription+sequence.

    Idempotent: the envelope's operation, request, and correlation ids are
    ``uuid5(NAMESPACE_URL, f"omp-push-cursor:{id}:{seq}")``, so replaying the
    same advance does not fork the cursor (the store returns the prior result
    for a repeated operation id).
    """
    operation_id = _cursor_operation_id(subscription_id, after_sequence)
    envelope = CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=operation_id,
        request_id=operation_id,
        correlation_id=operation_id,
        command=AdvanceEventCursorCommand(
            type="advance_event_cursor",
            payload=AdvanceEventCursor(
                subscription_id=UUID(subscription_id),
                after_sequence=after_sequence,
            ),
        ),
    )
    client.execute(envelope)
