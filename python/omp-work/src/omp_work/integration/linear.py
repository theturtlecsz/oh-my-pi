from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import json
import stat
from typing import Any, Iterator, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator, model_validator


class LinearCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["oauth", "api_key"]
    access_token: SecretStr
    scopes: tuple[str, ...] = ()
    expires_at: datetime | None = None

    @field_validator("access_token")
    @classmethod
    def nonempty_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value():
            raise ValueError("linear_credential_invalid")
        return value

    @model_validator(mode="after")
    def valid_authentication_mode(self) -> Self:
        if self.kind == "api_key":
            if not self.access_token.get_secret_value().startswith("lin_api_") or self.scopes or self.expires_at is not None:
                raise ValueError("linear_credential_invalid")
        elif set(self.scopes) != {"read"}:
            raise ValueError("linear_credential_scope_invalid")
        elif self.expires_at is None or self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None or self.expires_at <= datetime.now(timezone.utc):
            raise ValueError("linear_credential_expired")
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
        detail = str(error)
        for code in ("linear_credential_expired", "linear_credential_scope_invalid"):
            if code in detail:
                raise ValueError(code) from None
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
