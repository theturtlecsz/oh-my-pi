"""OMP-418: PostgreSQL integration tests for the project action and spend gates."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.project_store import ProjectAuthorityRefused
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import MissionScopeDraft, StandingMandate
from omp_work.standing_policy import ActionRequest, StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
REPOS = (
    {
        "key": "repo-alpha",
        "name": "repo-alpha",
        "url": "https://example.test/alpha.git",
        "default_branch": "main",
        "protected_branches": ["main"],
        "automation_ci_secret_free": True,
    },
    {
        "key": "repo-secret",
        "name": "repo-secret",
        "url": "https://example.test/secret.git",
        "default_branch": "main",
        "protected_branches": ["main"],
        "automation_ci_secret_free": False,
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


def _open(service):
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(workspace_id, OWNER, "gates", "Gates", "surface")
    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    return store, workspace_id, project_id


def _owner(decision_id: UUID | None, answered: str | None = "owner") -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind=answered,
    )


def _push_policy(decision_id: UUID, repositories: tuple[str, ...]) -> StandingPolicy:
    return StandingPolicy(
        policy_id=uuid4(),
        action_class="push_branch",
        repositories=repositories,
        branch_patterns=("topic/*",),
        decision_id=decision_id,
    )


def _spend_policy(decision_id: UUID, limit: str) -> StandingPolicy:
    return StandingPolicy(
        policy_id=uuid4(),
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal(limit),
        decision_id=decision_id,
    )


def _mandate(decision_id: UUID) -> StandingMandate:
    return StandingMandate(
        mandate_id=uuid4(),
        goals=frozenset({"build"}),
        repositories=frozenset({"repo-alpha"}),
        capabilities=frozenset({"git_read"}),
        tier3_classes=frozenset({"merge_protected_branch"}),
        decision_id=decision_id,
    )


def _rows(service, sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return list(cur.fetchall())


def _action_rows(service, workspace_id: UUID) -> list[dict[str, object]]:
    return _rows(
        service,
        "SELECT action_class, tier, outcome, code, policy_id, decision_id, mission_id"
        " FROM omp_work.project_action_records WHERE workspace_id=%s"
        " ORDER BY recorded_at, record_id",
        (workspace_id,),
    )


def _spend_rows(service, workspace_id: UUID) -> list[dict[str, object]]:
    return _rows(
        service,
        "SELECT amount_usd, tier, policy_id, mission_id FROM omp_work.spend_records"
        " WHERE workspace_id=%s ORDER BY recorded_at, record_id",
        (workspace_id,),
    )


def _mission_status(service, workspace_id: UUID, mission_id: UUID) -> str:
    rows = _rows(
        service,
        "SELECT status FROM omp_work.project_missions"
        " WHERE workspace_id=%s AND mission_id=%s",
        (workspace_id, mission_id),
    )
    assert rows
    return str(rows[0]["status"])


def test_push_without_policy_is_refused_and_recorded(service) -> None:
    store, workspace_id, project_id = _open(service)
    push = ActionRequest("push_branch", repository="repo-alpha", branch="topic/x")
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(workspace_id, OWNER, project_id, None, push, None, NOW)
    assert exc_info.value.code == "standing_policy_required"

    # The refusal is still an audit row: tier 2, refused, with its code.
    rows = _action_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["action_class"] == "push_branch"
    assert rows[0]["tier"] == 2
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["code"] == "standing_policy_required"
    assert rows[0]["policy_id"] is None
    assert _spend_rows(service, workspace_id) == []


def test_covered_push_is_allowed_and_names_the_policy(service) -> None:
    store, workspace_id, project_id = _open(service)
    decision = uuid4()
    policy = _push_policy(decision, ("repo-alpha",))
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(decision))

    push = ActionRequest("push_branch", repository="repo-alpha", branch="topic/x")
    store.request_action(workspace_id, OWNER, project_id, None, push, None, NOW)

    rows = _action_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["tier"] == 2
    assert rows[0]["outcome"] == "allowed"
    assert rows[0]["code"] is None
    assert rows[0]["policy_id"] == policy.policy_id


def test_push_to_repo_not_ci_secret_free_is_refused(service) -> None:
    store, workspace_id, project_id = _open(service)
    decision = uuid4()
    policy = _push_policy(decision, ("repo-alpha", "repo-secret"))
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(decision))

    # The policy names repo-secret, but covers() refuses a repo that is not
    # automation CI secret-free, so the push is refused instead of allowed.
    push = ActionRequest("push_branch", repository="repo-secret", branch="topic/x")
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(workspace_id, OWNER, project_id, None, push, None, NOW)
    assert exc_info.value.code == "standing_policy_required"

    rows = _action_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "refused"
    assert rows[0]["code"] == "standing_policy_required"
    assert rows[0]["policy_id"] is None


def test_tier3_merge_blocked_until_owner_signed(service) -> None:
    store, workspace_id, project_id = _open(service)
    mandate_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, OWNER, project_id, _mandate(mandate_decision), _owner(mandate_decision)
    )
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(uuid4()),
    )
    mission_id = uuid4()
    store.admit_mission(
        workspace_id,
        OWNER,
        project_id,
        mission_id,
        "Merge the candidate",
        MissionScopeDraft(
            goals=frozenset({"build"}),
            repositories=frozenset({"repo-alpha"}),
            capabilities=frozenset({"git_read"}),
            budget_ceiling_usd=Decimal("20"),
        ),
    )
    assert _mission_status(service, workspace_id, mission_id) == "approved"

    merge = ActionRequest(
        "merge_protected_branch", repository="repo-alpha", branch="main"
    )
    # The class is inside the mandate, so the mission stays approved, but the
    # merge waits on the owner signature.
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(
            workspace_id, OWNER, project_id, mission_id, merge, None, NOW
        )
    assert exc_info.value.code == "blocked_owner_signature"
    assert _mission_status(service, workspace_id, mission_id) == "approved"
    rows = _action_rows(service, workspace_id)
    assert rows[-1]["tier"] == 3
    assert rows[-1]["outcome"] == "refused"
    assert rows[-1]["code"] == "blocked_owner_signature"
    assert rows[-1]["decision_id"] is None

    # An owner-signed authorization allows the merge and names its decision.
    decision = uuid4()
    store.request_action(
        workspace_id,
        OWNER,
        project_id,
        mission_id,
        merge,
        _owner(decision),
        NOW,
    )
    assert _mission_status(service, workspace_id, mission_id) == "approved"
    rows = _action_rows(service, workspace_id)
    assert len(rows) == 2
    assert rows[-1]["outcome"] == "allowed"
    assert rows[-1]["code"] is None
    assert rows[-1]["decision_id"] == decision


def test_tier3_outside_mandate_awaits_confirmation(service) -> None:
    store, workspace_id, project_id = _open(service)
    mandate_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, OWNER, project_id, _mandate(mandate_decision), _owner(mandate_decision)
    )
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(uuid4()),
    )
    mission_id = uuid4()
    store.admit_mission(
        workspace_id,
        OWNER,
        project_id,
        mission_id,
        "Deploy the candidate",
        MissionScopeDraft(
            goals=frozenset({"build"}),
            repositories=frozenset({"repo-alpha"}),
            capabilities=frozenset({"git_read"}),
            budget_ceiling_usd=Decimal("20"),
        ),
    )
    assert _mission_status(service, workspace_id, mission_id) == "approved"

    # production_deploy is tier 3 but outside the mandate's tier3_classes.
    deploy = ActionRequest("production_deploy", resource_type="web")
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(
            workspace_id, OWNER, project_id, mission_id, deploy, _owner(uuid4()), NOW
        )
    assert exc_info.value.code == "blocked_owner_signature"
    assert _mission_status(service, workspace_id, mission_id) == "awaiting_confirmation"
    rows = _action_rows(service, workspace_id)
    assert rows[-1]["action_class"] == "production_deploy"
    assert rows[-1]["tier"] == 3
    assert rows[-1]["outcome"] == "refused"
    assert rows[-1]["mission_id"] == mission_id


def test_spend_above_threshold_refused_then_allowed_with_policy(service) -> None:
    store, workspace_id, project_id = _open(service)
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(uuid4()),
    )

    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.record_spend(
            workspace_id, OWNER, project_id, None, Decimal("50"), NOW
        )
    assert exc_info.value.code == "standing_policy_required"
    assert _spend_rows(service, workspace_id) == []

    decision = uuid4()
    policy = _spend_policy(decision, "60")
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(decision))
    store.record_spend(workspace_id, OWNER, project_id, None, Decimal("50"), NOW)

    rows = _spend_rows(service, workspace_id)
    assert len(rows) == 1
    assert rows[0]["tier"] == "tier2"
    assert rows[0]["policy_id"] == policy.policy_id
    assert rows[0]["amount_usd"] == Decimal("50")


def test_spend_past_ceiling_refused_under_policy(service) -> None:
    store, workspace_id, project_id = _open(service)
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(uuid4()),
    )
    decision = uuid4()
    policy = _spend_policy(decision, "1000")
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(decision))
    store.record_spend(workspace_id, OWNER, project_id, None, Decimal("50"), NOW)

    # 50 already recorded + 60 exceeds the 100 ceiling, so even a policy that
    # would cover the amount cannot admit the spend.
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.record_spend(workspace_id, OWNER, project_id, None, Decimal("60"), NOW)
    assert exc_info.value.code == "ceiling_exceeded"
    assert len(_spend_rows(service, workspace_id)) == 1


def test_admitted_mission_past_its_lower_ceiling_refused(service) -> None:
    store, workspace_id, project_id = _open(service)
    mandate_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, OWNER, project_id, _mandate(mandate_decision), _owner(mandate_decision)
    )
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(uuid4()),
    )
    mission_id = uuid4()
    store.admit_mission(
        workspace_id,
        OWNER,
        project_id,
        mission_id,
        "Build under its own cap",
        MissionScopeDraft(
            goals=frozenset({"build"}),
            repositories=frozenset({"repo-alpha"}),
            capabilities=frozenset({"git_read"}),
            budget_ceiling_usd=Decimal("20"),
        ),
    )

    # The mission budget's 20 ceiling is tighter than the project's 100.
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.record_spend(
            workspace_id, OWNER, project_id, mission_id, Decimal("25"), NOW
        )
    assert exc_info.value.code == "ceiling_exceeded"
    assert _spend_rows(service, workspace_id) == []


def test_spend_without_budget_requires_decision(service) -> None:
    store, workspace_id, project_id = _open(service)
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.record_spend(workspace_id, OWNER, project_id, None, Decimal("5"), NOW)
    assert exc_info.value.code == "budget_decision_required"
    assert _spend_rows(service, workspace_id) == []
