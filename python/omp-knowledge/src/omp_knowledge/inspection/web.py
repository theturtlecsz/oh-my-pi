from __future__ import annotations

import argparse
import html
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .report import InspectionReport, build_report

VALID_API_SECTIONS = ("sources", "evidence", "procedures", "acceptance")


def render_html_page(report: InspectionReport) -> str:
    """Renders a complete HTML page with all four sections and all values HTML-escaped."""
    data = report.to_dict()

    sources_html = _render_sources_html(data.get("sources"))
    evidence_html = _render_evidence_html(data.get("evidence"))
    procedures_html = _render_procedures_html(data.get("procedures"))
    acceptance_html = _render_acceptance_html(data.get("acceptance"))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Fleet Knowledge Inspection Dashboard</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem; color: #1e293b; background: #f8fafc; }}
    h1 {{ color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem; }}
    h2 {{ color: #334155; margin-top: 2rem; }}
    section {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
    th, td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #e2e8f0; font-size: 0.9rem; }}
    th {{ background: #f1f5f9; font-weight: 600; color: #475569; }}
    .missing {{ color: #dc2626; font-style: italic; }}
    .badge {{ display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.8rem; font-weight: 600; }}
    .badge-active, .badge-accepted {{ background: #dcfce7; color: #166534; }}
    .badge-rejected, .badge-failed {{ background: #fee2e2; color: #991b1b; }}
    .badge-withdrawn {{ background: #f1f5f9; color: #475569; }}
    pre {{ background: #f8fafc; padding: 0.5rem; border-radius: 4px; overflow-x: auto; }}
  </style>
</head>
<body>
  <h1>Fleet Knowledge Inspection Dashboard</h1>

  <section id="procedures-section">
    <h2>Procedures</h2>
    {procedures_html}
  </section>

  <section id="evidence-section">
    <h2>Evidence</h2>
    {evidence_html}
  </section>

  <section id="acceptance-section">
    <h2>Acceptance</h2>
    {acceptance_html}
  </section>

  <section id="sources-section">
    <h2>Sources</h2>
    {sources_html}
  </section>
</body>
</html>
"""


def _render_sources_html(sources_data: Any) -> str:
    if sources_data == "missing":
        return "<p class='missing'>missing</p>"
    if not isinstance(sources_data, dict):
        return f"<p>{html.escape(str(sources_data))}</p>"

    snaps = sources_data.get("structural_snapshots")
    units = sources_data.get("capture_units")

    parts: list[str] = []
    parts.append("<h3>Structural Snapshots</h3>")
    if snaps == "missing":
        parts.append("<p class='missing'>missing</p>")
    elif isinstance(snaps, list) and snaps:
        parts.append(
            "<table><thead><tr><th>Workspace</th><th>Repository</th><th>Snapshot</th><th>State</th><th>Published At</th></tr></thead><tbody>"
        )
        for s in snaps:
            parts.append(
                f"<tr><td>{html.escape(str(s.get('workspace')))}</td>"
                f"<td>{html.escape(str(s.get('repository')))}</td>"
                f"<td>{html.escape(str(s.get('snapshot')))}</td>"
                f"<td>{html.escape(str(s.get('state')))}</td>"
                f"<td>{html.escape(str(s.get('published_at')))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No structural snapshots recorded.</p>")

    parts.append("<h3>Capture Units</h3>")
    if units == "missing":
        parts.append("<p class='missing'>missing</p>")
    elif isinstance(units, list) and units:
        parts.append(
            "<table><thead><tr><th>Unit ID</th><th>Event Source</th><th>State</th><th>Error Code</th></tr></thead><tbody>"
        )
        for u in units:
            parts.append(
                f"<tr><td>{html.escape(str(u.get('unit_id')))}</td>"
                f"<td>{html.escape(str(u.get('event_source')))}</td>"
                f"<td>{html.escape(str(u.get('state')))}</td>"
                f"<td>{html.escape(str(u.get('error_code')))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No capture units recorded.</p>")

    return "".join(parts)


def _render_evidence_html(evidence_data: Any) -> str:
    if evidence_data == "missing":
        return "<p class='missing'>missing</p>"
    if isinstance(evidence_data, list) and evidence_data:
        parts = [
            "<table><thead><tr><th>Procedure ID</th><th>Support Receipts</th><th>Uses Receipts</th><th>Outcomes</th><th>Corrections</th></tr></thead><tbody>"
        ]
        for ev in evidence_data:
            parts.append(
                f"<tr><td>{html.escape(str(ev.get('procedure_id')))}</td>"
                f"<td>{html.escape(str(ev.get('procedure_support') or ev.get('supporting_receipt_ids')))}</td>"
                f"<td>{html.escape(str(ev.get('uses') or ev.get('use_receipt_ids')))}</td>"
                f"<td>{html.escape(str(ev.get('outcomes') or ev.get('outcome_receipt_ids')))}</td>"
                f"<td>{html.escape(str(ev.get('corrections') or ev.get('correction_receipt_ids')))}</td></tr>"
            )
        parts.append("</tbody></table>")
        return "".join(parts)
    return "<p>No evidence recorded.</p>"


def _render_procedures_html(procedures_data: Any) -> str:
    if procedures_data == "missing":
        return "<p class='missing'>missing</p>"
    if isinstance(procedures_data, list) and procedures_data:
        parts = [
            "<table><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Version</th><th>Supporting Receipts</th><th>Outcome Receipts</th><th>Version History</th></tr></thead><tbody>"
        ]
        for p in procedures_data:
            v_hist = p.get("version_history", [])
            v_str = ", ".join(f"v{v.get('version')}: {v.get('title')}" for v in v_hist) if v_hist else "-"
            parts.append(
                f"<tr><td>{html.escape(str(p.get('id')))}</td>"
                f"<td>{html.escape(str(p.get('title')))}</td>"
                f"<td><span class='badge badge-{html.escape(str(p.get('status')))}'>{html.escape(str(p.get('status')))}</span></td>"
                f"<td>v{html.escape(str(p.get('current_version')))}</td>"
                f"<td>{html.escape(str(p.get('supporting_receipt_ids')))}</td>"
                f"<td>{html.escape(str(p.get('outcome_receipt_ids')))}</td>"
                f"<td>{html.escape(v_str)}</td></tr>"
            )
        parts.append("</tbody></table>")
        return "".join(parts)
    return "<p>No procedures recorded.</p>"


def _render_acceptance_html(acceptance_data: Any) -> str:
    if acceptance_data == "missing":
        return "<p class='missing'>missing</p>"
    if not isinstance(acceptance_data, dict):
        return f"<p>{html.escape(str(acceptance_data))}</p>"

    props = acceptance_data.get("proposals", [])
    cleanup = acceptance_data.get("cleanup_queue", {})

    parts: list[str] = []
    parts.append("<h3>Proposals</h3>")
    if props:
        parts.append(
            "<table><thead><tr><th>Proposal ID</th><th>Status</th><th>Reason</th><th>Procedure ID</th><th>Created At</th></tr></thead><tbody>"
        )
        for pr in props:
            parts.append(
                f"<tr><td>{html.escape(str(pr.get('proposal_id')))}</td>"
                f"<td><span class='badge badge-{html.escape(str(pr.get('status')))}'>{html.escape(str(pr.get('status')))}</span></td>"
                f"<td>{html.escape(str(pr.get('reason')))}</td>"
                f"<td>{html.escape(str(pr.get('procedure_id')))}</td>"
                f"<td>{html.escape(str(pr.get('created_at')))}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<p>No proposals recorded.</p>")

    pending = cleanup.get("pending", [])
    done = cleanup.get("done", [])
    parts.append("<h3>Cleanup Queue</h3>")
    parts.append(f"<p><strong>Pending:</strong> {len(pending)} | <strong>Done:</strong> {len(done)}</p>")
    if pending:
        parts.append("<h4>Pending Tasks</h4><table><thead><tr><th>Queue ID</th><th>Procedure ID</th><th>Action</th><th>Created At</th></tr></thead><tbody>")
        for q in pending:
            parts.append(
                f"<tr><td>{html.escape(str(q.get('queue_id')))}</td>"
                f"<td>{html.escape(str(q.get('procedure_id')))}</td>"
                f"<td>{html.escape(str(q.get('action')))}</td>"
                f"<td>{html.escape(str(q.get('created_at')))}</td></tr>"
            )
        parts.append("</tbody></table>")

    return "".join(parts)


class InspectionServer(HTTPServer):
    def __init__(self, server_address: tuple[str, int], state_root: str | Path) -> None:
        super().__init__(server_address, InspectionHandler)
        self.state_root = Path(state_root)


class InspectionHandler(BaseHTTPRequestHandler):
    server: InspectionServer

    def log_message(self, format: str, *args: Any) -> None:  # pylint: disable=redefined-builtin
        # Suppress routine console log spam
        pass

    def _send_405(self) -> None:
        self.send_response(405)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Allow", "GET")
        body = b"Method Not Allowed"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self._send_405()

    def do_PUT(self) -> None:
        self._send_405()

    def do_DELETE(self) -> None:
        self._send_405()

    def do_PATCH(self) -> None:
        self._send_405()

    def do_HEAD(self) -> None:
        self._send_405()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        # Report is rebuilt per request as required by specification
        report = build_report(self.server.state_root)

        if path in ("/", "/index.html"):
            html_page = render_html_page(report)
            body = html_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/api/"):
            sec = path[len("/api/") :]
            if sec in VALID_API_SECTIONS:
                sec_data = report.to_dict().get(sec)
                body = json.dumps(sec_data, indent=2, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if path in ("/api", "/api/all"):
            body = json.dumps(report.to_dict(), indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Any unknown path is 404
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = b"Not Found"
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    state_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> tuple[InspectionServer, int]:
    """Creates the inspection HTTP server bound to 127.0.0.1 only."""
    server = InspectionServer((host, port), state_root=state_root)
    actual_port = server.server_port
    return server, actual_port


def serve(
    state_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 0,
    once: bool = False,
) -> int:
    """Serves the inspection HTTP dashboard and API."""
    server, actual_port = create_server(state_root, host=host, port=port)
    url = f"http://{host}:{actual_port}/"
    print(f"Fleet Knowledge Inspection Web Dashboard listening at {url}")
    sys.stdout.flush()

    try:
        if once:
            server.handle_request()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omp_knowledge.inspection serve",
        description="Serve Web inspection dashboard and JSON API",
    )
    parser.add_argument(
        "--state-root",
        required=True,
        help="Path to state root holding learning.sqlite and structural-publications.sqlite",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to listen on (default 0 for ephemeral)",
    )
    args = parser.parse_args(argv)

    return serve(args.state_root, port=args.port)


if __name__ == "__main__":
    sys.exit(main())
