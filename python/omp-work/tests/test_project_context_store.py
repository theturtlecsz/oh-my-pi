"""OMP-419: PostgreSQL integration tests for the stage-compiler project context."""

from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.project_store import ProjectNotFound
from omp_work.v1.store import PostgresWorkStore
from test_research_contract import _admit_payload, _sample_spec, _setup_research_fixtures
from test_workflow_service import OWNER, _command, _create, _grant

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


def _research(service, workspace_id: UUID) -> SimpleNamespace:
    policy, evaluator, environment, manifest, manifest_sha = (
        _setup_research_fixtures(service, workspace_id)
    )
    return SimpleNamespace(
        policy=policy,
        evaluator=evaluator,
        environment=environment,
        manifest=manifest,
        manifest_sha=manifest_sha,
    )


def _campaign(service, workspace_id: UUID, item: dict, research: SimpleNamespace) -> UUID:
    """A campaign on the item's work, left in the evaluating state."""
    campaign_id = uuid4()
    spec, spec_sha = _sample_spec()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "create_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": item["work_id"],
                "revision_id": item["revision_id"],
                "domain": "machine_learning",
                "spec": spec,
                "spec_sha256": spec_sha,
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "admit_research_campaign",
            "payload": _admit_payload(
                campaign_id,
                item["work_id"],
                item["revision_id"],
                spec_sha,
                research.policy,
                research.manifest,
                research.manifest_sha,
            ),
        },
    )
    assert status == 200, body
    for expected, target in (("admitted", "running"), ("running", "evaluating")):
        status, body = _command(
            service,
            workspace_id,
            {
                "type": "set_research_campaign_state",
                "payload": {
                    "campaign_id": str(campaign_id),
                    "work_id": item["work_id"],
                    "expected_state": expected,
                    "target_state": target,
                    "policy_sha256": research.policy,
                },
            },
        )
        assert status == 200, body
    return campaign_id


def _conclude(
    service, workspace_id: UUID, item: dict, campaign_id: UUID, research: SimpleNamespace
) -> None:
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "conclude_research_campaign",
            "payload": {
                "campaign_id": str(campaign_id),
                "work_id": item["work_id"],
                "policy_sha256": research.policy,
                "outcome": "supported",
                "reason": "evidence is sufficient",
            },
        },
    )
    assert status == 200, body


def test_context_keeps_only_decision_roadmap_refs_and_terminal_missions(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(workspace_id, OWNER, "ctx", "Context", "surface")

    store.update_profile(
        workspace_id,
        OWNER,
        project_id,
        refs=[
            {"kind": "artifact", "ref": "A-1", "title": "unused artifact"},
            {"kind": "evidence", "ref": "E-1", "title": "unused evidence"},
            {"kind": "decision", "ref": "D-2", "title": "second decision"},
            {"kind": "decision", "ref": "D-1", "title": "first decision"},
            {"kind": "roadmap", "ref": "R-1", "title": "roadmap"},
        ],
    )

    completed = uuid4()
    approved = uuid4()
    running = uuid4()
    store.link_mission(workspace_id, OWNER, project_id, completed, "done", "completed")
    store.link_mission(workspace_id, OWNER, project_id, approved, "pending", "approved")
    store.link_mission(workspace_id, OWNER, project_id, running, "moving", "running")

    store.append_history(
        workspace_id, OWNER, project_id, "mission_completed", "done", mission_id=completed
    )
    store.append_history(workspace_id, OWNER, project_id, "profile_updated", "no mission")

    context = store.project_context(workspace_id, OWNER, project_id)

    # Refs carry kind/ref/title for decision and roadmap only, ordered by kind then ref.
    assert context["refs"] == [
        {"kind": "decision", "ref": "D-1", "title": "first decision"},
        {"kind": "decision", "ref": "D-2", "title": "second decision"},
        {"kind": "roadmap", "ref": "R-1", "title": "roadmap"},
    ]
    # Only terminal missions survive, ordered by mission_id.
    assert context["missions"] == [
        {"mission_id": str(completed), "objective": "done", "status": "completed"}
    ]
    # History keeps only rows bound to a mission.
    assert context["history"] == [
        {"mission_id": str(completed), "kind": "mission_completed", "summary": "done"}
    ]
    assert context["research"] == []


def test_context_research_is_concluded_campaigns_on_the_project(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(workspace_id, OWNER, "ctx", "Context", "surface")
    other_project_id = store.ensure_project(workspace_id, OWNER, "other", "Other", "surface")
    research = _research(service, workspace_id)

    item = _create(service, workspace_id, "project item", project_id=str(project_id))
    other_item = _create(
        service, workspace_id, "other item", project_id=str(other_project_id)
    )

    concluded = _campaign(service, workspace_id, item, research)
    _conclude(service, workspace_id, item, concluded, research)
    unconcluded = _campaign(service, workspace_id, item, research)
    foreign = _campaign(service, workspace_id, other_item, research)
    _conclude(service, workspace_id, other_item, foreign, research)

    context = store.project_context(workspace_id, OWNER, project_id)

    assert context["research"] == [
        {
            "campaign_id": str(concluded),
            "domain": "machine_learning",
            "outcome": "supported",
            "outcome_reason": "evidence is sufficient",
        }
    ]
    assert str(unconcluded) not in {row["campaign_id"] for row in context["research"]}
    assert str(foreign) not in {row["campaign_id"] for row in context["research"]}


def test_context_unknown_project_raises(service) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)

    with pytest.raises(ProjectNotFound):
        store.project_context(workspace_id, OWNER, uuid4())
