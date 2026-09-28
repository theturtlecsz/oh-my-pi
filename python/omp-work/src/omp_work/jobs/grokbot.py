"""Deliver ``budget_alert`` outbox rows to grokbot (OMP-404-s06).

``jobs.budget`` writes one committed ``budget_alert`` row per reached
threshold. ``send_alert`` POSTs that payload as JSON. https is allowed, and
http only to 127.0.0.1 or localhost. The bearer token is sent as a header and
is never copied into an error.

``deliver_budget_alerts`` reads one workspace's ``committed`` or ``failed``
``budget_alert`` rows in ``event_id`` order under ``FOR UPDATE SKIP LOCKED``.
A failed row is committed again first. A sent row is acknowledged and closed
with the ``contracts.v1.recovery`` helpers ``jobs.outbox`` uses. A delivery
error returns the row to ``failed`` for a later retry. The return value is the
number of alerts sent.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error, request
from urllib.parse import urlparse
from uuid import UUID

from omp_work.contracts.v1 import recovery
from omp_work.jobs.store import NativeJobStore

__all__ = ["GrokbotError", "deliver_budget_alerts", "send_alert"]

_PENDING_STATES = ("committed", "failed")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


class GrokbotError(RuntimeError):
    """A refused URL or a failed delivery. The message never includes the token."""


class _Request(request.Request):
    """Keep header names as set.

    ``urllib.request.Request`` stores headers with ``str.capitalize``, which
    rewrites ``Idempotency-Key`` to ``Idempotency-key``.
    """

    def add_header(self, key: str, val: str) -> None:
        self.headers[key] = val

    def add_unredirected_header(self, key: str, val: str) -> None:
        self.unredirected_hdrs[key] = val

    def has_header(self, header_name: str) -> bool:
        wanted = header_name.lower()
        return any(key.lower() == wanted for key in self.headers) or any(
            key.lower() == wanted for key in self.unredirected_hdrs
        )


class _NoRedirect(request.HTTPRedirectHandler):
    """A 3xx is a failed delivery. Do not resend the bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise error.HTTPError(req.full_url, code, msg, headers, fp)


def send_alert(
    url: str,
    token: str,
    event_id: str,
    payload: object,
    timeout: float = 10,
) -> None:
    """POST ``payload`` as JSON. Raise ``GrokbotError`` on a refused URL, non-2xx, or network error."""
    _require_allowed_url(url)
    header_token = _header_value(token)
    header_event = _header_value(event_id)
    try:
        body = json.dumps(payload).encode("utf-8")
    except (TypeError, ValueError):
        raise GrokbotError("alert delivery failed: payload is not json") from None

    outgoing = _Request(url, data=body, method="POST")
    outgoing.add_unredirected_header("Authorization", f"Bearer {header_token}")
    outgoing.add_unredirected_header("Idempotency-Key", header_event)
    outgoing.add_unredirected_header("Content-Type", "application/json")
    # An empty proxy map replaces the default ProxyHandler, which would
    # otherwise follow http_proxy and send the bearer token off-loopback.
    opener = request.build_opener(_NoRedirect, request.ProxyHandler({}))
    try:
        with opener.open(outgoing, timeout=timeout) as response:
            response.read()
            status = response.getcode()
    except error.HTTPError as exc:
        raise GrokbotError(f"alert delivery failed: HTTP {exc.code}") from None
    except GrokbotError:
        raise
    except Exception:  # noqa: BLE001 - network and timeout failures share one token-free error
        raise GrokbotError("alert delivery failed: network error") from None
    if status is None or not 200 <= status < 300:
        raise GrokbotError(f"alert delivery failed: HTTP {status}")


def deliver_budget_alerts(
    store: NativeJobStore,
    *,
    workspace_id: UUID | str,
    actor_id: UUID | str,
    url: str,
    token: str,
) -> int:
    """Send pending ``budget_alert`` rows for ``workspace_id``. Return how many were sent."""
    if isinstance(workspace_id, str):
        workspace_id = UUID(workspace_id)
    if isinstance(actor_id, str):
        actor_id = UUID(actor_id)
    # A refused URL or a token that cannot be a header is a config error: leave
    # the rows committed so a corrected call can send them.
    _require_allowed_url(url)
    _header_value(token)

    sent = 0
    with store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            SELECT event_id, state, revision, ack_token, payload
            FROM omp_jobs.outbox
            WHERE workspace_id=%s AND state = ANY(%s) AND kind = 'budget_alert'
            ORDER BY event_id
            FOR UPDATE SKIP LOCKED
            """,
            (workspace_id, list(_PENDING_STATES)),
        )
        for row in cur.fetchall():
            record = recovery.CloseoutRecord(
                id=str(row["event_id"]),
                state=row["state"],
                revision=int(row["revision"]),
                ack_token=row["ack_token"],
            )
            if record.state == "failed":
                record = recovery.apply_commit(record)
            try:
                send_alert(url, token, record.id, row["payload"])
            except GrokbotError:
                failed = recovery.recover_unacked_commit(
                    record, evidence_trusted=False
                )
                _persist(cur, workspace_id, failed)
                continue
            acknowledged = recovery.recover_unacked_commit(
                record, evidence_trusted=True
            )
            _persist(cur, workspace_id, recovery.close(acknowledged))
            sent += 1
    return sent


def _require_allowed_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise GrokbotError("alert url refused")
    parsed = urlparse(url.strip())
    try:
        parsed.port
    except ValueError:
        raise GrokbotError("alert url refused") from None
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    if scheme == "https" and host:
        return
    if scheme == "http" and host in _LOOPBACK_HOSTS:
        return
    raise GrokbotError("alert url refused")


def _header_value(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\0"):
        raise GrokbotError("alert delivery failed: invalid header")
    return value


def _persist(cur: Any, workspace_id: UUID, record: recovery.CloseoutRecord) -> None:
    cur.execute(
        """
        UPDATE omp_jobs.outbox
        SET state=%s, revision=%s, ack_token=%s, updated_at=clock_timestamp()
        WHERE event_id=%s AND workspace_id=%s
        """,
        (record.state, record.revision, record.ack_token, record.id, workspace_id),
    )
