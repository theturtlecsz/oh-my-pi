"""OMP-418: PostgreSQL integration tests for project records, profile and missions."""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from omp_work.project_store import AmbiguousProjectKey, ProjectNotFound
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)


def _workspace(service, workspace_id) -> None:
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


def _insert_project(service, workspace_id, project_id, key: str) -> None:
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
            " VALUES (%s, %s, %s, %s, 'surface')",
            (project_id, workspace_id, key, f"Project {key}"),
        )


def test_importer_project_gets_record_and_full_read(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    actor_id = OWNER
    _workspace(service, workspace_id)

    # A project inserted as omp_work_importer is seeded by the AFTER INSERT trigger.
    importer_project_id = uuid4()
    _insert_project(service, workspace_id, importer_project_id, "importer-key")
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT count(*) FROM omp_work.project_records"
            " WHERE workspace_id=%s AND project_id=%s",
            (workspace_id, importer_project_id),
        )
        row = cur.fetchone()
        assert row is not None and int(row[0]) == 1

    projects, missing = store.count_missing_records(workspace_id, actor_id)
    assert projects == 1
    assert missing == 0

    # The backfill-equivalent read has every field, including the repository url.
    found = store.find_project(workspace_id, actor_id, "importer-key")
    assert found["project_id"] == str(importer_project_id)
    store.update_profile(
        workspace_id,
        actor_id,
        importer_project_id,
        purpose="Ship the thing",
        goals=["grow", "harden"],
        questions=["which repo?"],
        refs=[{"kind": "decision", "ref": "D29", "title": "mandate"}],
        repositories=[
            {
                "key": "media-discovery",
                "name": "media-discovery",
                "url": "https://example.test/media-discovery.git",
                "default_branch": "main",
                "protected_branches": ["main", "release/*"],
                "automation_ci_secret_free": False,
            }
        ],
    )
    mission_id = uuid4()
    store.link_mission(
        workspace_id,
        actor_id,
        importer_project_id,
        mission_id,
        "Objective one",
        "approved",
    )
    store.append_history(
        workspace_id, actor_id, importer_project_id, "mission_linked", "approved"
    )

    view = store.read_project(workspace_id, actor_id, importer_project_id)
    assert view["key"] == "importer-key"
    assert view["purpose"] == "Ship the thing"
    assert view["updated_at"] is not None
    assert [goal["goal"] for goal in view["goals"]] == ["grow", "harden"]
    assert [q["question"] for q in view["questions"]] == ["which repo?"]
    assert view["questions"][0]["resolved"] is False
    assert [ref["ref"] for ref in view["refs"]] == ["D29"]
    assert view["refs"][0]["kind"] == "decision"
    assert view["refs"][0]["title"] == "mandate"
    assert len(view["repositories"]) == 1
    repository = view["repositories"][0]
    assert repository["key"] == "media-discovery"
    assert repository["name"] == "media-discovery"
    assert repository["url"] == "https://example.test/media-discovery.git"
    assert repository["default_branch"] == "main"
    assert list(repository["protected_branches"]) == ["main", "release/*"]
    assert repository["automation_ci_secret_free"] is False
    assert [m["mission_id"] for m in view["missions"]] == [str(mission_id)]
    assert view["missions"][0]["objective"] == "Objective one"
    assert view["missions"][0]["status"] == "approved"
    assert view["missions"][0]["basis_mandate_id"] is None
    assert view["missions"][0]["basis_decision_id"] is None
    assert [h["kind"] for h in view["history"]] == ["mission_linked"]

    # The record backfill also produced rows for projects created before the read.
    other_project_id = uuid4()
    _insert_project(service, workspace_id, other_project_id, "other-key")
    projects, missing = store.count_missing_records(workspace_id, actor_id)
    assert projects == 2
    assert missing == 0


def test_projects_are_isolated_from_each_other(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    actor_id = OWNER
    _workspace(service, workspace_id)

    alpha = store.ensure_project(workspace_id, actor_id, "alpha", "Alpha", "world")
    beta = store.ensure_project(workspace_id, actor_id, "beta", "Beta", "surface")

    store.update_profile(
        workspace_id,
        actor_id,
        alpha,
        purpose="alpha purpose",
        goals=["alpha goal"],
        questions=["alpha question?"],
        refs=[{"kind": "roadmap", "ref": "R-1", "title": "alpha roadmap"}],
        repositories=[
            {
                "key": "alpha-repo",
                "url": "https://example.test/alpha.git",
                "default_branch": "main",
            }
        ],
    )
    store.update_profile(
        workspace_id,
        actor_id,
        beta,
        purpose="beta purpose",
        goals=["beta goal"],
        questions=["beta question?"],
        refs=[{"kind": "artifact", "ref": "A-1", "title": "beta artifact"}],
        repositories=[
            {
                "key": "beta-repo",
                "url": "https://example.test/beta.git",
                "default_branch": "trunk",
            }
        ],
    )
    alpha_mission = uuid4()
    beta_mission = uuid4()
    store.link_mission(workspace_id, actor_id, alpha, alpha_mission, "alpha", "draft")
    store.link_mission(workspace_id, actor_id, beta, beta_mission, "beta", "running")
    store.append_history(workspace_id, actor_id, alpha, "alpha_event", "alpha")
    store.append_history(workspace_id, actor_id, beta, "beta_event", "beta")

    alpha_view = store.read_project(workspace_id, actor_id, alpha)
    beta_view = store.read_project(workspace_id, actor_id, beta)

    assert alpha_view["purpose"] == "alpha purpose"
    assert beta_view["purpose"] == "beta purpose"
    assert [g["goal"] for g in alpha_view["goals"]] == ["alpha goal"]
    assert [g["goal"] for g in beta_view["goals"]] == ["beta goal"]
    assert [q["question"] for q in alpha_view["questions"]] == ["alpha question?"]
    assert [q["question"] for q in beta_view["questions"]] == ["beta question?"]
    assert [r["ref"] for r in alpha_view["refs"]] == ["R-1"]
    assert [r["ref"] for r in beta_view["refs"]] == ["A-1"]
    assert [r["key"] for r in alpha_view["repositories"]] == ["alpha-repo"]
    assert [r["key"] for r in beta_view["repositories"]] == ["beta-repo"]
    assert [m["mission_id"] for m in alpha_view["missions"]] == [str(alpha_mission)]
    assert [m["mission_id"] for m in beta_view["missions"]] == [str(beta_mission)]
    assert [h["kind"] for h in alpha_view["history"]] == ["alpha_event"]
    assert [h["kind"] for h in beta_view["history"]] == ["beta_event"]


def test_ensure_project_is_idempotent_and_duplicate_key_raises(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    actor_id = OWNER
    _workspace(service, workspace_id)

    first = store.ensure_project(workspace_id, actor_id, "dup", "Dup", "promise")
    second = store.ensure_project(workspace_id, actor_id, "dup", "Dup Renamed", "world")
    assert first == second

    # Two unarchived projects with the same key are ambiguous.
    _insert_project(service, workspace_id, uuid4(), "dup")
    with pytest.raises(AmbiguousProjectKey):
        store.find_project(workspace_id, actor_id, "dup")
    with pytest.raises(AmbiguousProjectKey):
        store.ensure_project(workspace_id, actor_id, "dup", "Dup Again", "world")

    with pytest.raises(ProjectNotFound):
        store.find_project(workspace_id, actor_id, "missing")
    with pytest.raises(ProjectNotFound):
        store.read_project(workspace_id, actor_id, uuid4())
