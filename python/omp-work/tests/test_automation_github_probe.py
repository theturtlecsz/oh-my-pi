"""OMP-402: the automation token probe must push but never administer.

The probe runs as a subprocess against a loopback fake GitHub API that records
every request. Each test pins one externally observable contract: a restricted
token passes with no write recorded, and any read that proves the token could
administer stops the probe before it sends a write.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

PROBE = Path(__file__).resolve().parents[3] / "infra" / "automation-user" / "github-probe.py"
TOKEN = "ghs_probe_secret_1d4f9a7c"
WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class FakeGitHub:
    """A loopback GitHub API stand-in that records every request it receives."""

    def __init__(
        self,
        *,
        push: bool = True,
        admin: bool = False,
        description: str = "base description",
        protection_status: int = 403,
        enforce_status: int = 403,
        repo_patch_status: int = 403,
        org_error_status: int = 403,
        org_description: str = "org description",
    ) -> None:
        self.push = push
        self.admin = admin
        self.description = description
        self.protection_status = protection_status
        self.enforce_status = enforce_status
        self.repo_patch_status = repo_patch_status
        self.org_error_status = org_error_status
        self.org_description = org_description
        self.requests: list[dict] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeGitHub":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _record(self) -> dict:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b""
                entry = {
                    "method": self.command,
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": json.loads(raw) if raw else None,
                }
                fake.requests.append(entry)
                return entry

            def _reply(self, status: int, body: object | None = None) -> None:
                payload = json.dumps(body if body is not None else {}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _route(self, method: str) -> None:
                request = self._record()
                path = request["path"].split("?", 1)[0]
                repo = "/repos/owner/name"
                protection = f"{repo}/branches/main/protection"
                if method == "GET" and path == repo:
                    self._reply(
                        200,
                        {
                            "description": fake.description,
                            "permissions": {"push": fake.push, "admin": fake.admin},
                        },
                    )
                elif method == "PATCH" and path == repo:
                    self._reply(fake.repo_patch_status)
                elif method == "GET" and path == protection:
                    self._reply(fake.protection_status)
                elif method == "POST" and path == f"{protection}/enforce_admins":
                    self._reply(fake.enforce_status)
                elif method == "GET" and path == "/orgs/owner":
                    self._reply(200, {"description": fake.org_description})
                elif method == "PATCH" and path == "/orgs/owner":
                    self._reply(fake.org_error_status)
                else:
                    self._reply(404)

            def do_GET(self) -> None:  # noqa: N802
                self._route("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._route("POST")

            def do_PATCH(self) -> None:  # noqa: N802
                self._route("PATCH")

            def do_PUT(self) -> None:  # noqa: N802
                self._route("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._route("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self._server is not None and self._thread is not None
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def writes(self) -> list[dict]:
        return [request for request in self.requests if request["method"] in WRITE_METHODS]


def run_probe(fake: FakeGitHub, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROBE),
            "--repo",
            "owner/name",
            "--branch",
            "main",
            "--api-url",
            fake.url,
            *extra,
        ],
        env={**os.environ, "GH_TOKEN": TOKEN},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_restricted_token_passes_with_every_check_ok() -> None:
    """A push-only token is exactly what the automation needs; no write is needed to prove it."""
    with FakeGitHub() as fake:
        result = run_probe(fake)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    checks = json.loads(result.stdout)
    assert checks == {
        "can_push": {"ok": True, "detail": "GET /repos/owner/name -> 200 permissions.push=true permissions.admin=false"},
        "branch_protection_denied": {
            "ok": True,
            "detail": "GET /repos/owner/name/branches/main/protection -> 403; POST /repos/owner/name/branches/main/protection/enforce_admins -> 403",
        },
        "repo_settings_denied": {
            "ok": True,
            "detail": "PATCH /repos/owner/name with the current description -> 403",
        },
        "org_settings_denied": {"ok": True, "detail": "skipped"},
    }
    # The description no-op carries the description read from the repo GET.
    repo_patch = next(request for request in fake.writes if request["method"] == "PATCH")
    assert repo_patch["body"] == {"description": "base description"}


def test_org_scope_enabled_probes_org_settings() -> None:
    """With --org the org description PATCH is probed; refused means the token cannot administer the org."""
    with FakeGitHub() as fake:
        result = run_probe(fake, "--org", "owner")
    assert result.returncode == 0, result.stderr
    checks = json.loads(result.stdout)
    assert checks["org_settings_denied"] == {
        "ok": True,
        "detail": "PATCH /orgs/owner with the current description -> 403",
    }
    org_patch = next(request for request in fake.writes if request["path"] == "/orgs/owner")
    assert org_patch["body"] == {"description": "org description"}


def test_admin_token_fails_before_any_write() -> None:
    """Admin permission on the repo GET must stop the probe before it sends any write."""
    with FakeGitHub(admin=True) as fake:
        result = run_probe(fake)
    assert result.returncode == 1
    checks = json.loads(result.stdout)
    assert checks["can_push"]["ok"] is False
    assert "admin" in checks["can_push"]["detail"]
    assert fake.writes == []


def test_reachable_branch_protection_fails_before_any_write() -> None:
    """Reading branch protection at all means the token can read admin surface; refuse every write."""
    with FakeGitHub(protection_status=200) as fake:
        result = run_probe(fake)
    assert result.returncode == 1
    checks = json.loads(result.stdout)
    assert checks["branch_protection_denied"]["ok"] is False
    assert "expected 403/404" in checks["branch_protection_denied"]["detail"]
    assert fake.writes == []


def test_allowed_repo_patch_is_a_failure() -> None:
    """An applied repo settings PATCH is an administered write, so the probe must report failure."""
    with FakeGitHub(repo_patch_status=200) as fake:
        result = run_probe(fake)
    assert result.returncode == 1
    checks = json.loads(result.stdout)
    assert checks["repo_settings_denied"]["ok"] is False
    assert "200" in checks["repo_settings_denied"]["detail"]
    assert [request["method"] for request in fake.writes] == ["POST", "PATCH"]


def test_token_never_reaches_stdout_or_stderr() -> None:
    """The bearer token must never be echoed, on success or on failure."""
    with FakeGitHub(protection_status=200) as fake:
        denied = run_probe(fake)
    with FakeGitHub() as fake:
        allowed = run_probe(fake)
    for result in (denied, allowed):
        assert TOKEN not in result.stdout
        assert TOKEN not in result.stderr
    # The fake still saw the token, proving the probe did authenticate.
    assert any(request["authorization"] == f"Bearer {TOKEN}" for request in fake.requests)


@pytest.mark.parametrize(
    "api_url",
    ["http://example.com", "http://10.0.0.5", "ftp://127.0.0.1"],
)
def test_non_https_non_loopback_api_url_is_refused_before_any_request(api_url: str) -> None:
    """A remote plaintext (or non-HTTP) API URL is refused at startup, before any request is sent."""
    with FakeGitHub() as fake:
        result = subprocess.run(
            [
                sys.executable,
                str(PROBE),
                "--repo",
                "owner/name",
                "--branch",
                "main",
                "--api-url",
                api_url,
            ],
            env={**os.environ, "GH_TOKEN": TOKEN},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert fake.requests == []
    assert result.returncode != 0
    assert "must be https" in json.loads(result.stdout)["error"]


def test_missing_token_is_refused_before_any_request() -> None:
    """Without GH_TOKEN there is nothing to probe; refuse before any request."""
    env = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
    with FakeGitHub() as fake:
        result = subprocess.run(
            [sys.executable, str(PROBE), "--repo", "owner/name", "--branch", "main", "--api-url", fake.url],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert fake.requests == []
    assert result.returncode != 0
    assert "GH_TOKEN" in json.loads(result.stdout)["error"]
