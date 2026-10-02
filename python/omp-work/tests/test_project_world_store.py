"""OMP-527-s02: PostgreSQL integration tests for project sides and world moves."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.project_store import ProjectNotFound, WorkItemNotFound, WorkStoreError
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _batch, _command, _grant, _plan

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _workspace(service, workspace_id: UUID) -> None:
    _grant(service, workspace_id)
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )


def _insert_project(
    service,
    workspace_id: UUID,
    project_id: UUID,
    key: str,
    name: str,
    kind: str = "surface",
) -> None:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_importer"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects(project_id, workspace_id, key, name, kind)"
            " VALUES (%s, %s, %s, %s, %s)",
            (project_id, workspace_id, key, name, kind),
        )


def _active_relation_count(service, workspace_id: UUID) -> int:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_work.project_relations"
            " WHERE workspace_id=%s AND active",
            (workspace_id,),
        )
        row = cur.fetchone()
        return 0 if row is None else int(row[0])


def _item_row(service, workspace_id: UUID, work_id: str) -> tuple[int, UUID | None]:
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT row_version, project_id FROM omp_work.work_items"
            " WHERE workspace_id=%s AND work_id=%s",
            (workspace_id, work_id),
        )
        row = cur.fetchone()
        assert row is not None
        return int(row[0]), row[1]


def _tree(service, workspace_id: UUID, world: str = "") -> dict:
    store = PostgresWorkStore(service.config)
    return store.read(workspace_id, OWNER, "tree", world)


def _tree_item_ids(tree: dict) -> set[str]:
    return {str(item["work_id"]) for item in tree["items"]}


def _tree_project_ids(tree: dict) -> set[str]:
    return {str(project["project_id"]) for project in tree["projects"]}


def _history(store, workspace_id: UUID, project_id: UUID) -> list[dict]:
    return list(store.read_project(workspace_id, OWNER, project_id)["history"])


def _create_item(service, workspace_id: UUID, project_id: UUID) -> dict:
    status, body = _command(
        service,
        workspace_id,
        _batch(
            [
                {
                    "client_ref": "root",
                    "title": f"Item {uuid4().hex[:8]}",
                    "project_id": str(project_id),
                }
            ]
        ),
    )
    assert status == 200, body
    return body["result"]["items"][0]


def test_link_world_puts_a_project_on_the_media_discovery_side(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    world_id = uuid4()
    target_id = uuid4()
    other_id = uuid4()
    _insert_project(
        service, workspace_id, world_id, "media-discovery", "Media Discovery", "world"
    )
    _insert_project(service, workspace_id, target_id, "md-surface", "MD Surface")
    _insert_project(service, workspace_id, other_id, "omp-surface", "OMP Surface")

    status, body = _command(
        service,
        workspace_id,
        _batch(
            items=[
                {
                    "client_ref": "md-item",
                    "title": "Target Item",
                    "project_id": str(target_id),
                },
                {
                    "client_ref": "omp-item",
                    "title": "Other Item",
                    "project_id": str(other_id),
                },
            ]
        ),
    )
    assert status == 200, body
    created = {item["client_ref"]: item["work_id"] for item in body["result"]["items"]}

    # Before the link, the target's item sits on the omp side (default tree).
    assert _tree_item_ids(_tree(service, workspace_id)) == {
        created["md-item"],
        created["omp-item"],
    }

    result = store.link_world(workspace_id, OWNER, target_id)
    assert result["linked"] is True
    assert result["project_id"] == str(target_id)

    # After the link, the default tree drops the target's item and project.
    default = _tree(service, workspace_id)
    assert _tree_item_ids(default) == {created["omp-item"]}
    assert _tree_project_ids(default) == {str(other_id)}

    # The linked target appears under world=media-discovery with its item.
    md = _tree(service, workspace_id, "media-discovery")
    assert _tree_item_ids(md) == {created["md-item"]}
    assert _tree_project_ids(md) == {str(world_id), str(target_id)}

    # The world records one membership history row.
    world_history = _history(store, workspace_id, world_id)
    assert [row["kind"] for row in world_history] == ["world_member_added"]

    # A second link is idempotent: no new relation and no new history.
    before = _active_relation_count(service, workspace_id)
    second = store.link_world(workspace_id, OWNER, target_id)
    assert second["linked"] is False
    assert _active_relation_count(service, workspace_id) == before
    assert [row["kind"] for row in _history(store, workspace_id, world_id)] == [
        "world_member_added"
    ]


def test_link_world_refuses_missing_project_and_self(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    world_id = uuid4()
    _insert_project(
        service, workspace_id, world_id, "media-discovery", "Media Discovery", "world"
    )

    with pytest.raises(ProjectNotFound):
        store.link_world(workspace_id, OWNER, uuid4())
    with pytest.raises(WorkStoreError):
        store.link_world(workspace_id, OWNER, world_id)


def test_project_sides_labels_the_world_and_its_members(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    world_id = uuid4()
    linked_id = uuid4()
    unlinked_id = uuid4()
    archived_id = uuid4()
    _insert_project(
        service, workspace_id, world_id, "media-discovery", "Media Discovery", "world"
    )
    _insert_project(service, workspace_id, linked_id, "md-surface", "MD Surface")
    _insert_project(service, workspace_id, unlinked_id, "omp-surface", "OMP Surface")
    _insert_project(service, workspace_id, archived_id, "gone", "Archived Surface")
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_importer"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false), set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "UPDATE omp_work.projects SET archived=true WHERE workspace_id=%s AND project_id=%s",
            (workspace_id, archived_id),
        )

    store.link_world(workspace_id, OWNER, linked_id)

    status, body = _command(
        service,
        workspace_id,
        _batch(
            items=[
                {
                    "client_ref": "live",
                    "title": "Live Item",
                    "project_id": str(linked_id),
                },
                {
                    "client_ref": "shut",
                    "title": "Canceled Item",
                    "project_id": str(linked_id),
                    "state": "CANCELED",
                },
            ]
        ),
    )
    assert status == 200, body

    payload = store.project_sides(workspace_id, OWNER)
    by_id = {str(project["project_id"]): project for project in payload["projects"]}
    assert set(by_id) == {str(world_id), str(linked_id), str(unlinked_id)}
    assert by_id[str(world_id)]["side"] == "media-discovery"
    assert by_id[str(linked_id)]["side"] == "media-discovery"
    assert by_id[str(unlinked_id)]["side"] == "omp"
    assert by_id[str(linked_id)]["key"] == "md-surface"
    # Only the non-terminal item counts as open.
    assert by_id[str(linked_id)]["open_items"] == 1
    assert by_id[str(unlinked_id)]["open_items"] == 0


def test_move_item_keeps_identity_and_records_both_sides(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    world_id = uuid4()
    from_id = uuid4()
    to_id = uuid4()
    _insert_project(
        service, workspace_id, world_id, "media-discovery", "Media Discovery", "world"
    )
    _insert_project(service, workspace_id, from_id, "omp-surface", "OMP Surface")
    _insert_project(service, workspace_id, to_id, "md-surface", "MD Surface")
    store.link_world(workspace_id, OWNER, to_id)

    item = _create_item(service, workspace_id, from_id)
    key = item["key"]
    plan = _plan(service, workspace_id, item)
    before = store.read(workspace_id, OWNER, "workflow", key)
    version_before, _ = _item_row(service, workspace_id, item["work_id"])

    result = store.move_item(workspace_id, OWNER, key, to_id)
    assert result["key"] == key
    assert result["from_project_id"] == str(from_id)
    assert result["to_project_id"] == str(to_id)
    assert result["side"] == "media-discovery"

    after = store.read(workspace_id, OWNER, "workflow", key)
    assert str(after["item"]["work_id"]) == item["work_id"]
    assert after["item"]["alias"]["key"] == key
    assert str(after["item"]["revision"]["revision_id"]) == str(
        before["item"]["revision"]["revision_id"]
    )
    assert [str(r["receipt_id"]) for r in after["receipts"]] == [
        str(r["receipt_id"]) for r in before["receipts"]
    ]
    assert plan["receipt_id"] in {str(r["receipt_id"]) for r in after["receipts"]}
    assert str(after["item"]["project_id"]) == str(to_id)

    # The row_version advanced exactly once.
    version_after, proj_after = _item_row(service, workspace_id, item["work_id"])
    assert version_after == version_before + 1
    assert proj_after == to_id

    # Both projects recorded the move, and the summaries name key and both sides.
    out_history = _history(store, workspace_id, from_id)
    in_history = _history(store, workspace_id, to_id)
    assert [row["kind"] for row in out_history] == ["item_moved_out"]
    assert [row["kind"] for row in in_history] == ["item_moved_in"]
    assert key in out_history[0]["summary"]
    assert str(from_id) in out_history[0]["summary"]
    assert str(to_id) in out_history[0]["summary"]
    assert in_history[0]["summary"] == out_history[0]["summary"]

    # The tree follows the item to its new side.
    assert _tree_item_ids(_tree(service, workspace_id)) == set()
    md = _tree(service, workspace_id, "media-discovery")
    assert _tree_item_ids(md) == {item["work_id"]}
    assert str(md["items"][0]["project_id"]) == str(to_id)


def test_move_item_to_same_project_writes_nothing(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    project_id = uuid4()
    _insert_project(service, workspace_id, project_id, "omp-surface", "OMP Surface")
    item = _create_item(service, workspace_id, project_id)
    version_before, _ = _item_row(service, workspace_id, item["work_id"])

    result = store.move_item(workspace_id, OWNER, item["key"], project_id)
    assert result == {"key": item["key"], "unchanged": True}
    version_after, _ = _item_row(service, workspace_id, item["work_id"])
    assert version_after == version_before
    assert _history(store, workspace_id, project_id) == []


def test_move_item_unknown_key_or_project_raises(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    project_id = uuid4()
    _insert_project(service, workspace_id, project_id, "omp-surface", "OMP Surface")
    item = _create_item(service, workspace_id, project_id)

    with pytest.raises(WorkItemNotFound):
        store.move_item(workspace_id, OWNER, "OMP-999999", project_id)
    with pytest.raises(ProjectNotFound):
        store.move_item(workspace_id, OWNER, item["key"], uuid4())
