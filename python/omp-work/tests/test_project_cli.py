"""OMP-418: unit tests for owner-run projects CLI."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.project_cli import check, run_projects, seed, show
from omp_work.project_store import ProjectNotFound

REPO_ROOT = Path(__file__).resolve().parents[3]
MEDIA_DISCOVERY_JSON = REPO_ROOT / "infra" / "work-ledger" / "projects" / "media-discovery.json"
LINEAR_IMPORT_MAP_JSON = REPO_ROOT / "infra" / "work-ledger" / "linear-import-map.json"


class FakeStore:
    def __init__(self) -> None:
        self.ensure_calls: list[dict[str, Any]] = []
        self.update_profile_calls: list[dict[str, Any]] = []
        self.find_project_calls: list[dict[str, Any]] = []
        self.read_project_calls: list[dict[str, Any]] = []
        self.count_missing_calls: list[dict[str, Any]] = []
        self.projects_by_key: dict[str, dict[str, Any]] = {}
        self.projects_by_id: dict[UUID, dict[str, Any]] = {}
        self.missing_records_count: int = 0
        self.total_projects_count: int = 0

    def ensure_project(
        self, workspace_id: UUID, actor_id: UUID, key: str, name: str, kind: str
    ) -> UUID:
        project_id = uuid4()
        self.ensure_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "key": key,
                "name": name,
                "kind": kind,
                "project_id": project_id,
            }
        )
        if key in self.projects_by_key:
            return UUID(str(self.projects_by_key[key]["project_id"]))
        record = {
            "project_id": str(project_id),
            "key": key,
            "name": name,
            "kind": kind,
        }
        self.projects_by_key[key] = record
        self.projects_by_id[project_id] = record
        return project_id

    def update_profile(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        *,
        purpose: str = "",
        goals: Iterable[str] = (),
        questions: Iterable[str] = (),
        refs: Iterable[dict[str, str]] = (),
        repositories: Iterable[dict[str, object]] = (),
    ) -> None:
        self.update_profile_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "project_id": project_id,
                "purpose": purpose,
                "goals": list(goals),
                "questions": list(questions),
                "refs": list(refs),
                "repositories": list(repositories),
            }
        )

    def find_project(
        self, workspace_id: UUID, actor_id: UUID, key: str
    ) -> dict[str, Any]:
        self.find_project_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "key": key,
            }
        )
        if key not in self.projects_by_key:
            raise ProjectNotFound(f"with key {key}")
        return self.projects_by_key[key]

    def read_project(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, Any]:
        self.read_project_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "project_id": project_id,
            }
        )
        if project_id not in self.projects_by_id:
            raise ProjectNotFound(f"id {project_id}")
        return self.projects_by_id[project_id]

    def count_missing_records(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[int, int]:
        self.count_missing_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
            }
        )
        return self.total_projects_count, self.missing_records_count


def test_seed_real_media_discovery(capsys: pytest.CaptureFixture[str]) -> None:
    assert MEDIA_DISCOVERY_JSON.is_file(), f"missing seed file {MEDIA_DISCOVERY_JSON}"
    import_map = json.loads(LINEAR_IMPORT_MAP_JSON.read_text(encoding="utf-8"))
    expected_url = import_map["repositories"]["media-discovery"]["url"]

    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()

    code = seed(store, workspace_id, actor_id, MEDIA_DISCOVERY_JSON)
    assert code == 0

    assert len(store.ensure_calls) == 1
    ensure = store.ensure_calls[0]
    assert ensure["workspace_id"] == workspace_id
    assert ensure["actor_id"] == actor_id
    assert ensure["key"] == "media-discovery"
    assert ensure["name"] == "Media Discovery"
    assert ensure["kind"] == "world"

    assert len(store.update_profile_calls) == 1
    update = store.update_profile_calls[0]
    assert update["workspace_id"] == workspace_id
    assert update["actor_id"] == actor_id
    assert update["project_id"] == ensure["project_id"]
    assert update["goals"] == []

    repositories = update["repositories"]
    assert len(repositories) == 1
    assert repositories[0]["key"] == "media-discovery"
    assert repositories[0]["name"] == "media-discovery"
    assert repositories[0]["url"] == expected_url
    assert repositories[0]["default_branch"] == "main"
    assert repositories[0]["protected_branches"] == ["main"]
    assert repositories[0]["automation_ci_secret_free"] is False

    out = json.loads(capsys.readouterr().out)
    assert out == {"project_id": str(ensure["project_id"])}


def test_seed_missing_key_exits_2_no_store_call(tmp_path: Path) -> None:
    bad_file = tmp_path / "no_key.json"
    bad_file.write_text(
        json.dumps(
            {
                "name": "Media Discovery",
                "kind": "world",
                "repositories": [
                    {
                        "key": "media-discovery",
                        "name": "media-discovery",
                        "url": "https://example.test/repo.git",
                        "default_branch": "main",
                        "protected_branches": ["main"],
                        "automation_ci_secret_free": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()

    assert seed(store, workspace_id, actor_id, bad_file) == 2
    assert len(store.ensure_calls) == 0
    assert len(store.update_profile_calls) == 0


@pytest.mark.parametrize("wildcard_key", ["media*", "media?", "media[0]"])
def test_seed_wildcard_repository_key_exits_2_no_store_call(
    tmp_path: Path, wildcard_key: str
) -> None:
    bad_file = tmp_path / f"wildcard_{wildcard_key}.json"
    bad_file.write_text(
        json.dumps(
            {
                "key": "media-discovery",
                "name": "Media Discovery",
                "kind": "world",
                "repositories": [
                    {
                        "key": wildcard_key,
                        "name": "media-discovery",
                        "url": "https://example.test/repo.git",
                        "default_branch": "main",
                        "protected_branches": ["main"],
                        "automation_ci_secret_free": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()

    assert seed(store, workspace_id, actor_id, bad_file) == 2
    assert len(store.ensure_calls) == 0
    assert len(store.update_profile_calls) == 0


def test_seed_missing_file_or_invalid_json_exits_2_no_store_call(tmp_path: Path) -> None:
    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()

    missing_path = tmp_path / "does_not_exist.json"
    assert seed(store, workspace_id, actor_id, missing_path) == 2
    assert len(store.ensure_calls) == 0

    invalid_json = tmp_path / "corrupt.json"
    invalid_json.write_text("not json content", encoding="utf-8")
    assert seed(store, workspace_id, actor_id, invalid_json) == 2
    assert len(store.ensure_calls) == 0


def test_check_exits_1_with_missing_records_1_and_0_with_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()
    md_id = uuid4()
    store.projects_by_key["media-discovery"] = {"project_id": str(md_id)}
    store.total_projects_count = 3

    # Case 1: missing_records == 1 exits 1
    store.missing_records_count = 1
    code = check(store, workspace_id, actor_id)
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "projects": 3,
        "missing_records": 1,
        "media_discovery": str(md_id),
    }

    # Case 2: missing_records == 0 exits 0
    store.missing_records_count = 0
    code = check(store, workspace_id, actor_id)
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "projects": 3,
        "missing_records": 0,
        "media_discovery": str(md_id),
    }

    # Case 3: media-discovery null exits 1 even if missing_records == 0
    store_no_md = FakeStore()
    store_no_md.total_projects_count = 2
    store_no_md.missing_records_count = 0
    code = check(store_no_md, workspace_id, actor_id)
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "projects": 2,
        "missing_records": 0,
        "media_discovery": None,
    }


def test_show_prints_the_read(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()
    project_id = uuid4()

    read_payload = {
        "project_id": str(project_id),
        "key": "media-discovery",
        "name": "Media Discovery",
        "kind": "world",
        "purpose": "The media-discovery application.",
        "goals": [],
        "questions": ["What are Media Discovery's goals and standing mandate? (Chris)"],
        "repositories": [
            {
                "key": "media-discovery",
                "name": "media-discovery",
                "url": "https://github.com/theturtlecsz/media-discovery.git",
                "default_branch": "main",
                "protected_branches": ["main"],
                "automation_ci_secret_free": False,
            }
        ],
    }
    store.projects_by_key["media-discovery"] = {"project_id": str(project_id)}
    store.projects_by_id[project_id] = read_payload

    code = show(store, workspace_id, actor_id, "media-discovery")
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out == read_payload


def test_run_projects_dispatches_with_fake_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeStore()
    workspace_id = uuid4()
    actor_id = uuid4()
    md_id = uuid4()
    store.projects_by_key["media-discovery"] = {"project_id": str(md_id)}
    store.total_projects_count = 1
    store.missing_records_count = 0

    args = SimpleNamespace(
        projects_command="check",
        workspace=workspace_id,
        actor=actor_id,
    )
    assert run_projects(args, store=store) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["media_discovery"] == str(md_id)
