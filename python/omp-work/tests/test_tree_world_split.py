"""OMP-527-s01: PostgreSQL integration tests for WorkService tree world split."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.project_store import media_discovery_project_ids
from test_workflow_service import OWNER, _batch, _command, _grant, _owner_headers

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


def _link_projects(
    service,
    workspace_id: UUID,
    source_id: UUID,
    target_id: UUID,
    kind: str = "initiative_project",
    active: bool = True,
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
            "INSERT INTO omp_work.project_relations(relation_id, workspace_id, source_project_id, target_project_id, kind, active)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (uuid4(), workspace_id, source_id, target_id, kind, active),
        )


def test_tree_world_split(service) -> None:
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    # 1. Seed keyed media-discovery world, one linked surface, one unlinked surface
    md_world_id = uuid4()
    linked_surface_id = uuid4()
    unlinked_surface_id = uuid4()

    _insert_project(
        service, workspace_id, md_world_id, "media-discovery", "Media Discovery", "world"
    )
    _insert_project(
        service, workspace_id, linked_surface_id, "md-surface", "MD Linked Surface", "surface"
    )
    _insert_project(
        service, workspace_id, unlinked_surface_id, "omp-surface", "OMP Surface", "surface"
    )

    # Link media-discovery world to linked surface via initiative_project relation
    _link_projects(service, workspace_id, md_world_id, linked_surface_id, "initiative_project", active=True)

    # Unit check media_discovery_project_ids directly
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        md_ids = media_discovery_project_ids(cur, workspace_id)
        assert md_ids == frozenset({md_world_id, linked_surface_id})

    # 2. Seed items in each project, one with no project, and one blocks relation across sides
    batch = _batch(
        items=[
            {
                "client_ref": "item-md-world",
                "title": "Item in MD World",
                "scope": "task",
                "project_id": str(md_world_id),
            },
            {
                "client_ref": "item-md-surface",
                "title": "Item in MD Surface",
                "scope": "task",
                "project_id": str(linked_surface_id),
            },
            {
                "client_ref": "item-omp-surface",
                "title": "Item in OMP Surface",
                "scope": "task",
                "project_id": str(unlinked_surface_id),
            },
            {
                "client_ref": "item-no-project",
                "title": "Item with No Project",
                "scope": "task",
            },
        ],
        relations=[
            {
                "source_ref": "item-omp-surface",
                "target_ref": "item-md-world",
                "kind": "blocks",
            },
            {
                "source_ref": "item-no-project",
                "target_ref": "item-omp-surface",
                "kind": "blocks",
            },
            {
                "source_ref": "item-md-world",
                "target_ref": "item-md-surface",
                "kind": "blocks",
            },
        ],
    )
    status, body = _command(service, workspace_id, batch)
    assert status == 200, body

    created_items = {item["client_ref"]: item["work_id"] for item in body["result"]["items"]}

    # 3. Default tree: omits world's and linked surface's items and projects,
    # keeps all others, and drops the cross-side relation.
    resp_default = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree", headers=_owner_headers(workspace_id)
    )
    assert resp_default.status_code == 200
    tree_default = resp_default.json()

    default_item_ids = {item["work_id"] for item in tree_default["items"]}
    assert default_item_ids == {
        created_items["item-omp-surface"],
        created_items["item-no-project"],
    }

    default_project_ids = {p["project_id"] for p in tree_default["projects"]}
    assert default_project_ids == {str(unlinked_surface_id)}

    # Relations must only include edges whose source and target are both in returned items
    default_relation_pairs = [
        (rel["source_work_id"], rel["target_work_id"]) for rel in tree_default["relations"]
    ]
    assert default_relation_pairs == [
        (created_items["item-no-project"], created_items["item-omp-surface"])
    ]

    # Explicit world="" also matches default
    resp_empty_world = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree?world=", headers=_owner_headers(workspace_id)
    )
    assert resp_empty_world.status_code == 200
    assert resp_empty_world.json() == tree_default

    # 4. GET .../tree?world=media-discovery returns exactly the Media Discovery items and projects
    resp_md = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree?world=media-discovery",
        headers=_owner_headers(workspace_id),
    )
    assert resp_md.status_code == 200
    tree_md = resp_md.json()

    md_item_ids = {item["work_id"] for item in tree_md["items"]}
    assert md_item_ids == {
        created_items["item-md-world"],
        created_items["item-md-surface"],
    }

    md_resp_project_ids = {p["project_id"] for p in tree_md["projects"]}
    assert md_resp_project_ids == {str(md_world_id), str(linked_surface_id)}

    md_relation_pairs = [
        (rel["source_work_id"], rel["target_work_id"]) for rel in tree_md["relations"]
    ]
    assert md_relation_pairs == [
        (created_items["item-md-world"], created_items["item-md-surface"])
    ]

    # 5. A workspace with no media-discovery project returns every item by default
    ws_no_md = uuid4()
    _workspace(service, ws_no_md)

    other_proj_id = uuid4()
    _insert_project(
        service, ws_no_md, other_proj_id, "other-surface", "Other Surface", "surface"
    )

    batch_no_md = _batch(
        items=[
            {
                "client_ref": "item-other-proj",
                "title": "Item in Other Project",
                "scope": "task",
                "project_id": str(other_proj_id),
            },
            {
                "client_ref": "item-plain",
                "title": "Item Plain",
                "scope": "task",
            },
        ],
        relations=[
            {
                "source_ref": "item-plain",
                "target_ref": "item-other-proj",
                "kind": "blocks",
            }
        ],
    )
    status_no_md, body_no_md = _command(service, ws_no_md, batch_no_md)
    assert status_no_md == 200, body_no_md
    ws_no_md_items = {item["client_ref"]: item["work_id"] for item in body_no_md["result"]["items"]}

    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        assert media_discovery_project_ids(cur, ws_no_md) == frozenset()

    resp_no_md_default = service.client.get(
        f"/v1/workspaces/{ws_no_md}/tree", headers=_owner_headers(ws_no_md)
    )
    assert resp_no_md_default.status_code == 200
    tree_no_md_default = resp_no_md_default.json()
    assert {item["work_id"] for item in tree_no_md_default["items"]} == {
        ws_no_md_items["item-other-proj"],
        ws_no_md_items["item-plain"],
    }
    assert {p["project_id"] for p in tree_no_md_default["projects"]} == {str(other_proj_id)}
    assert len(tree_no_md_default["relations"]) == 1

    # In a workspace with no media discovery project, world=media-discovery returns empty
    resp_no_md_md = service.client.get(
        f"/v1/workspaces/{ws_no_md}/tree?world=media-discovery",
        headers=_owner_headers(ws_no_md),
    )
    assert resp_no_md_md.status_code == 200
    assert resp_no_md_md.json()["items"] == []
    assert resp_no_md_md.json()["projects"] == []
    assert resp_no_md_md.json()["relations"] == []

    # 6. world=bogus returns HTTP 400 invalid_request
    resp_bogus = service.client.get(
        f"/v1/workspaces/{workspace_id}/tree?world=bogus",
        headers=_owner_headers(workspace_id),
    )
    assert resp_bogus.status_code == 400
    assert resp_bogus.json()["error"]["code"] == "invalid_request"


def test_media_discovery_project_ids_edge_cases(service) -> None:
    ws = uuid4()
    _workspace(service, ws)

    md_id = uuid4()
    active_target = uuid4()
    inactive_target = uuid4()
    milestone_target = uuid4()
    other_source = uuid4()
    other_target = uuid4()

    _insert_project(service, ws, md_id, "media-discovery", "MD World", "world")
    _insert_project(service, ws, active_target, "active-target", "Active Target", "surface")
    _insert_project(service, ws, inactive_target, "inactive-target", "Inactive Target", "surface")
    _insert_project(service, ws, milestone_target, "milestone-target", "Milestone Target", "surface")
    _insert_project(service, ws, other_source, "other-source", "Other Source", "surface")
    _insert_project(service, ws, other_target, "other-target", "Other Target", "surface")

    _link_projects(service, ws, md_id, active_target, "initiative_project", active=True)
    _link_projects(service, ws, md_id, inactive_target, "initiative_project", active=False)
    _link_projects(service, ws, md_id, milestone_target, "project_milestone", active=True)
    _link_projects(service, ws, other_source, other_target, "initiative_project", active=True)

    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        ids = media_discovery_project_ids(cur, ws)
        assert ids == frozenset({md_id, active_target})

        # When media-discovery project is archived, it must yield an empty set
        cur.execute(
            "UPDATE omp_work.projects SET archived = true WHERE workspace_id = %s AND project_id = %s",
            (ws, md_id),
        )
        assert media_discovery_project_ids(cur, ws) == frozenset()

