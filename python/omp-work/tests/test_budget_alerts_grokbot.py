# ruff: noqa: F811
"""Deliver budget_alert rows to grokbot (OMP-404-s06).

Proves the delivery contract on the shared ``omp_jobs`` substrate and the
loopback HTTP path:

- a loopback ``http.server`` receives the 50 and 80 alerts with
  ``Authorization: Bearer`` and ``Idempotency-Key``, and a second run sends
  nothing;
- a 500 leaves the row failed, and the next 200 delivers it once;
- ``send_alert`` refuses ``file:`` and non-loopback http;
- ``budget-alerts`` exits 2 when the URL or token file is missing or unreadable;
- ``test_live_grokbot`` sends one synthetic 50-percent tokens alert only when
  ``OMP_GROKBOT_LIVE=1``.
"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from native_jobs_support import native_jobs  # noqa: F401  (fixture)
from omp_work.__main__ import main
from omp_work.jobs.grokbot import GrokbotError, deliver_budget_alerts, send_alert
from omp_work.jobs.store import NativeJobStore
from omp_work.v1.canonical import sha256
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

pytest_plugins = ("test_workflow_service",)

_TOKEN = "budget-alert-token"
requires_postgres = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


class _AlertServer(HTTPServer):
    requests: list[dict[str, object]]
    status_code: int

    def __init__(self, server_address, handler) -> None:  # noqa: ANN001
        super().__init__(server_address, handler)
        self.requests = []
        self.status_code = 200


class _Handler(BaseHTTPRequestHandler):
    server: _AlertServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode())
        self.server.requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "idempotency_key": self.headers.get("Idempotency-Key"),
                "header_names": list(self.headers.keys()),
                "body": body,
            }
        )
        raw = b"{}"
        self.send_response(self.server.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args) -> None:  # noqa: A002, ANN001
        return


@pytest.fixture
def alert_server():
    """Loopback stub. The server thread is shut down when the test ends."""
    server = _AlertServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _url(server: _AlertServer) -> str:
    host, port = server.server_address
    return f"http://{host}:{port}/alerts"


def _store(native_jobs) -> NativeJobStore:
    return NativeJobStore(native_jobs.service.config)


def _payload(threshold: int) -> dict[str, object]:
    return {
        "event": "budget.threshold_reached",
        "dimension": "tokens",
        "threshold_percent": threshold,
        "spent": 500 if threshold == 50 else 800,
        "limit": 1000,
    }


def _event_id(workspace_id: UUID, work_id: UUID, threshold: int) -> str:
    return sha256(
        {
            "workspace_id": str(workspace_id),
            "work_id": str(work_id),
            "dimension": "tokens",
            "threshold": threshold,
        }
    )


def _insert(
    store: NativeJobStore,
    workspace_id: UUID,
    actor_id: UUID,
    event_id: str,
    payload: dict[str, object],
    *,
    kind: str = "budget_alert",
) -> None:
    with store.transaction(workspace_id, actor_id) as cur:
        cur.execute(
            """
            INSERT INTO omp_jobs.outbox(
                event_id, workspace_id, operation_id, kind, payload, state, revision
            ) VALUES (%s, %s, %s, %s, %s, 'committed', 1)
            """,
            (event_id, workspace_id, f"op-{event_id[:12]}", kind, Jsonb(payload)),
        )


def _rows(native_jobs, workspace_id: UUID) -> dict[str, dict[str, object]]:
    with psycopg.connect(
        **native_jobs.service.config.connection_kwargs("omp_work_app"),
        row_factory=dict_row,
        autocommit=True,
    ) as conn:
        found = conn.execute(
            """
            SELECT event_id, kind, state, revision, ack_token
            FROM omp_jobs.outbox
            WHERE workspace_id=%s
            """,
            (workspace_id,),
        ).fetchall()
    return {str(row["event_id"]): row for row in found}


def _forbid_network(monkeypatch) -> None:
    def _open(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("send_alert opened a connection")

    monkeypatch.setattr("omp_work.jobs.grokbot.request.OpenerDirector.open", _open)


def test_send_alert_refuses_file_and_non_loopback_http(monkeypatch) -> None:
    """file: and http to anything but loopback raise before a connection is opened."""
    _forbid_network(monkeypatch)
    payload = {"event": "budget.threshold_reached", "dimension": "tokens", "threshold_percent": 50}
    for url in (
        "file:///etc/passwd",
        "file:/etc/passwd",
        "http://example.com/hook",
        "http://192.0.2.1/alerts",
        "http://127.0.0.1.example.com/alerts",
        "http://127.0.0.1@example.com/alerts",
    ):
        with pytest.raises(GrokbotError, match="alert url refused"):
            send_alert(url, "tok", "evt", payload, timeout=0.2)


def test_send_alert_posts_json_with_both_headers(alert_server) -> None:
    """A loopback POST carries the bearer token and the idempotency key."""
    token = "grokbot-bearer-secret"
    send_alert(
        _url(alert_server),
        token,
        "event-50",
        {"event": "budget.threshold_reached", "dimension": "tokens", "threshold_percent": 50},
    )
    assert len(alert_server.requests) == 1
    recorded = alert_server.requests[0]
    assert recorded["authorization"] == f"Bearer {token}"
    assert recorded["idempotency_key"] == "event-50"
    assert "Authorization" in recorded["header_names"]
    assert "Idempotency-Key" in recorded["header_names"]
    assert recorded["body"]["threshold_percent"] == 50


def test_send_alert_http_error_omits_token(alert_server) -> None:
    """A non-2xx raises, and the error text does not contain the bearer token."""
    alert_server.status_code = 500
    token = "grokbot-bearer-secret"
    with pytest.raises(GrokbotError, match="HTTP 500") as caught:
        send_alert(_url(alert_server), token, "event-500", {"threshold_percent": 50})
    assert token not in str(caught.value)


def test_https_is_allowed_and_localhost_http_is_allowed(monkeypatch) -> None:
    """https to any host, and http to localhost, pass the URL check."""
    seen: list[object] = []

    class _Response:
        def read(self) -> bytes:
            return b""

        def getcode(self) -> int:
            return 200

        def __enter__(self):
            return self

        def __exit__(self, *args) -> bool:  # noqa: ANN002
            return False

    def _open(self, outgoing, timeout=None):  # noqa: ANN001, ARG001
        seen.append(outgoing)
        return _Response()

    monkeypatch.setattr("omp_work.jobs.grokbot.request.OpenerDirector.open", _open)
    payload = {"threshold_percent": 50}
    send_alert("https://alerts.example/hook", "tok", "evt-https", payload)
    send_alert("http://localhost/alerts", "tok", "evt-local", payload)
    assert [item.get_header("Idempotency-Key") for item in seen] == ["evt-https", "evt-local"]
    assert seen[0].get_header("Authorization") == "Bearer tok"
    assert seen[0].get_header("Content-Type") == "application/json"


def test_cli_exits_2_without_env(monkeypatch, capsys, tmp_path) -> None:
    """Missing URL, missing token file, unreadable file, or an empty file exits 2."""
    secret = "cli-should-not-print-this-token"
    token_file = tmp_path / "token"
    token_file.write_text(secret + "\n")

    def _forbid(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("budget-alerts ran without config")

    monkeypatch.setattr("omp_work.jobs.budget.sweep_item_budgets", _forbid)
    monkeypatch.setattr("omp_work.jobs.grokbot.deliver_budget_alerts", _forbid)
    args = ["budget-alerts", "--workspace", str(uuid4()), "--actor", str(uuid4())]

    monkeypatch.delenv("OMP_GROKBOT_ALERT_URL", raising=False)
    monkeypatch.delenv("OMP_GROKBOT_ALERT_TOKEN_FILE", raising=False)
    assert main(args) == 2

    monkeypatch.setenv("OMP_GROKBOT_ALERT_URL", "https://alerts.example/hook")
    assert main(args) == 2

    monkeypatch.setenv("OMP_GROKBOT_ALERT_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("OMP_GROKBOT_ALERT_URL", raising=False)
    assert main(args) == 2

    monkeypatch.setenv("OMP_GROKBOT_ALERT_URL", "https://alerts.example/hook")
    monkeypatch.setenv("OMP_GROKBOT_ALERT_TOKEN_FILE", str(tmp_path / "missing"))
    assert main(args) == 2

    token_file.write_text(" \n")
    monkeypatch.setenv("OMP_GROKBOT_ALERT_TOKEN_FILE", str(token_file))
    assert main(args) == 2

    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_cli_prints_json_count(monkeypatch, capsys, tmp_path) -> None:
    """Success sweeps, then delivers, and prints the JSON count. The token stays out of the output."""
    secret = "cli-delivery-token"
    token_file = tmp_path / "token"
    token_file.write_text(secret + "\n")
    monkeypatch.setenv("OMP_GROKBOT_ALERT_URL", "https://alerts.example/hook")
    monkeypatch.setenv("OMP_GROKBOT_ALERT_TOKEN_FILE", str(token_file))
    calls: list[tuple[object, ...]] = []

    def _sweep(store, *, operation_id, workspace_id, actor_id):  # noqa: ANN001
        calls.append(("sweep", workspace_id, actor_id, operation_id))
        return []

    def _deliver(store, *, workspace_id, actor_id, url, token):  # noqa: ANN001
        calls.append(("deliver", workspace_id, actor_id, url, token))
        return 2

    monkeypatch.setattr("omp_work.jobs.budget.sweep_item_budgets", _sweep)
    monkeypatch.setattr("omp_work.jobs.grokbot.deliver_budget_alerts", _deliver)
    workspace_id, actor_id = uuid4(), uuid4()
    assert main(["budget-alerts", "--workspace", str(workspace_id), "--actor", str(actor_id)]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == 2
    assert secret not in captured.out
    assert secret not in captured.err
    assert calls[0][0] == "sweep"
    assert calls[1][0] == "deliver"
    assert calls[0][1] == workspace_id
    assert calls[1][1] == workspace_id
    assert calls[1][3] == "https://alerts.example/hook"
    assert calls[1][4] == secret


@requires_postgres
def test_delivers_50_and_80_then_second_run_sends_nothing(native_jobs, alert_server) -> None:
    """The 50 and 80 alerts are posted once, with both headers, and a second run sends nothing."""
    store = _store(native_jobs)
    workspace_id = uuid4()
    work_id = uuid4()
    actor_id = native_jobs.actor_id
    payloads = {threshold: _payload(threshold) for threshold in (50, 80)}
    event_ids = {
        threshold: _event_id(workspace_id, work_id, threshold) for threshold in (50, 80)
    }
    for threshold in (50, 80):
        _insert(store, workspace_id, actor_id, event_ids[threshold], payloads[threshold])
    usage_id = _event_id(workspace_id, work_id, 1)
    _insert(
        store,
        workspace_id,
        actor_id,
        usage_id,
        {"usage_id": "keep-me", "event": "usage"},
        kind="usage_ledger",
    )

    sent = deliver_budget_alerts(
        store,
        workspace_id=workspace_id,
        actor_id=actor_id,
        url=_url(alert_server),
        token=_TOKEN,
    )
    assert sent == 2
    assert [row["idempotency_key"] for row in alert_server.requests] == sorted(event_ids.values())
    for recorded in alert_server.requests:
        assert recorded["authorization"] == f"Bearer {_TOKEN}"
        assert "Authorization" in recorded["header_names"]
        assert "Idempotency-Key" in recorded["header_names"]
        assert recorded["body"] == payloads[
            50 if recorded["body"]["threshold_percent"] == 50 else 80
        ]

    assert (
        deliver_budget_alerts(
            store,
            workspace_id=workspace_id,
            actor_id=actor_id,
            url=_url(alert_server),
            token=_TOKEN,
        )
        == 0
    )
    assert len(alert_server.requests) == 2

    rows = _rows(native_jobs, workspace_id)
    for threshold in (50, 80):
        row = rows[event_ids[threshold]]
        assert row["kind"] == "budget_alert"
        assert row["state"] == "closed"
        assert int(row["revision"]) == 1
        assert row["ack_token"] == "ack-1"
    usage = rows[usage_id]
    assert usage["kind"] == "usage_ledger"
    assert usage["state"] == "committed"


@requires_postgres
def test_http_500_leaves_row_failed_then_delivers_once(native_jobs, alert_server) -> None:
    """A 500 marks the row failed. The next 200 delivers it once and a further run does not."""
    store = _store(native_jobs)
    workspace_id = uuid4()
    actor_id = native_jobs.actor_id
    event_id = _event_id(workspace_id, uuid4(), 80)
    payload = _payload(80)
    _insert(store, workspace_id, actor_id, event_id, payload)
    alert_server.status_code = 500

    assert (
        deliver_budget_alerts(
            store,
            workspace_id=workspace_id,
            actor_id=actor_id,
            url=_url(alert_server),
            token=_TOKEN,
        )
        == 0
    )
    failed = _rows(native_jobs, workspace_id)[event_id]
    assert failed["state"] == "failed"
    assert int(failed["revision"]) == 1
    assert failed["ack_token"] is None
    assert len(alert_server.requests) == 1
    assert alert_server.requests[0]["idempotency_key"] == event_id
    assert alert_server.requests[0]["authorization"] == f"Bearer {_TOKEN}"

    alert_server.status_code = 200
    assert (
        deliver_budget_alerts(
            store,
            workspace_id=workspace_id,
            actor_id=actor_id,
            url=_url(alert_server),
            token=_TOKEN,
        )
        == 1
    )
    assert len(alert_server.requests) == 2
    assert alert_server.requests[1]["idempotency_key"] == event_id
    assert alert_server.requests[1]["body"] == payload
    assert (
        deliver_budget_alerts(
            store,
            workspace_id=workspace_id,
            actor_id=actor_id,
            url=_url(alert_server),
            token=_TOKEN,
        )
        == 0
    )
    assert len(alert_server.requests) == 2
    closed = _rows(native_jobs, workspace_id)[event_id]
    assert closed["state"] == "closed"
    assert int(closed["revision"]) == 2
    assert closed["ack_token"] == "ack-2"


@pytest.mark.skipif(
    os.environ.get("OMP_GROKBOT_LIVE") != "1",
    reason="set OMP_GROKBOT_LIVE=1",
)
def test_live_grokbot() -> None:
    """Send one synthetic 50-percent tokens alert to the configured grokbot URL."""
    url = os.environ["OMP_GROKBOT_ALERT_URL"]
    token = Path(os.environ["OMP_GROKBOT_ALERT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    send_alert(
        url,
        token,
        f"synthetic-{uuid4()}",
        {
            "event": "budget.threshold_reached",
            "dimension": "tokens",
            "threshold_percent": 50,
            "spent": 50,
            "limit": 100,
        },
    )
