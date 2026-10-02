"""OMP-527-s02: unit tests for the owner-run project world CLI commands."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from omp_work.project_cli import link_world, move_item, run_projects, sides
from omp_work.project_store import (
    ProjectNotFound,
    WorkItemNotFound,
    WorkStoreError,
)


class FakeWorldStore:
    def __init__(self) -> None:
        self.sides_calls: list[dict[str, Any]] = []
        self.link_calls: list[dict[str, Any]] = []
        self.move_calls: list[dict[str, Any]] = []
        self.sides_payload: dict[str, Any] = {"workspace_id": "ws", "projects": []}
        self.link_result: dict[str, Any] = {}
        self.link_error: Exception | None = None
        self.move_result: dict[str, Any] = {}
        self.move_error: Exception | None = None

    def project_sides(self, workspace_id: UUID, actor_id: UUID) -> dict[str, Any]:
        self.sides_calls.append({"workspace_id": workspace_id, "actor_id": actor_id})
        return self.sides_payload

    def link_world(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, Any]:
        self.link_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "project_id": project_id,
            }
        )
        if self.link_error is not None:
            raise self.link_error
        return self.link_result

    def move_item(
        self, workspace_id: UUID, actor_id: UUID, key: str, project_id: UUID
    ) -> dict[str, Any]:
        self.move_calls.append(
            {
                "workspace_id": workspace_id,
                "actor_id": actor_id,
                "key": key,
                "project_id": project_id,
            }
        )
        if self.move_error is not None:
            raise self.move_error
        return self.move_result


def test_sides_prints_one_json_document(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeWorldStore()
    workspace_id = uuid4()
    actor_id = uuid4()
    linked_id = uuid4()
    store.sides_payload = {
        "workspace_id": str(workspace_id),
        "projects": [
            {
                "project_id": str(linked_id),
                "name": "MD Surface",
                "key": "md-surface",
                "side": "media-discovery",
                "open_items": 4,
            },
            {
                "project_id": str(uuid4()),
                "name": "OMP Surface",
                "key": "omp-surface",
                "side": "omp",
                "open_items": 0,
            },
        ],
    }

    assert sides(store, workspace_id, actor_id) == 0
    assert store.sides_calls == [{"workspace_id": workspace_id, "actor_id": actor_id}]
    out = json.loads(capsys.readouterr().out)
    assert out["projects"][0]["side"] == "media-discovery"
    assert out["projects"][1]["side"] == "omp"


def test_link_world_rejects_unknown_world_before_store(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeWorldStore()
    assert link_world(store, uuid4(), uuid4(), "bogus", uuid4()) == 2
    assert store.link_calls == []
    assert "unknown world" in capsys.readouterr().err


def test_link_world_missing_project_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeWorldStore()
    store.link_error = ProjectNotFound("id missing")
    assert link_world(store, uuid4(), uuid4(), "media-discovery", uuid4()) == 1
    assert len(store.link_calls) == 1
    assert "not found" in capsys.readouterr().err


def test_link_world_self_link_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeWorldStore()
    store.link_error = WorkStoreError("invalid_request", ("world cannot link itself",))
    assert link_world(store, uuid4(), uuid4(), "media-discovery", uuid4()) == 2
    assert "refused" in capsys.readouterr().err


def test_link_world_prints_payload(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeWorldStore()
    project_id = uuid4()
    store.link_result = {
        "world_project_id": str(uuid4()),
        "project_id": str(project_id),
        "linked": True,
    }
    assert link_world(store, uuid4(), uuid4(), "media-discovery", project_id) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project_id"] == str(project_id)
    assert out["linked"] is True


def test_move_item_unchanged_prints_and_exits_0(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeWorldStore()
    store.move_result = {"key": "OMP-7", "unchanged": True}
    assert move_item(store, uuid4(), uuid4(), "OMP-7", uuid4()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"key": "OMP-7", "unchanged": True}


@pytest.mark.parametrize(
    "error",
    [WorkItemNotFound("OMP-404"), ProjectNotFound("id missing")],
)
def test_move_item_not_found_exits_1(
    error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    store = FakeWorldStore()
    store.move_error = error
    assert move_item(store, uuid4(), uuid4(), "OMP-404", uuid4()) == 1
    assert len(store.move_calls) == 1
    assert "not found" in capsys.readouterr().err


def test_move_item_prints_the_move(capsys: pytest.CaptureFixture[str]) -> None:
    store = FakeWorldStore()
    from_id = uuid4()
    to_id = uuid4()
    store.move_result = {
        "key": "OMP-7",
        "from_project_id": str(from_id),
        "to_project_id": str(to_id),
        "side": "media-discovery",
    }
    assert move_item(store, uuid4(), uuid4(), "OMP-7", to_id) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["from_project_id"] == str(from_id)
    assert out["to_project_id"] == str(to_id)
    assert out["side"] == "media-discovery"


def test_run_projects_dispatches_world_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = FakeWorldStore()
    store.sides_payload = {"workspace_id": "ws", "projects": []}
    workspace_id = uuid4()
    actor_id = uuid4()

    args = SimpleNamespace(
        projects_command="sides", workspace=workspace_id, actor=actor_id
    )
    assert run_projects(args, store=store) == 0
    assert len(store.sides_calls) == 1
    assert json.loads(capsys.readouterr().out) == store.sides_payload

    project_id = uuid4()
    store.link_result = {"linked": False}
    args = SimpleNamespace(
        projects_command="link-world",
        workspace=workspace_id,
        actor=actor_id,
        world="media-discovery",
        project=project_id,
    )
    assert run_projects(args, store=store) == 0
    assert store.link_calls[-1]["project_id"] == project_id

    store.move_result = {"key": "OMP-7", "unchanged": True}
    args = SimpleNamespace(
        projects_command="move-item",
        workspace=workspace_id,
        actor=actor_id,
        key="OMP-7",
        project=project_id,
    )
    assert run_projects(args, store=store) == 0
    assert store.move_calls[-1] == {
        "workspace_id": workspace_id,
        "actor_id": actor_id,
        "key": "OMP-7",
        "project_id": project_id,
    }
