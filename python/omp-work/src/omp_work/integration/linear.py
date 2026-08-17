from __future__ import annotations

import base64
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json
import os
import secrets
import stat
import time
from typing import Any, Callable, Iterator, Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator, model_validator

from omp_work.operations.capabilities import _write_secret


class LinearCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["oauth", "api_key"]
    access_token: SecretStr
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None
    refresh_token: SecretStr | None = None
    client_id: str | None = None

    @field_validator("access_token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("linear_credential_invalid")
        return value

    @model_validator(mode="after")
    def valid_authentication_mode(self) -> Self:
        if self.kind == "api_key":
            if (
                not self.access_token.get_secret_value().startswith("lin_api_")
                or self.scopes
                or self.expires_at is not None
                or self.refresh_token is not None
                or self.client_id is not None
            ):
                raise ValueError("linear_credential_invalid")
        elif set(self.scopes) != {"read"}:
            raise ValueError("linear_credential_scope_invalid")
        elif (
            self.expires_at is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.refresh_token is None
            or not self.refresh_token.get_secret_value()
            or not self.client_id
        ):
            raise ValueError("linear_credential_invalid")
        return self


class LinearStream(StrEnum):
    teams = "teams"
    initiatives = "initiatives"
    projects = "projects"
    project_updates = "projectUpdates"
    milestones = "projectMilestones"
    issues = "issues"
    states = "workflowStates"
    labels = "issueLabels"
    initiative_projects = "initiativeToProjects"
    relations = "issueRelations"
    comments = "comments"
    attachments = "attachments"


QUERIES: dict[LinearStream, str] = {
    LinearStream.teams: "query teams($first: Int!, $after: String, $filter: TeamFilter) { teams(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id key name description createdAt updatedAt archivedAt } pageInfo { hasNextPage endCursor } } }",
    LinearStream.initiatives: "query initiatives($first: Int!, $after: String, $filter: InitiativeFilter) { initiatives(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id name description status targetDate createdAt updatedAt archivedAt } pageInfo { hasNextPage endCursor } } }",
    LinearStream.projects: "query projects($first: Int!, $after: String, $filter: ProjectFilter) { projects(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id name description status { id name type } health startDate targetDate createdAt updatedAt archivedAt teams { nodes { key } } lead { id name displayName active } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.project_updates: "query projectUpdates($first: Int!, $after: String, $filter: ProjectUpdateFilter) { projectUpdates(first: $first, after: $after, filter: $filter) { nodes { id body health createdAt updatedAt archivedAt project { id } user { id name displayName active } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.milestones: "query projectMilestones($first: Int!, $after: String, $filter: ProjectMilestoneFilter) { projectMilestones(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id name description targetDate createdAt updatedAt archivedAt project { id } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.issues: "query issues($first: Int!, $after: String, $filter: IssueFilter) { issues(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id identifier previousIdentifiers title description priority estimate dueDate createdAt updatedAt archivedAt canceledAt completedAt url team { key } parent { id } project { id } projectMilestone { id } state { id name type } labels { nodes { id name } } assignee { id name displayName active } creator { id name displayName active } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.states: "query workflowStates($first: Int!, $after: String, $filter: WorkflowStateFilter) { workflowStates(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id name type position description createdAt updatedAt archivedAt team { key } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.labels: "query issueLabels($first: Int!, $after: String, $filter: IssueLabelFilter) { issueLabels(first: $first, after: $after, includeArchived: true, filter: $filter) { nodes { id name color description createdAt updatedAt archivedAt team { key } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.initiative_projects: "query initiativeToProjects($first: Int!, $after: String) { initiativeToProjects(first: $first, after: $after) { nodes { id createdAt updatedAt archivedAt initiative { id } project { id } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.relations: "query issueRelations($first: Int!, $after: String) { issueRelations(first: $first, after: $after) { nodes { id type createdAt updatedAt archivedAt issue { id identifier team { key } } relatedIssue { id identifier team { key } } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.comments: "query comments($first: Int!, $after: String, $filter: CommentFilter) { comments(first: $first, after: $after, filter: $filter) { nodes { id body url createdAt updatedAt archivedAt issue { id identifier team { key } } user { id name displayName active } parent { id } } pageInfo { hasNextPage endCursor } } }",
    LinearStream.attachments: "query attachments($first: Int!, $after: String) { attachments(first: $first, after: $after) { nodes { id title subtitle url sourceType metadata createdAt updatedAt archivedAt issue { id identifier team { key } } creator { id name displayName active } } pageInfo { hasNextPage endCursor } } }",
}


def load_credential(path: Path) -> LinearCredential:
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("linear_credential_permissions_invalid")
        return LinearCredential.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError("linear_credential_missing") from None
    except ValidationError as error:
        if "linear_credential_scope_invalid" in str(error):
            raise ValueError("linear_credential_scope_invalid") from None
        raise ValueError("linear_credential_invalid") from None
    except ValueError:
        raise
    except OSError:
        raise ValueError("linear_credential_invalid") from None


class LinearClient:
    def __init__(self, credential: LinearCredential, *, transport: httpx.BaseTransport | None = None) -> None:
        token = credential.access_token.get_secret_value()
        authorization = token if credential.kind == "api_key" else f"Bearer {token}"
        self._client = httpx.Client(base_url="https://api.linear.app/graphql", headers={"Authorization": authorization}, transport=transport, timeout=30)

    def close(self) -> None:
        self._client.close()

    def pages(self, stream: LinearStream, *, filter: dict[str, Any] | None = None, after: str | None = None) -> Iterator[tuple[str | None, list[dict[str, Any]], bool, str | None, str]]:
        cursor = after
        while True:
            variables: dict[str, Any] = {"first": 50, "after": cursor}
            if filter is not None:
                variables["filter"] = filter
            try:
                response = self._client.post("", json={"query": QUERIES[stream], "variables": variables})
                response.raise_for_status()
                payload = response.json()
                if errors := payload.get("errors"):
                    detail = json.dumps(errors).casefold()
                    code = "linear_credential_permission_denied" if any(marker in detail for marker in ("auth", "forbidden", "permission", "unauthorized")) else "linear_transport_failed"
                    raise RuntimeError(code)
                page = payload["data"][stream.value]
                nodes = page["nodes"]
                info = page["pageInfo"]
                has_next, next_cursor = bool(info["hasNextPage"]), info["endCursor"]
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 401:
                    raise RuntimeError("linear_credential_invalid") from None
                if error.response.status_code == 403:
                    raise RuntimeError("linear_credential_permission_denied") from None
                raise RuntimeError("linear_transport_failed") from None
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError("linear_transport_failed") from None
            if not isinstance(nodes, list) or (has_next and (not isinstance(next_cursor, str) or next_cursor == cursor)):
                raise RuntimeError("pagination_count_hash_gap")
            yield cursor, nodes, has_next, next_cursor, json.dumps(variables, sort_keys=True, separators=(",", ":"))
            if not has_next:
                return
            cursor = next_cursor


AUTHORIZE_ENDPOINT = "https://linear.app/oauth/authorize"
TOKEN_ENDPOINT = "https://api.linear.app/oauth/token"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 54323
CALLBACK_PATH = "/oauth/callback"
REDIRECT_URI = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
REFRESH_MARGIN = timedelta(minutes=5)
LOGIN_DEADLINE_SECONDS = 300


def _credential_payload(credential: LinearCredential) -> str:
    data: dict[str, Any] = {"kind": credential.kind, "access_token": credential.access_token.get_secret_value()}
    if credential.kind == "oauth":
        data |= {
            "refresh_token": credential.refresh_token.get_secret_value() if credential.refresh_token else None,
            "client_id": credential.client_id,
            "scopes": list(credential.scopes),
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
        }
    return json.dumps(data, indent=2, sort_keys=True)


@contextmanager
def _credential_lock(path: Path) -> Generator[None]:
    descriptor = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _token_request(params: dict[str, str], *, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    try:
        with httpx.Client(transport=transport, timeout=httpx.Timeout(10.0), follow_redirects=False) as client:
            response = client.post(TOKEN_ENDPOINT, data=params, headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError:
        raise RuntimeError("oauth_transient_failure") from None
    if response.status_code == 429 or response.status_code >= 500:
        raise RuntimeError("oauth_transient_failure")
    if response.status_code != 200:
        error = ""
        try:
            error = str(response.json().get("error", ""))
        except ValueError:
            pass
        if error == "invalid_grant":
            raise RuntimeError("oauth_reauthorization_required")
        raise RuntimeError("oauth_transient_failure")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("oauth_transient_failure") from None
    if not isinstance(payload, dict):
        raise RuntimeError("oauth_transient_failure")
    return payload


def _validate_token_payload(payload: dict[str, Any], *, require_refresh: bool) -> tuple[str, str | None, datetime]:
    """Strict success-response validation. Raises before any credential file change."""
    access = payload.get("access_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("oauth_response_invalid")
    if payload.get("token_type") not in (None, "Bearer"):
        raise RuntimeError("oauth_response_invalid")
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, bool) or not isinstance(expires_in, int) or not 0 < expires_in <= 5_000_000:
        raise RuntimeError("oauth_response_invalid")
    scope = payload.get("scope")
    if scope is not None:
        granted = set(scope.split()) if isinstance(scope, str) else set(scope) if isinstance(scope, list) else None
        if granted != {"read"}:
            raise RuntimeError("linear_credential_scope_invalid")
    refresh = payload.get("refresh_token")
    if refresh is not None and (not isinstance(refresh, str) or not refresh):
        raise RuntimeError("oauth_response_invalid")
    if require_refresh and refresh is None:
        raise RuntimeError("oauth_response_invalid")
    return access, refresh, datetime.now(timezone.utc) + timedelta(seconds=expires_in)


def _write_credential(path: Path, credential: LinearCredential) -> None:
    _write_secret(path, _credential_payload(credential))


def refresh_credential(path: Path, *, transport: httpx.BaseTransport | None = None) -> LinearCredential:
    """Return a valid OAuth credential, rotating via Linear's token endpoint when near expiry.

    The lock spans reread, expiry decision, token request, and atomic replacement so
    concurrent writers cannot lose a rotated refresh token.
    """
    with _credential_lock(path):
        credential = load_credential(path)
        if credential.kind != "oauth":
            raise ValueError("oauth_credential_required")
        if credential.expires_at is not None and credential.expires_at - datetime.now(timezone.utc) > REFRESH_MARGIN:
            return credential
        payload = _token_request(
            {"grant_type": "refresh_token", "refresh_token": credential.refresh_token.get_secret_value(), "client_id": credential.client_id},
            transport=transport,
        )
        access, refresh, expires_at = _validate_token_payload(payload, require_refresh=False)
        updated = LinearCredential(
            kind="oauth",
            access_token=access,
            refresh_token=refresh if refresh is not None else credential.refresh_token,
            client_id=credential.client_id,
            scopes=credential.scopes,
            expires_at=expires_at,
        )
        _write_credential(path, updated)
        return updated


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(client_id: str, *, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": "read",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - request lines carry code/state; never log them
        pass

    def _respond(self, status: int, body: str) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server: HTTPServer = self.server
        if urlsplit(self.path).path != CALLBACK_PATH:
            self._respond(404, "not found")
            return
        params = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
        states = [value for key, value in params if key == "state"]
        if len(states) != 1 or not states[0] or not hmac.compare_digest(states[0], server.expected_state):
            self._respond(400, "invalid state")
            return
        errors = [value for key, value in params if key == "error"]
        if errors:
            server.outcome = ("error", errors[0][:64])
            self._respond(400, "authorization failed")
            return
        codes = [value for key, value in params if key == "code"]
        if len(codes) != 1 or not codes[0]:
            self._respond(400, "invalid code")
            return
        server.outcome = ("code", codes[0])
        self._respond(200, "Authorization received. You may close this window.")


def oauth_login(
    path: Path,
    *,
    client_id: str,
    force: bool = False,
    transport: httpx.BaseTransport | None = None,
    announce: Callable[[str], None] = print,
    deadline_seconds: int = LOGIN_DEADLINE_SECONDS,
) -> LinearCredential:
    """Run the PKCE authorization flow against Linear and persist the read-only credential."""
    if path.exists() and not force:
        raise ValueError("linear_export_credential_exists")
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _CallbackHandler)
    server.expected_state = state
    server.outcome = None
    server.timeout = 1
    try:
        announce(build_authorize_url(client_id, state=state, challenge=challenge))
        deadline = time.monotonic() + deadline_seconds
        while server.outcome is None and time.monotonic() < deadline:
            server.handle_request()
        if server.outcome is None:
            raise RuntimeError("oauth_login_timeout")
        kind, value = server.outcome
        if kind == "error":
            raise RuntimeError(f"oauth_authorization_denied:{value}")
        payload = _token_request(
            {"grant_type": "authorization_code", "code": value, "redirect_uri": REDIRECT_URI, "client_id": client_id, "code_verifier": verifier},
            transport=transport,
        )
        access, refresh, expires_at = _validate_token_payload(payload, require_refresh=True)
        credential = LinearCredential(kind="oauth", access_token=access, refresh_token=refresh, client_id=client_id, scopes=("read",), expires_at=expires_at)
        with _credential_lock(path):
            if path.exists() and not force:
                raise ValueError("linear_export_credential_exists")
            _write_credential(path, credential)
        return credential
    finally:
        server.server_close()
