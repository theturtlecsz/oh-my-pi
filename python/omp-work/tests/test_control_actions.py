"""OMP-403: the disposable-resource registry and the tier 2 policy return."""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from omp_work.project_store import ProjectAuthorityRefused
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import ActionRequest, StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
REPOS = (
    {
        "key": "repo-alpha",
        "name": "repo-alpha",
        "url": "https://example.test/alpha.git",
        "default_branch": "main",
        "protected_branches": ["main"],
        "automation_ci_secret_free": True,
    },
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


def _open(service, key: str = "control") -> tuple[PostgresWorkStore, UUID, UUID]:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(
        workspace_id, OWNER, key, f"Control {key}", "surface"
    )
    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    return store, workspace_id, project_id


def _owner(decision_id: UUID | None) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind="owner",
    )


def test_record_then_read_is_true(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy_id = uuid4()
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", policy_id
    )
    assert store.is_disposable_resource(workspace_id, OWNER, project_id, "web-1")


def test_disposable_match_is_exact_id_not_a_like_prefix(service) -> None:
    store, workspace_id, project_id = _open(service)
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", uuid4()
    )
    assert store.is_disposable_resource(workspace_id, OWNER, project_id, "web-1")
    # ``_`` is not a wildcard and a prefix does not match.
    assert not store.is_disposable_resource(workspace_id, OWNER, project_id, "web_1")
    assert not store.is_disposable_resource(workspace_id, OWNER, project_id, "web")


def test_disposable_registry_is_scoped_to_workspace_and_project(service) -> None:
    store, workspace_id, project_id = _open(service, "control-a")
    other_project = store.ensure_project(
        workspace_id, OWNER, "control-b", "Control B", "surface"
    )
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", uuid4()
    )
    assert not store.is_disposable_resource(
        workspace_id, OWNER, other_project, "web-1"
    )

    other_store, other_workspace, other_workspace_project = _open(service, "control")
    assert not other_store.is_disposable_resource(
        other_workspace, OWNER, other_workspace_project, "web-1"
    )


def test_tier2_request_action_returns_the_covering_policy_id(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class="nonprod_update",
        destinations=("https://a.test",),
        resource_types=("vm",),
        decision_id=uuid4(),
    )
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(uuid4()))

    returned = store.request_action(
        workspace_id,
        OWNER,
        project_id,
        None,
        ActionRequest(
            "nonprod_update", destination="https://a.test", resource_type="vm"
        ),
        None,
        NOW,
    )
    assert returned == policy.policy_id


def test_tier2_request_action_without_a_policy_returns_nothing(service) -> None:
    store, workspace_id, project_id = _open(service)
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(
            workspace_id,
            OWNER,
            project_id,
            None,
            ActionRequest(
                "nonprod_update", destination="https://a.test", resource_type="vm"
            ),
            None,
            NOW,
        )
    assert exc_info.value.code == "standing_policy_required"
