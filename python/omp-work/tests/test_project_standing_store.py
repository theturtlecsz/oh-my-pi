"""OMP-418: PostgreSQL integration tests for project standing authority."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.project_store import ProjectAuthorityRefused, ProjectNotFound
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import MissionScopeDraft, StandingMandate
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
T1 = datetime(2027, 6, 1, tzinfo=timezone.utc)
BASE_DEST = ("https://a.test", "https://b.test")
WIDE_DEST = ("https://a.test", "https://b.test", "https://c.test")
CREATE_DEST = ("https://a.test",)

CASES = (
    "policy_create",
    "policy_widen",
    "policy_extend",
    "budget_create",
    "budget_ceiling",
    "budget_threshold",
    "mandate_create",
    "mandate_widen",
)
NEEDS_BASE = frozenset(
    {
        "policy_widen",
        "policy_extend",
        "budget_ceiling",
        "budget_threshold",
        "mandate_widen",
    }
)
HISTORY = {
    "policy_create": "policy_create",
    "policy_widen": "policy_widen",
    "policy_extend": "policy_extend",
    "budget_create": "budget_create",
    "budget_ceiling": "budget_widen",
    "budget_threshold": "budget_widen",
    "mandate_create": "mandate_create",
    "mandate_widen": "mandate_widen",
}


class _Ids:
    def __init__(self) -> None:
        self.policy_id = uuid4()
        self.mandate_id = uuid4()
        self.budget_id = "budget-1"
        self.base_decision = uuid4()


@pytest.fixture
def ids() -> _Ids:
    return _Ids()


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
    project_id = store.ensure_project(
        workspace_id, OWNER, "standing", "Standing", "surface"
    )
    return store, workspace_id, OWNER, project_id


def _owner(
    decision_id: UUID | None,
    requested: str = "owner",
    answered: str | None = "owner",
) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind=requested,
        decision_id=decision_id,
        answered_by_kind=answered,
    )


def _policy(policy_id: UUID, destinations: tuple[str, ...], expires: datetime, decision_id: UUID) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="network_access",
        destinations=destinations,
        expires_at=expires,
        decision_id=decision_id,
    )


def _mandate(mandate_id: UUID, goals: set[str], decision_id: UUID) -> StandingMandate:
    return StandingMandate(
        mandate_id=mandate_id,
        goals=frozenset(goals),
        repositories=frozenset({"repo-alpha"}),
        capabilities=frozenset({"git_read"}),
        tier3_classes=frozenset({"merge_protected_branch"}),
        decision_id=decision_id,
    )


def _budget(budget_id: str, ceiling: str, threshold: str) -> SpendBudget:
    return SpendBudget(
        budget_id=budget_id,
        ceiling_usd=Decimal(ceiling),
        threshold_usd=Decimal(threshold),
    )


def _money(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decision_refs(view: dict[str, object]) -> list[str]:
    refs = view["refs"]
    assert isinstance(refs, list)
    return sorted(ref["ref"] for ref in refs if ref["kind"] == "decision")


def _snap(view: dict[str, object]) -> dict[str, object]:
    history = view["history"]
    assert isinstance(history, list)
    return {
        "mandate": view["standing_mandate"],
        "policies": view["standing_policies"],
        "budget": view["standing_budget"],
        "refs": _decision_refs(view),
        "history": [(row["kind"], row["summary"]) for row in history],
    }


def _prepare(store, workspace_id, actor, project_id, case: str, ids: _Ids) -> None:
    auth = _owner(ids.base_decision)
    if case in {"policy_widen", "policy_extend"}:
        store.put_standing_policy(
            workspace_id,
            actor,
            project_id,
            _policy(ids.policy_id, BASE_DEST, T0, ids.base_decision),
            auth,
        )
    elif case == "mandate_widen":
        store.set_standing_mandate(
            workspace_id,
            actor,
            project_id,
            _mandate(ids.mandate_id, {"build"}, ids.base_decision),
            auth,
        )
    elif case in {"budget_ceiling", "budget_threshold"}:
        store.set_spend_budget(
            workspace_id,
            actor,
            project_id,
            None,
            _budget(ids.budget_id, "20", "10"),
            auth,
        )


def _apply(store, workspace_id, actor, project_id, case: str, ids: _Ids, decision: UUID, authority: ChangeAuthority) -> None:
    if case == "policy_create":
        store.put_standing_policy(
            workspace_id, actor, project_id,
            _policy(ids.policy_id, CREATE_DEST, T0, decision), authority,
        )
    elif case == "policy_widen":
        store.put_standing_policy(
            workspace_id, actor, project_id,
            _policy(ids.policy_id, WIDE_DEST, T0, decision), authority,
        )
    elif case == "policy_extend":
        store.put_standing_policy(
            workspace_id, actor, project_id,
            _policy(ids.policy_id, BASE_DEST, T1, decision), authority,
        )
    elif case == "budget_create":
        store.set_spend_budget(
            workspace_id, actor, project_id, None,
            _budget(ids.budget_id, "20", "10"), authority,
        )
    elif case == "budget_ceiling":
        store.set_spend_budget(
            workspace_id, actor, project_id, None,
            _budget(ids.budget_id, "30", "10"), authority,
        )
    elif case == "budget_threshold":
        store.set_spend_budget(
            workspace_id, actor, project_id, None,
            _budget(ids.budget_id, "20", "15"), authority,
        )
    elif case == "mandate_create":
        store.set_standing_mandate(
            workspace_id, actor, project_id,
            _mandate(ids.mandate_id, {"build"}, decision), authority,
        )
    elif case == "mandate_widen":
        store.set_standing_mandate(
            workspace_id, actor, project_id,
            _mandate(ids.mandate_id, {"build", "ship"}, decision), authority,
        )
    else:
        raise AssertionError(case)


def _assert_stored(case: str, view: dict[str, object], decision: UUID, ids: _Ids) -> None:
    refs = _decision_refs(view)
    assert str(decision) in refs
    if case in NEEDS_BASE:
        assert str(ids.base_decision) in refs
        assert len(refs) == 2
    else:
        assert refs == [str(decision)]
    history = view["history"]
    assert isinstance(history, list)
    assert history[-1]["kind"] == HISTORY[case]
    if case == "mandate_create":
        mandate = view["standing_mandate"]
        assert isinstance(mandate, dict)
        assert mandate["decision_id"] == str(decision)
        assert mandate["mandate_id"] == str(ids.mandate_id)
        assert set(mandate["goals"]) == {"build"}
    elif case == "mandate_widen":
        mandate = view["standing_mandate"]
        assert isinstance(mandate, dict)
        assert mandate["decision_id"] == str(decision)
        assert set(mandate["goals"]) == {"build", "ship"}
    elif case == "policy_create":
        policies = view["standing_policies"]
        assert isinstance(policies, list) and len(policies) == 1
        assert policies[0]["decision_id"] == str(decision)
        assert set(policies[0]["destinations"]) == set(CREATE_DEST)
    elif case == "policy_widen":
        policies = view["standing_policies"]
        assert isinstance(policies, list) and len(policies) == 1
        assert policies[0]["decision_id"] == str(decision)
        assert set(policies[0]["destinations"]) == set(WIDE_DEST)
    elif case == "policy_extend":
        policies = view["standing_policies"]
        assert isinstance(policies, list) and len(policies) == 1
        assert policies[0]["decision_id"] == str(decision)
        assert set(policies[0]["destinations"]) == set(BASE_DEST)
        assert "2027" in policies[0]["expires_at"]
    elif case == "budget_create":
        budget = view["standing_budget"]
        assert isinstance(budget, dict)
        assert _money(budget["ceiling_usd"]) == Decimal("20")
        assert _money(budget["threshold_usd"]) == Decimal("10")
        assert budget["decision_id"] == str(decision)
        assert budget["budget_id"] == ids.budget_id
    elif case == "budget_ceiling":
        budget = view["standing_budget"]
        assert isinstance(budget, dict)
        assert _money(budget["ceiling_usd"]) == Decimal("30")
        assert _money(budget["threshold_usd"]) == Decimal("10")
        assert budget["decision_id"] == str(decision)
    elif case == "budget_threshold":
        budget = view["standing_budget"]
        assert isinstance(budget, dict)
        assert _money(budget["ceiling_usd"]) == Decimal("20")
        assert _money(budget["threshold_usd"]) == Decimal("15")
        assert budget["decision_id"] == str(decision)


def _rows(service, sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return list(cur.fetchall())


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize(
    ("label", "requested", "answered", "code"),
    [
        ("unanswered", "owner", None, "owner_signature_required"),
        ("grokbot-answered", "grokbot", "grokbot", "owner_signature_required"),
        ("automation-requested", "automation", "owner", "worker_not_permitted"),
        ("task-agent-requested", "task-agent", "owner", "worker_not_permitted"),
    ],
)
def test_opening_change_refused_stores_nothing(service, ids: _Ids, case: str, label: str, requested: str, answered: str | None, code: str) -> None:
    store, workspace_id, actor, project_id = _open(service)
    _prepare(store, workspace_id, actor, project_id, case, ids)
    before = _snap(store.read_project(workspace_id, actor, project_id))
    decision = uuid4()
    authority = _owner(decision, requested, answered)
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        _apply(store, workspace_id, actor, project_id, case, ids, decision, authority)
    assert exc_info.value.code == code, label
    assert _snap(store.read_project(workspace_id, actor, project_id)) == before


@pytest.mark.parametrize("case", CASES)
def test_owner_answered_change_is_stored(service, ids: _Ids, case: str) -> None:
    store, workspace_id, actor, project_id = _open(service)
    _prepare(store, workspace_id, actor, project_id, case, ids)
    decision = uuid4()
    _apply(store, workspace_id, actor, project_id, case, ids, decision, _owner(decision))
    view = store.read_project(workspace_id, actor, project_id)
    _assert_stored(case, view, decision, ids)


def test_narrow_and_revoke_record_no_decision(service) -> None:
    store, workspace_id, actor, project_id = _open(service)
    quiet = _owner(None, answered=None)
    policy_id = uuid4()
    policy_decision = uuid4()
    store.put_standing_policy(
        workspace_id, actor, project_id,
        _policy(policy_id, BASE_DEST, T0, policy_decision),
        _owner(policy_decision),
    )
    refs = _decision_refs(store.read_project(workspace_id, actor, project_id))
    store.put_standing_policy(
        workspace_id, actor, project_id,
        _policy(policy_id, CREATE_DEST, T0, policy_decision),
        quiet,
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert _decision_refs(view) == refs
    policies = view["standing_policies"]
    assert isinstance(policies, list) and len(policies) == 1
    assert set(policies[0]["destinations"]) == set(CREATE_DEST)
    assert policies[0]["decision_id"] == str(policy_decision)
    assert view["history"][-1]["kind"] == "policy_narrow"
    store.revoke_standing_policy(workspace_id, actor, project_id, policy_id, quiet)
    view = store.read_project(workspace_id, actor, project_id)
    assert view["standing_policies"] == []
    assert _decision_refs(view) == refs
    assert view["history"][-1]["kind"] == "policy_revoke"
    policy_rows = _rows(
        service,
        "SELECT active, revoked_at FROM omp_work.standing_policies"
        " WHERE workspace_id=%s AND project_id=%s",
        (workspace_id, project_id),
    )
    assert policy_rows
    assert all(row["active"] is False and row["revoked_at"] is not None for row in policy_rows)

    mandate_id = uuid4()
    mandate_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, actor, project_id,
        _mandate(mandate_id, {"build", "ship"}, mandate_decision),
        _owner(mandate_decision),
    )
    refs = _decision_refs(store.read_project(workspace_id, actor, project_id))
    store.set_standing_mandate(
        workspace_id, actor, project_id,
        _mandate(mandate_id, {"build"}, mandate_decision),
        quiet,
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert _decision_refs(view) == refs
    mandate = view["standing_mandate"]
    assert isinstance(mandate, dict)
    assert mandate["decision_id"] == str(mandate_decision)
    assert set(mandate["goals"]) == {"build"}
    assert view["history"][-1]["kind"] == "mandate_narrow"
    store.revoke_standing_mandate(workspace_id, actor, project_id, quiet)
    view = store.read_project(workspace_id, actor, project_id)
    assert view["standing_mandate"] is None
    assert _decision_refs(view) == refs
    assert view["history"][-1]["kind"] == "mandate_revoke"
    mandate_rows = _rows(
        service,
        "SELECT active FROM omp_work.standing_mandates"
        " WHERE workspace_id=%s AND project_id=%s",
        (workspace_id, project_id),
    )
    assert mandate_rows
    assert all(row["active"] is False for row in mandate_rows)

    budget_decision = uuid4()
    store.set_spend_budget(
        workspace_id, actor, project_id, None,
        _budget("budget-1", "20", "10"),
        _owner(budget_decision),
    )
    refs = _decision_refs(store.read_project(workspace_id, actor, project_id))
    store.set_spend_budget(
        workspace_id, actor, project_id, None,
        _budget("budget-1", "15", "8"),
        quiet,
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert _decision_refs(view) == refs
    budget = view["standing_budget"]
    assert isinstance(budget, dict)
    assert _money(budget["ceiling_usd"]) == Decimal("15")
    assert _money(budget["threshold_usd"]) == Decimal("8")
    assert budget["decision_id"] == str(budget_decision)
    assert view["history"][-1]["kind"] == "budget_narrow"


def test_tier3_policy_stores_nothing(service) -> None:
    store, workspace_id, actor, project_id = _open(service)
    decision = uuid4()
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class="production_deploy",
        decision_id=decision,
    )
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_standing_policy(
            workspace_id, actor, project_id, policy, _owner(decision)
        )
    assert exc_info.value.code == "not_tier2"
    view = store.read_project(workspace_id, actor, project_id)
    assert view["standing_policies"] == []
    assert view["standing_mandate"] is None
    assert view["standing_budget"] is None
    assert _decision_refs(view) == []
    assert view["history"] == []


def test_policy_validation_uses_project_repositories(service) -> None:
    store, workspace_id, actor, project_id = _open(service)
    store.update_profile(
        workspace_id,
        actor,
        project_id,
        repositories=[
            {
                "key": "repo-alpha",
                "name": "repo-alpha",
                "url": "https://example.test/repo-alpha.git",
                "default_branch": "main",
                "protected_branches": ["main"],
                "automation_ci_secret_free": True,
            }
        ],
    )
    missing = uuid4()
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_standing_policy(
            workspace_id,
            actor,
            project_id,
            StandingPolicy(
                policy_id=uuid4(),
                action_class="push_branch",
                repositories=("missing",),
                branch_patterns=("topic/*",),
                decision_id=missing,
            ),
            _owner(missing),
        )
    assert exc_info.value.code == "unknown_repository"
    assert store.read_project(workspace_id, actor, project_id)["standing_policies"] == []

    decision = uuid4()
    store.put_standing_policy(
        workspace_id,
        actor,
        project_id,
        StandingPolicy(
            policy_id=uuid4(),
            action_class="push_branch",
            repositories=("repo-alpha",),
            branch_patterns=("topic/*",),
            decision_id=decision,
        ),
        _owner(decision),
    )
    view = store.read_project(workspace_id, actor, project_id)
    policies = view["standing_policies"]
    assert isinstance(policies, list) and len(policies) == 1
    assert policies[0]["repositories"] == ["repo-alpha"]
    assert _decision_refs(view) == [str(decision)]


def test_admit_mission_inside_stores_ceiling_without_a_new_decision(service) -> None:
    store, workspace_id, actor, project_id = _open(service)
    mandate_id = uuid4()
    mandate_decision = uuid4()
    budget_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, actor, project_id,
        _mandate(mandate_id, {"build"}, mandate_decision),
        _owner(mandate_decision),
    )
    store.set_spend_budget(
        workspace_id, actor, project_id, None,
        _budget("project", "100", "40"),
        _owner(budget_decision),
    )
    refs = _decision_refs(store.read_project(workspace_id, actor, project_id))
    inside = uuid4()
    store.admit_mission(
        workspace_id,
        actor,
        project_id,
        inside,
        "Ship the reader",
        MissionScopeDraft(
            goals=frozenset({"build"}),
            repositories=frozenset({"repo-alpha"}),
            capabilities=frozenset({"git_read"}),
            tier3_classes=frozenset(),
            budget_ceiling_usd=Decimal("40"),
            budget_threshold_usd=Decimal("10"),
        ),
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert _decision_refs(view) == refs
    mission = next(row for row in view["missions"] if row["mission_id"] == str(inside))
    assert mission["status"] == "approved"
    assert mission["objective"] == "Ship the reader"
    assert mission["basis_mandate_id"] == str(mandate_id)
    assert mission["basis_decision_id"] == str(mandate_decision)
    assert any(
        row["kind"] == "mission_admitted" and row["mission_id"] == str(inside)
        for row in view["history"]
    )
    standing = view["standing_budget"]
    assert isinstance(standing, dict)
    assert _money(standing["ceiling_usd"]) == Decimal("100")
    budgets = _rows(
        service,
        "SELECT ceiling_usd, threshold_usd, decision_id, active FROM omp_work.spend_budgets"
        " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
        (workspace_id, project_id, inside),
    )
    assert len(budgets) == 1
    assert budgets[0]["active"] is True
    assert _money(budgets[0]["ceiling_usd"]) == Decimal("40")
    assert _money(budgets[0]["threshold_usd"]) == Decimal("10")
    assert budgets[0]["decision_id"] == mandate_decision

    outside = uuid4()
    store.admit_mission(
        workspace_id,
        actor,
        project_id,
        outside,
        "Leave the mandate",
        MissionScopeDraft(
            goals=frozenset({"not-a-goal"}),
            repositories=frozenset({"repo-alpha"}),
            capabilities=frozenset({"git_read"}),
            budget_ceiling_usd=Decimal("40"),
            budget_threshold_usd=Decimal("10"),
        ),
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert _decision_refs(view) == refs
    mission = next(row for row in view["missions"] if row["mission_id"] == str(outside))
    assert mission["status"] == "awaiting_confirmation"
    assert mission["basis_mandate_id"] is None
    assert mission["basis_decision_id"] is None
    assert _rows(
        service,
        "SELECT 1 FROM omp_work.spend_budgets"
        " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
        (workspace_id, project_id, outside),
    ) == []

    raised = uuid4()
    store.set_spend_budget(
        workspace_id, actor, project_id, None,
        _budget("project", "150", "40"),
        _owner(raised),
    )
    view = store.read_project(workspace_id, actor, project_id)
    assert isinstance(view["standing_budget"], dict)
    assert _money(view["standing_budget"]["ceiling_usd"]) == Decimal("150")
    assert _decision_refs(view) == sorted([*refs, str(raised)])
    kept = _rows(
        service,
        "SELECT ceiling_usd, decision_id, active FROM omp_work.spend_budgets"
        " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
        (workspace_id, project_id, inside),
    )
    assert len(kept) == 1 and kept[0]["active"] is True
    assert _money(kept[0]["ceiling_usd"]) == Decimal("40")
    assert kept[0]["decision_id"] == mandate_decision


def test_standing_authority_is_isolated(service) -> None:
    store, workspace_id, actor, alpha = _open(service)
    beta = store.ensure_project(workspace_id, actor, "beta", "Beta", "surface")
    alpha_decision = uuid4()
    beta_decision = uuid4()
    store.set_standing_mandate(
        workspace_id, actor, alpha,
        _mandate(uuid4(), {"alpha-goal"}, alpha_decision),
        _owner(alpha_decision),
    )
    store.put_standing_policy(
        workspace_id, actor, alpha,
        _policy(uuid4(), CREATE_DEST, T0, alpha_decision),
        _owner(alpha_decision),
    )
    store.set_spend_budget(
        workspace_id, actor, alpha, None,
        _budget("alpha-budget", "20", "10"),
        _owner(alpha_decision),
    )
    beta_view = store.read_project(workspace_id, actor, beta)
    assert beta_view["standing_mandate"] is None
    assert beta_view["standing_policies"] == []
    assert beta_view["standing_budget"] is None
    assert _decision_refs(beta_view) == []

    store.set_standing_mandate(
        workspace_id, actor, beta,
        _mandate(uuid4(), {"beta-goal"}, beta_decision),
        _owner(beta_decision),
    )
    alpha_view = store.read_project(workspace_id, actor, alpha)
    beta_view = store.read_project(workspace_id, actor, beta)
    assert isinstance(alpha_view["standing_mandate"], dict)
    assert isinstance(beta_view["standing_mandate"], dict)
    assert set(alpha_view["standing_mandate"]["goals"]) == {"alpha-goal"}
    assert set(beta_view["standing_mandate"]["goals"]) == {"beta-goal"}
    assert alpha_view["standing_mandate"]["decision_id"] == str(alpha_decision)
    assert len(alpha_view["standing_policies"]) == 1
    assert beta_view["standing_policies"] == []
    assert isinstance(alpha_view["standing_budget"], dict)
    assert alpha_view["standing_budget"]["budget_id"] == "alpha-budget"
    assert beta_view["standing_budget"] is None

    other_workspace = uuid4()
    _workspace(service, other_workspace)
    other_project = store.ensure_project(
        other_workspace, actor, "other", "Other", "surface"
    )
    with pytest.raises(ProjectNotFound):
        store.read_project(other_workspace, actor, alpha)
    other_view = store.read_project(other_workspace, actor, other_project)
    assert other_view["standing_mandate"] is None
    assert other_view["standing_policies"] == []
    assert other_view["standing_budget"] is None


def test_content_columns_are_not_updatable(service) -> None:
    store, workspace_id, actor, project_id = _open(service)
    decision = uuid4()
    store.set_standing_mandate(
        workspace_id, actor, project_id,
        _mandate(uuid4(), {"build"}, decision),
        _owner(decision),
    )
    store.put_standing_policy(
        workspace_id, actor, project_id,
        _policy(uuid4(), CREATE_DEST, T0, decision),
        _owner(decision),
    )
    store.set_spend_budget(
        workspace_id, actor, project_id, None,
        _budget("budget-1", "20", "10"),
        _owner(decision),
    )
    denied = (
        (
            "UPDATE omp_work.standing_mandates SET goals = %s WHERE workspace_id = %s",
            (["tampered"], workspace_id),
        ),
        (
            "UPDATE omp_work.standing_policies SET money_limit_usd = 1 WHERE workspace_id = %s",
            (workspace_id,),
        ),
        (
            "UPDATE omp_work.spend_budgets SET ceiling_usd = 1 WHERE workspace_id = %s",
            (workspace_id,),
        ),
    )
    for sql, params in denied:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn:
                with conn.transaction(), conn.cursor() as cur:
                    cur.execute(
                        "SELECT set_config('omp.workspace_id', %s, true),"
                        " set_config('omp.actor_id', %s, true)",
                        (str(workspace_id), str(actor)),
                    )
                    cur.execute(sql, params)
    view = store.read_project(workspace_id, actor, project_id)
    assert isinstance(view["standing_mandate"], dict)
    assert set(view["standing_mandate"]["goals"]) == {"build"}
    assert isinstance(view["standing_budget"], dict)
    assert _money(view["standing_budget"]["ceiling_usd"]) == Decimal("20")
