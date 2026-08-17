from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256 as bytes_sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
import json
import re
import shutil
import sys

import httpx
import pytest

import omp_work.__main__ as main_module
import omp_work.integration.exporter as exporter_module
import omp_work.operations.cli as operations_cli
from omp_work.integration.exporter import (
    ArtifactRecord,
    CursorRecord,
    ExportLedger,
    ExportManifest,
    ExportRun,
    LinearExporter,
    SourceHashIndex,
    WORKFLOW_PREFIXES,
    load_manifest,
)
from omp_work.integration.linear import LinearClient, LinearCredential, LinearStream, QUERIES, load_credential
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import sha256
from omp_work.v1.models import Anomaly


ISSUE_A = "00000000-0000-7000-8000-000000000145"
ISSUE_B = "00000000-0000-7000-8000-000000000146"


def credential() -> LinearCredential:
    return LinearCredential(kind="oauth", access_token="secret", refresh_token="refresh", client_id="test-client", scopes=("read",), expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


@pytest.mark.parametrize("stream", list(LinearStream))
def test_each_static_query_traverses_relay_pages_with_exact_variables(stream: LinearStream) -> None:
    requests: list[dict[str, object]] = []
    filter_value = None if stream in {LinearStream.initiative_projects, LinearStream.relations, LinearStream.attachments} else {"team": {"key": {"eq": "HOME"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["query"] == QUERIES[stream]
        expected = {"first": 50, "after": None if len(requests) == 1 else "cursor-1"}
        if filter_value is not None:
            expected["filter"] = filter_value
        assert payload["variables"] == expected
        return httpx.Response(
            200,
            json={
                "data": {
                    stream.value: {
                        "nodes": [{"id": f"node-{len(requests)}"}],
                        "pageInfo": {"hasNextPage": len(requests) == 1, "endCursor": "cursor-1" if len(requests) == 1 else None},
                    }
                }
            },
        )

    client = LinearClient(credential(), transport=httpx.MockTransport(handler))
    try:
        pages = list(client.pages(stream, filter=filter_value))
    finally:
        client.close()
    assert [node["id"] for _, nodes, _, _, _ in pages for node in nodes] == ["node-1", "node-2"]
    assert all(str(request["query"]).startswith(f"query {stream.value}") for request in requests)
    assert all("mutation" not in str(request["query"]).lower() for request in requests)


def test_static_queries_only_use_supported_archive_and_filter_arguments() -> None:
    archived = {LinearStream.teams, LinearStream.initiatives, LinearStream.projects, LinearStream.milestones, LinearStream.issues, LinearStream.states, LinearStream.labels}
    unfilterable = {LinearStream.initiative_projects, LinearStream.relations, LinearStream.attachments}
    forbidden = {"email", "profile", "workspace"}
    assert "users" not in LinearStream.__members__
    for stream, query in QUERIES.items():
        assert query.startswith(f"query {stream.value}")
        assert ("includeArchived: true" in query) is (stream in archived)
        assert ("$filter:" in query) is (stream not in unfilterable)
        assert not forbidden.intersection(re.findall(r"\b[a-zA-Z]+\b", query))
    assert " teams { nodes { key } }" not in QUERIES[LinearStream.initiatives]
    assert " teams { nodes { key } }" in QUERIES[LinearStream.projects]
    assert "accessibleTeams {" not in QUERIES[LinearStream.projects]
    assert "projectMilestone { id }" in QUERIES[LinearStream.issues]
    assert " milestone { id }" not in QUERIES[LinearStream.issues]
    assert "status { id name type }" in QUERIES[LinearStream.projects]


def test_home_filters_are_stream_specific_and_bounded() -> None:
    lower = datetime(2026, 8, 14, tzinfo=timezone.utc)
    upper = datetime(2026, 8, 15, tzinfo=timezone.utc)
    assert LinearExporter._filter(LinearStream.teams, lower, upper) == {"key": {"eq": "HOME"}, "updatedAt": {"gte": lower.isoformat(), "lte": upper.isoformat()}}
    offset_lower = lower.astimezone(timezone(timedelta(hours=-4)))
    offset_filter = LinearExporter._filter(LinearStream.issues, offset_lower, None)
    assert offset_filter is not None
    assert offset_filter["updatedAt"] == {"gte": lower.isoformat()}
    assert LinearExporter._filter(LinearStream.initiatives, None, None) == {"teams": {"some": {"key": {"eq": "HOME"}}}}
    assert LinearExporter._filter(LinearStream.projects, None, None) == {"accessibleTeams": {"some": {"key": {"eq": "HOME"}}}}
    assert LinearExporter._filter(LinearStream.project_updates, None, None) == {"project": {"accessibleTeams": {"some": {"key": {"eq": "HOME"}}}}}
    assert LinearExporter._filter(LinearStream.milestones, None, None) == {"project": {"accessibleTeams": {"some": {"key": {"eq": "HOME"}}}}}
    assert LinearExporter._filter(LinearStream.comments, None, None) == {
        "issue": {"team": {"key": {"eq": "HOME"}}},
        "or": [{"body": {"startsWith": prefix}} for prefix in WORKFLOW_PREFIXES],
    }
    for stream in (LinearStream.initiative_projects, LinearStream.relations, LinearStream.attachments):
        assert LinearExporter._filter(stream, lower, upper) is None


@pytest.mark.parametrize("scopes", [(), ("read", "write"), ("admin",)])
def test_credential_rejects_any_scope_other_than_exact_read(scopes: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="linear_credential_scope_invalid"):
        LinearCredential(kind="oauth", access_token="secret", scopes=scopes, expires_at=datetime.now(timezone.utc) + timedelta(hours=1))


@pytest.mark.parametrize(
    ("access_token", "scopes", "expires_at"),
    [
        ("not-a-personal-key", (), None),
        ("lin_api_secret", ("read",), None),
        ("lin_api_secret", (), datetime.now(timezone.utc) + timedelta(hours=1)),
    ],
)
def test_personal_api_key_rejects_invalid_shape(access_token: str, scopes: tuple[str, ...], expires_at: datetime | None) -> None:
    with pytest.raises(ValueError, match="linear_credential_invalid"):
        LinearCredential(kind="api_key", access_token=access_token, scopes=scopes, expires_at=expires_at)


@pytest.mark.parametrize(
    ("value", "authorization"),
    [
        (credential(), "Bearer secret"),
        (LinearCredential(kind="api_key", access_token="lin_api_secret"), "lin_api_secret"),
    ],
)
def test_client_uses_credential_specific_authorization(value: LinearCredential, authorization: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == authorization
        return httpx.Response(200, json={"data": {"teams": {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}}})

    client = LinearClient(value, transport=httpx.MockTransport(handler))
    try:
        list(client.pages(LinearStream.teams))
    finally:
        client.close()


def test_credential_file_failures_are_fixed_and_redacted(tmp_path: Path) -> None:
    path = tmp_path / "linear-export.json"
    with pytest.raises(ValueError, match="^linear_credential_missing$"):
        load_credential(path)
    path.write_text(json.dumps({"kind": "oauth", "access_token": "do-not-leak", "refresh_token": "refresh-token", "client_id": "test-client", "scopes": ["admin"], "expires_at": "2099-01-01T00:00:00Z"}))
    path.chmod(0o600)
    with pytest.raises(ValueError) as captured:
        load_credential(path)
    assert str(captured.value) == "linear_credential_scope_invalid"
    assert "do-not-leak" not in str(captured.value)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="^linear_credential_permissions_invalid$"):
        load_credential(path)


def test_client_maps_auth_and_graphql_failures_to_fixed_errors() -> None:
    for status, code in ((401, "linear_credential_invalid"), (403, "linear_credential_permission_denied")):
        client = LinearClient(credential(), transport=httpx.MockTransport(lambda _: httpx.Response(status)))
        try:
            with pytest.raises(RuntimeError, match=f"^{code}$"):
                list(client.pages(LinearStream.teams))
        finally:
            client.close()
    for message, code in (("permission denied private detail", "linear_credential_permission_denied"), ("Cannot query field private detail", "linear_transport_failed")):
        client = LinearClient(credential(), transport=httpx.MockTransport(lambda _, value=message: httpx.Response(200, json={"errors": [{"message": value}]})))
        try:
            with pytest.raises(RuntimeError) as captured:
                list(client.pages(LinearStream.teams))
        finally:
            client.close()
        assert str(captured.value) == code
        assert "private detail" not in str(captured.value)


def test_normalization_deeply_removes_forbidden_user_and_workspace_fields() -> None:
    raw = {
        "id": "project-1",
        "name": "Project",
        "workspace": {"id": "secret-workspace"},
        "lead": {"id": "user-1", "name": "User", "displayName": "Display", "active": True, "email": "secret@example.com", "profile": {"bio": "secret"}},
        "teams": {"nodes": [{"key": "HOME", "workspace": "secret"}]},
        "status": {"id": "status-1", "name": "Planned", "type": "planned", "description": "secret"},
    }
    normalized = LinearExporter._normalize(LinearStream.projects, raw)
    encoded = json.dumps(normalized, sort_keys=True)
    assert "secret@example.com" not in encoded
    assert "secret-workspace" not in encoded
    assert "profile" not in encoded
    assert normalized["lead"] == {"id": "user-1", "name": "User", "displayName": "Display", "active": True}
    assert normalized["teams"] == {"nodes": [{"key": "HOME"}]}
    assert normalized["status"] == {"id": "status-1", "name": "Planned", "type": "planned"}


def test_unfilterable_scope_uses_discovered_home_endpoints() -> None:
    scope = {"initiatives": {"initiative-home"}, "projects": {"project-home"}, "issues": {ISSUE_A}}
    assert LinearExporter._in_scope(LinearStream.initiative_projects, {"initiative": {"id": "other"}, "project": {"id": "project-home"}}, scope)
    assert LinearExporter._in_scope(LinearStream.relations, {"issue": {"id": "other"}, "relatedIssue": {"id": ISSUE_A}}, scope)
    assert LinearExporter._in_scope(LinearStream.attachments, {"issue": {"id": ISSUE_A}}, scope)
    assert not LinearExporter._in_scope(LinearStream.relations, {"issue": {"id": "other"}, "relatedIssue": {"id": "other-2"}}, scope)


class MemoryLedger:
    runs: dict[UUID, ExportRun] = {}
    cursors: dict[tuple[UUID, str], list[CursorRecord]] = defaultdict(list)
    fail_commit_once = False

    def __init__(self, config: OperationsConfig, workspace_id: UUID) -> None:
        self.config = config
        self.workspace_id = workspace_id

    @classmethod
    def reset(cls) -> None:
        cls.runs = {}
        cls.cursors = defaultdict(list)
        cls.fail_commit_once = False

    def start(self, export_id: UUID, mode: str, base_export_id: UUID | None, root: str, started_at: datetime, lower: datetime | None) -> None:
        self.runs[export_id] = ExportRun(
            export_id=export_id,
            workspace_id=self.workspace_id,
            mode=mode,
            base_export_id=base_export_id,
            state="running",
            source_started_at=started_at,
            source_lower_bound=lower,
            source_boundary=None,
            storage_root=root,
        )

    def run(self, export_id: UUID) -> ExportRun | None:
        return self.runs.get(export_id)

    def latest_complete(self) -> ExportRun | None:
        candidates = [run for run in self.runs.values() if run.workspace_id == self.workspace_id and run.state == "complete"]
        return candidates[-1] if candidates else None

    def committed(self, export_id: UUID, stream: str) -> list[CursorRecord]:
        return list(self.cursors[(export_id, stream)])

    def commit(self, export_id: UUID, page: exporter_module.SourcePage, scanned_count: int, retained_count: int, cumulative_count: int, artifact: ArtifactRecord) -> None:
        if type(self).fail_commit_once:
            type(self).fail_commit_once = False
            raise RuntimeError("simulated_database_failure")
        self.cursors[(export_id, page.stream)].append(
            CursorRecord(
                page_index=page.page_index,
                request_cursor=page.request_cursor,
                end_cursor=page.end_cursor,
                has_next_page=page.has_next_page,
                scanned_count=scanned_count,
                retained_count=retained_count,
                cumulative_count=cumulative_count,
                plaintext_sha256=artifact.plaintext_sha256,
                ciphertext_sha256=artifact.ciphertext_sha256,
                artifact_path=artifact.path,
                variables_sha256=artifact.variables_sha256 or "",
            )
        )

    def set_boundary(self, export_id: UUID, boundary: datetime) -> None:
        run = self.runs[export_id]
        if run.source_boundary is not None:
            raise RuntimeError("pagination_count_hash_gap")
        self.runs[export_id] = run.model_copy(update={"source_boundary": boundary})

    def finalize(self, export_id: UUID, boundary: datetime, raw_hash: str, manifest_hash: str, blocked: bool) -> None:
        run = self.runs[export_id]
        assert run.source_boundary == boundary and run.state == "running"
        self.runs[export_id] = run.model_copy(update={"state": "blocked" if blocked else "complete"})


def _copy_encrypt(source: Path, destination: Path, passphrase_file: Path, *, mode: int = 0o600) -> str:
    del passphrase_file
    if destination.exists():
        raise FileExistsError("immutable artifact already exists")
    destination.write_bytes(source.read_bytes())
    destination.chmod(mode)
    return bytes_sha256(destination.read_bytes()).hexdigest()


def _copy_decrypt(source: Path, destination: Path, passphrase_file: Path, *, mode: int = 0o600) -> None:
    del passphrase_file
    if destination.exists():
        raise FileExistsError("immutable artifact already exists")
    destination.write_bytes(source.read_bytes())
    destination.chmod(mode)


@pytest.fixture
def export_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OperationsConfig:
    credentials = tmp_path / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700)
    linear = credentials / "linear-export.json"
    linear.write_text(json.dumps({"kind": "oauth", "access_token": "secret-token", "refresh_token": "refresh-token", "client_id": "test-client", "scopes": ["read"], "expires_at": "2099-01-01T00:00:00Z"}))
    linear.chmod(0o600)
    for name, value in (("gpg-passphrase", "passphrase"), ("operator-actor-id", str(uuid4()))):
        path = credentials / name
        path.write_text(value)
        path.chmod(0o600)
    config = OperationsConfig(config_dir=tmp_path / "config", state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    MemoryLedger.reset()
    monkeypatch.setattr(exporter_module, "ExportLedger", MemoryLedger)
    monkeypatch.setattr(exporter_module, "encrypt_file", _copy_encrypt)
    monkeypatch.setattr(exporter_module, "decrypt_file", _copy_decrypt)
    return config


def _nodes(operation: str, variables: dict[str, object], call_index: int = 0) -> list[dict[str, object]]:
    filter_value = variables.get("filter")
    bounded = isinstance(filter_value, dict) and "updatedAt" in filter_value
    updated = str(filter_value["updatedAt"]["gte"]) if bounded else "2026-08-01T00:00:00+00:00"
    issue_title = "new title" if bounded else "old title"
    values: dict[str, list[dict[str, object]]] = {
        "teams": [{"id": "team-home", "key": "HOME", "name": "Home", "updatedAt": updated}],
        "initiatives": [{"id": "initiative-home", "name": "Initiative", "updatedAt": updated}],
        "projects": [{"id": "project-home", "name": "Project", "updatedAt": updated, "teams": {"nodes": [{"key": "HOME"}]}, "lead": {"id": "user-lead", "name": "Lead", "displayName": "Lead", "active": True, "email": "forbidden@example.com"}}],
        "projectUpdates": [{"id": "update-1", "body": "Update", "updatedAt": updated, "project": {"id": "project-home"}, "user": {"id": "user-update", "name": "Updater", "displayName": "Updater", "active": True}}],
        "projectMilestones": [{"id": "milestone-1", "name": "Milestone", "updatedAt": updated, "project": {"id": "project-home"}}],
        "issues": [{"id": ISSUE_A, "identifier": "HOME-145", "title": issue_title, "updatedAt": updated, "team": {"key": "HOME"}, "state": {"id": "state-1", "name": "Open", "type": "started"}, "labels": {"nodes": [{"id": "label-1", "name": "Queue"}]}, "project": {"id": "project-home"}, "projectMilestone": {"id": "milestone-1"}, "assignee": {"id": "user-assignee", "name": "Assignee", "displayName": "Assignee", "active": True}, "creator": {"id": "user-creator", "name": "Creator", "displayName": "Creator", "active": True}, "workspace": {"id": "forbidden-workspace"}}],
        "workflowStates": [{"id": "state-1", "name": "Open", "type": "started", "updatedAt": updated, "team": {"key": "HOME"}}],
        "issueLabels": [{"id": "label-1", "name": "Queue", "updatedAt": updated, "team": {"key": "HOME"}}],
        "initiativeToProjects": [{"id": "initiative-project-1", "updatedAt": updated, "initiative": {"id": "initiative-home"}, "project": {"id": "project-home"}}],
        "issueRelations": [{"id": "relation-1", "type": "related", "updatedAt": updated, "issue": {"id": ISSUE_A, "identifier": "HOME-145", "team": {"key": "HOME"}}, "relatedIssue": {"id": ISSUE_A, "identifier": "HOME-145", "team": {"key": "HOME"}}}],
        "comments": [{"id": "comment-1", "body": "**Plan approved**\n- SHA-256: `" + "a" * 64 + "`", "updatedAt": updated, "createdAt": updated, "issue": {"id": ISSUE_A, "identifier": "HOME-145", "team": {"key": "HOME"}}, "user": {"id": "user-creator", "name": "Creator", "displayName": "Creator", "active": True}}],
        "attachments": [{"id": "attachment-1", "title": "Evidence", "url": "https://example.test/evidence", "updatedAt": updated, "issue": {"id": ISSUE_A, "identifier": "HOME-145", "team": {"key": "HOME"}}, "creator": {"id": "user-creator", "name": "Creator", "displayName": "Creator", "active": True}}],
    }
    return values[operation]


class Router:
    def __init__(self, nodes=_nodes) -> None:
        self.nodes = nodes
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.counts: dict[str, int] = defaultdict(int)

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        operation = re.match(r"query (\w+)", payload["query"]).group(1)
        variables = payload["variables"]
        self.calls.append((operation, variables))
        call_index = self.counts[operation]
        self.counts[operation] += 1
        return httpx.Response(200, json={"data": {operation: {"nodes": self.nodes(operation, variables, call_index), "pageInfo": {"hasNextPage": False, "endCursor": None}}}})


def test_full_baseline_overlap_converges_and_artifacts_are_private(export_config: OperationsConfig) -> None:
    router = Router()
    manifest = LinearExporter(export_config, transport=httpx.MockTransport(router.handler)).full(uuid4())
    assert manifest.mode == "full"
    assert manifest.source_hashes.work_items[ISSUE_A].record_sha256 == sha256(LinearExporter._normalize(LinearStream.issues, _nodes("issues", {"filter": {"updatedAt": {"gte": manifest.source_started_at.isoformat()}}})[0]))
    assert manifest.source_hashes.surfaces["project-home"].record_sha256 != manifest.source_hashes.surfaces["project-home"].source_sha256
    assert manifest.source_hashes.project_updates["update-1"].artifact_ref.endswith(".json.gpg")
    assert manifest.source_hashes.users["user-lead"].artifact_ref == manifest.source_hashes.surfaces["project-home"].artifact_ref
    assert manifest.artifacts["manifest"].path.startswith("linear-exports/")
    root = export_config.data_dir / "linear-exports" / str(manifest.workspace_id) / str(manifest.export_id)
    assert root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o400 for path in root.iterdir())
    assert not (export_config.state_dir / "staging" / str(manifest.export_id)).exists()
    serialized = json.dumps(manifest.model_dump(mode="json"), sort_keys=True)
    assert "secret-token" not in serialized
    assert "forbidden@example.com" not in serialized
    assert "forbidden-workspace" not in serialized
    loaded = load_manifest(export_config, manifest.export_id)
    assert loaded.manifest_sha256 == manifest.manifest_sha256
    assert loaded.artifacts["manifest"].path == manifest.artifacts["manifest"].path
    assert any("updatedAt" not in variables.get("filter", {}) for operation, variables in router.calls if operation == "issues")
    assert any("updatedAt" in variables.get("filter", {}) for operation, variables in router.calls if operation == "issues")
    for operation, variables in router.calls:
        if operation in {"initiativeToProjects", "issueRelations", "attachments"}:
            assert "filter" not in variables


def test_delta_preserves_base_index_and_defers_above_upper_bound(export_config: OperationsConfig) -> None:
    workspace_id = uuid4()
    base = LinearExporter(export_config, transport=httpx.MockTransport(Router().handler)).full(workspace_id)

    def delta_nodes(operation: str, variables: dict[str, object], call_index: int) -> list[dict[str, object]]:
        del call_index
        if operation == "issues":
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            return [{"id": ISSUE_A, "identifier": "HOME-145", "title": "future", "updatedAt": future, "team": {"key": "HOME"}}]
        if operation in {"initiativeToProjects", "issueRelations", "attachments"}:
            return _nodes(operation, variables)
        return []

    delta = LinearExporter(export_config, transport=httpx.MockTransport(Router(delta_nodes).handler)).delta(workspace_id)
    assert delta.base_export_id == base.export_id
    assert delta.source_lower_bound == base.source_boundary
    assert set(delta.source_hashes.worlds) == set(base.source_hashes.worlds)
    assert delta.source_hashes.work_items[ISSUE_A] == base.source_hashes.work_items[ISSUE_A]
    assert delta.dimension_counts == base.dimension_counts


def test_equal_timestamp_different_hash_blocks_without_fragmenting_base() -> None:
    exporter = LinearExporter.__new__(LinearExporter)
    anomalies: list[Anomaly] = []
    records = defaultdict(
        list,
        {
            LinearStream.issues: [
                {"id": ISSUE_A, "identifier": "HOME-145", "title": "first", "updatedAt": "2026-08-15T00:00:00Z"},
                {"id": ISSUE_A, "identifier": "HOME-145", "title": "second", "updatedAt": "2026-08-15T00:00:00Z"},
            ]
        },
    )
    index, _ = exporter._indexes(records, {}, anomalies=anomalies)
    assert set(index.work_items) == {ISSUE_A}
    assert anomalies == [Anomaly(code="pagination_count_hash_gap", disposition="blocking")]


def test_logical_hashes_ignore_page_boundaries_but_raw_hash_tracks_pages() -> None:
    exporter = LinearExporter.__new__(LinearExporter)
    records = defaultdict(list, {LinearStream.issues: [{"id": ISSUE_A, "identifier": "HOME-145", "updatedAt": "2026-08-15T00:00:00Z"}]})
    first, _ = exporter._indexes(records, {}, {(LinearStream.issues, ISSUE_A): "page-a"})
    second, _ = exporter._indexes(records, {}, {(LinearStream.issues, ISSUE_A): "page-b"})
    logical_first = sha256([{"id": entry.id, "record_sha256": entry.record_sha256} for entry in first.work_items.values()])
    logical_second = sha256([{"id": entry.id, "record_sha256": entry.record_sha256} for entry in second.work_items.values()])
    assert logical_first == logical_second
    assert sha256({"source_hashes": first.model_dump(mode="json"), "pages": ["a"]}) != sha256({"source_hashes": second.model_dump(mode="json"), "pages": ["b"]})
    assert first.work_items[ISSUE_A].artifact_ref == "page-a"
    assert second.work_items[ISSUE_A].artifact_ref == "page-b"


def test_resume_skips_committed_page_and_starts_at_saved_cursor(export_config: OperationsConfig) -> None:
    class InterruptingRouter(Router):
        interrupted = False

        def handler(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            operation = re.match(r"query (\w+)", payload["query"]).group(1)
            variables = payload["variables"]
            self.calls.append((operation, variables))
            if operation == "teams" and variables["after"] is None:
                return httpx.Response(200, json={"data": {operation: {"nodes": _nodes(operation, variables), "pageInfo": {"hasNextPage": True, "endCursor": "saved-cursor"}}}})
            if operation == "teams" and not self.interrupted:
                self.interrupted = True
                raise httpx.ConnectError("interrupt", request=request)
            return httpx.Response(200, json={"data": {operation: {"nodes": _nodes(operation, variables), "pageInfo": {"hasNextPage": False, "endCursor": None}}}})

    workspace_id = uuid4()
    router = InterruptingRouter()
    exporter = LinearExporter(export_config, transport=httpx.MockTransport(router.handler))
    with pytest.raises(RuntimeError, match="linear_transport_failed"):
        exporter.full(workspace_id)
    export_id = next(iter(MemoryLedger.runs))
    assert MemoryLedger.runs[export_id].state == "running"
    manifest = exporter.resume(export_id)
    team_calls = [variables["after"] for operation, variables in router.calls if operation == "teams"]
    assert team_calls[:3] == [None, "saved-cursor", "saved-cursor"]
    assert team_calls.count(None) == 2
    assert manifest.source_hashes.work_items[ISSUE_A].key == "HOME-145"


def test_encrypt_before_cursor_crash_reuses_matching_immutable_page(export_config: OperationsConfig) -> None:
    workspace_id = uuid4()
    MemoryLedger.fail_commit_once = True
    router = Router()
    exporter = LinearExporter(export_config, transport=httpx.MockTransport(router.handler))
    with pytest.raises(RuntimeError, match="simulated_database_failure"):
        exporter.full(workspace_id)
    export_id = next(iter(MemoryLedger.runs))
    root = export_config.data_dir / MemoryLedger.runs[export_id].storage_root
    first_artifact = next(root.iterdir())
    before = bytes_sha256(first_artifact.read_bytes()).hexdigest()
    manifest = exporter.resume(export_id)
    assert bytes_sha256(first_artifact.read_bytes()).hexdigest() == before
    assert manifest.export_id == export_id


def test_resume_rejects_committed_variables_mismatch_and_keeps_run_running(export_config: OperationsConfig) -> None:
    workspace_id = uuid4()
    router = Router()
    exporter = LinearExporter(export_config, transport=httpx.MockTransport(router.handler))
    MemoryLedger.fail_commit_once = True
    with pytest.raises(RuntimeError):
        exporter.full(workspace_id)
    export_id = next(iter(MemoryLedger.runs))
    artifact = next((export_config.data_dir / MemoryLedger.runs[export_id].storage_root).iterdir())
    payload = json.loads(artifact.read_text())
    page = exporter_module.SourcePage.model_validate(payload)
    MemoryLedger.cursors[(export_id, page.stream)] = [
        CursorRecord(
            page_index=0,
            request_cursor=None,
            end_cursor=page.end_cursor,
            has_next_page=page.has_next_page,
            scanned_count=len(page.nodes),
            retained_count=len(page.nodes),
            cumulative_count=len(page.nodes),
            plaintext_sha256=sha256(payload),
            ciphertext_sha256=bytes_sha256(artifact.read_bytes()).hexdigest(),
            artifact_path=str(artifact.relative_to(export_config.data_dir)),
            variables_sha256="0" * 64,
        )
    ]
    with pytest.raises(RuntimeError, match="^pagination_count_hash_gap$"):
        exporter.resume(export_id)
    assert MemoryLedger.runs[export_id].state == "running"


def test_all_blocking_and_quarantined_anomaly_codes_are_observable() -> None:
    plan_a = "a" * 64
    plan_b = "b" * 64
    records = defaultdict(
        list,
        {
            LinearStream.initiative_projects: [{"id": "link", "initiative": {"id": "missing"}, "project": {"id": "missing"}}],
            LinearStream.labels: [{"id": "now", "name": "NOW"}],
            LinearStream.issues: [
                {"id": ISSUE_A, "identifier": "HOME-145", "labels": {"nodes": [{"id": "now"}]}, "parent": {"id": ISSUE_B}},
                {"id": ISSUE_B, "identifier": "HOME-145", "labels": {"nodes": [{"id": "now"}]}, "parent": {"id": ISSUE_A}},
            ],
            LinearStream.relations: [{"id": "unknown", "type": "unknown-type", "issue": {"id": ISSUE_A}, "relatedIssue": {"id": ISSUE_B}}],
            LinearStream.comments: [
                {"id": "plan", "createdAt": "2026-08-01T00:00:00Z", "body": f"**Plan approved**\n- SHA-256: `{plan_a}`", "issue": {"id": ISSUE_A}},
                {"id": "review", "createdAt": "2026-08-02T00:00:00Z", "body": f"**Session review**\nPlan SHA-256: `{plan_b}`", "issue": {"id": ISSUE_A}},
            ],
            LinearStream.attachments: [{"id": "attachment", "issue": {"id": ISSUE_A}}],
        },
    )
    codes = {(item.code, item.disposition) for item in LinearExporter._anomalies(records, set())}
    assert {
        ("duplicate_uuid_key_mapping", "blocking"),
        ("missing_relation_endpoint", "blocking"),
        ("relation_cycle", "blocking"),
        ("multiple_focus_slots", "blocking"),
        ("legacy_authority_claim", "blocking"),
        ("attachment_content_unavailable", "quarantined"),
        ("unsupported_non_workflow_object", "quarantined"),
    } <= codes


def test_authority_hashes_are_compared_within_each_issue() -> None:
    plan_a = "a" * 64
    plan_b = "b" * 64
    records = defaultdict(
        list,
        {
            LinearStream.comments: [
                {"id": "plan-a", "createdAt": "2026-08-01T00:00:00Z", "body": f"**Plan approved**\n- SHA-256: `{plan_a}`", "issue": {"id": ISSUE_A}},
                {"id": "plan-b", "createdAt": "2026-08-02T00:00:00Z", "body": f"**Plan approved**\n- SHA-256: `{plan_b}`", "issue": {"id": ISSUE_B}},
                {"id": "review-a", "createdAt": "2026-08-03T00:00:00Z", "body": f"**Session review**\nPlan SHA-256: `{plan_a}`", "issue": {"id": ISSUE_A}},
            ]
        },
    )
    assert not any(item.code == "legacy_authority_claim" for item in LinearExporter._anomalies(records, set()))


def test_cli_exits_two_only_for_blocking_anomalies(export_config: OperationsConfig, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    complete = LinearExporter(export_config, transport=httpx.MockTransport(Router().handler)).full(uuid4())

    class StubExporter:
        manifest: ExportManifest

        def __init__(self, config: OperationsConfig) -> None:
            del config

        def full(self, workspace_id: UUID) -> ExportManifest:
            del workspace_id
            return self.manifest

    monkeypatch.setattr(operations_cli, "LinearExporter", StubExporter)
    arguments = SimpleNamespace(ops_command="linear-export", linear_export_command="full", workspace_id=str(complete.workspace_id))
    StubExporter.manifest = complete.model_copy(update={"anomalies": (Anomaly(code="attachment_content_unavailable", disposition="quarantined"),)})
    operations_cli.run(arguments, export_config)
    summary = json.loads(capsys.readouterr().out)
    assert summary["state"] == "complete"
    assert not Path(summary["manifest_path"]).is_absolute()
    StubExporter.manifest = complete.model_copy(update={"anomalies": (Anomaly(code="missing_relation_endpoint", disposition="blocking"),)})
    with pytest.raises(SystemExit) as captured:
        operations_cli.run(arguments, export_config)
    assert captured.value.code == 2
    assert json.loads(capsys.readouterr().out)["state"] == "blocked"


def test_ops_cli_preserves_fixed_errors_and_redacts_unexpected_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["omp-work", "ops", "check"])
    for error, expected in (
        (ValueError("linear_credential_missing"), "linear_credential_missing"),
        (RuntimeError("/home/operator gpg --passphrase secret"), "operation_failed"),
    ):
        def fail(_: object, value: Exception = error) -> None:
            raise value

        monkeypatch.setattr(main_module.operations_cli, "run", fail)
        with pytest.raises(SystemExit) as captured:
            main_module.main()
        assert captured.value.code == expected
