"""OMP-418: project records, profile, missions, history and standing authority.

The store owns project_records/project_goals/project_questions/project_refs/
project_repositories/project_missions/project_history (migration 0030),
standing_mandates/standing_policies/spend_budgets (migration 0031) and the
project_action_records/spend_records audit rows (migration 0032) on top of the
existing omp_work.projects and omp_work.repositories rows. PostgresWorkStore
mixes this in; the host supplies _transaction and the workspace/actor claims.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import psycopg

from omp_work.action_tiers import tier_of
from omp_work.spend_budget import (
    SpendBudget,
    authorize_spend,
    budget_change_kind,
    effective_budgets,
)
from omp_work.standing_change import (
    ChangeAuthority,
    ChangeKind,
    StandingChangeRefused,
    authorize_standing_change,
    owner_signed,
)
from omp_work.standing_mandate import (
    MandateRefused,
    MissionScopeDraft,
    StandingMandate,
    encounter_tier3,
    mandate_change_kind,
    mission_scope,
    validate_mandate,
)
from omp_work.standing_policy import (
    ActionRequest,
    PolicyRefused,
    RepositoryRecord,
    StandingPolicy,
    covers,
    policy_change_kind,
    validate_policy,
)
from omp_work.v1.agent_stop import read_stop_state
from omp_work.v1.decision_records import find_decision
from omp_work.v1.missions import open_missions, project_mission_progress
from omp_work.v1.store_shared import WorkStoreError, row_json

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

_PROJECT_FIELDS = "project_id,key,name,kind,archived"
_LIST_FIELDS = "project_id,key,name,kind"
_RECORD_FIELDS = "workspace_id,project_id,purpose,updated_at"
_GOAL_FIELDS = "workspace_id,project_id,position,goal"
_QUESTION_FIELDS = "workspace_id,project_id,question,resolved"
_REF_FIELDS = "workspace_id,project_id,kind,ref,title"
_REPOSITORY_FIELDS = (
    "pr.repository_id,r.key,r.name,r.url,pr.default_branch,"
    "pr.protected_branches,pr.automation_ci_secret_free"
)
_MISSION_FIELDS = (
    "workspace_id,mission_id,project_id,objective,status,"
    "basis_mandate_id,basis_decision_id"
)
_HISTORY_FIELDS = "workspace_id,history_id,project_id,mission_id,kind,summary,at"
_MANDATE_FIELDS = (
    "mandate_id,goals,repositories,capabilities,tier3_classes,decision_id,active"
)
_POLICY_FIELDS = (
    "policy_id,action_class,repositories,destinations,branch_patterns,"
    "resource_types,money_limit_usd,expires_at,decision_id,active,revoked_at"
)
_BUDGET_FIELDS = "budget_id,mission_id,ceiling_usd,threshold_usd,decision_id,active"


def _disposable_summary(resource_id: str, policy_id: UUID | None) -> str:
    """Canonical JSON summary whose resource id is matched by equality, not LIKE."""
    return json.dumps(
        {
            "resource_id": resource_id,
            "policy_id": None if policy_id is None else str(policy_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _summary_names_resource(summary: str, resource_id: str) -> bool:
    """True only when this history summary recorded exactly ``resource_id``.

    A shorter id, or one that differs by a character ``LIKE`` would treat as a
    wildcard, does not match.
    """
    try:
        parsed = json.loads(summary)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, dict) and parsed.get("resource_id") == resource_id


def media_discovery_project_ids(
    cur: psycopg.Cursor[Any], workspace_id: UUID
) -> frozenset[UUID]:
    """The unarchived project with key 'media-discovery', plus every target of

    an active omp_work.project_relations row of kind initiative_project whose
    source is that project; no keyed project gives an empty set.
    """
    cur.execute(
        "SELECT p.project_id"
        " FROM omp_work.projects p"
        " WHERE p.workspace_id = %s AND p.key = 'media-discovery' AND NOT p.archived"
        " UNION"
        " SELECT pr.target_project_id AS project_id"
        " FROM omp_work.project_relations pr"
        " JOIN omp_work.projects p"
        "   ON p.workspace_id = pr.workspace_id AND p.project_id = pr.source_project_id"
        " WHERE pr.workspace_id = %s"
        "   AND pr.kind = 'initiative_project'"
        "   AND pr.active"
        "   AND p.key = 'media-discovery'"
        "   AND NOT p.archived",
        (workspace_id, workspace_id),
    )
    rows = cur.fetchall()
    return frozenset(
        UUID(str(row["project_id"] if isinstance(row, dict) else row[0]))
        for row in rows
    )


class ProjectAuthorityRefused(WorkStoreError):
    """Standing authority refused the change. Nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code, (code,))


class ProjectNotFound(WorkStoreError):
    """No project carries the requested identity."""

    def __init__(self, identity: str) -> None:
        super().__init__("project_not_found", (f"no project {identity}",))


class AmbiguousProjectKey(WorkStoreError):
    """More than one unarchived project carries the same key."""

    def __init__(self, key: str) -> None:
        super().__init__(
            "ambiguous_project_key",
            (f"more than one unarchived project has key {key}",),
        )


@dataclass(frozen=True)
class Tier3Approval:
    """One signed owner decision offered as authorization for a tier 3 action."""

    decision_id: UUID | str
    target_sha256: str


class ProjectStoreMixin:
    """Project records for PostgresWorkStore.

    omp_work.projects.key is not unique; the one unarchived project with a key is
    the project. More than one raises AmbiguousProjectKey. project_records rows
    are seeded by migration 0030 (backfill plus an AFTER INSERT trigger on
    omp_work.projects) so every ledger project has a record.
    """

    if TYPE_CHECKING:

        def _transaction(
            self, workspace_id: UUID, actor_id: UUID, *, serializable: bool = False
        ) -> AbstractContextManager[psycopg.Cursor[dict[str, object]]]: ...

    def ensure_project(
        self, workspace_id: UUID, actor_id: UUID, key: str, name: str, kind: str
    ) -> UUID:
        """Return the unarchived project with key, creating it (and its record) if absent."""
        with self._transaction(workspace_id, actor_id) as cur:
            project_id = self._existing_project_id(cur, workspace_id, key)
            if project_id is not None:
                return project_id
            project_id = uuid4()
            cur.execute(
                "INSERT INTO omp_work.projects(project_id, workspace_id, key, name, kind)"
                " VALUES (%s, %s, %s, %s, %s)",
                (project_id, workspace_id, key, name, kind),
            )
            return project_id

    def find_project(
        self, workspace_id: UUID, actor_id: UUID, key: str
    ) -> dict[str, object]:
        """The one unarchived project with key; zero raises ProjectNotFound."""
        with self._transaction(workspace_id, actor_id) as cur:
            cur.execute(
                f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND key=%s AND NOT archived ORDER BY project_id",
                (workspace_id, key),
            )
            rows = cur.fetchall()
            if not rows:
                raise ProjectNotFound(f"with key {key}")
            if len(rows) > 1:
                raise AmbiguousProjectKey(key)
            return row_json(rows[0]) or {}

    def list_projects(
        self, workspace_id: UUID, actor_id: UUID
    ) -> dict[str, object]:
        """Every unarchived project in the workspace, ordered by name then key."""
        with self._transaction(workspace_id, actor_id) as cur:
            cur.execute(
                f"SELECT {_LIST_FIELDS} FROM omp_work.projects"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND NOT archived ORDER BY name, key",
                (workspace_id,),
            )
            return {
                "workspace_id": str(workspace_id),
                "projects": [row_json(row) for row in cur.fetchall()],
            }

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
        """Write the project's purpose and replace-as-possible its profile rows."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            cur.execute(
                f"SELECT {_RECORD_FIELDS} FROM omp_work.project_records"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            )
            if cur.fetchone() is None:
                cur.execute(
                    "INSERT INTO omp_work.project_records(workspace_id, project_id, purpose)"
                    " VALUES (%s, %s, %s)",
                    (workspace_id, project_id, purpose),
                )
            else:
                cur.execute(
                    "UPDATE omp_work.project_records SET purpose=%s, updated_at=clock_timestamp()"
                    " WHERE workspace_id=%s AND project_id=%s",
                    (purpose, workspace_id, project_id),
                )
            for position, goal in enumerate(goals):
                cur.execute(
                    "INSERT INTO omp_work.project_goals(workspace_id, project_id, position, goal)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (workspace_id, project_id, position, goal),
                )
            for question in questions:
                cur.execute(
                    "INSERT INTO omp_work.project_questions(workspace_id, project_id, question)"
                    " VALUES (%s, %s, %s)"
                    " ON CONFLICT (workspace_id, project_id, question)"
                    " DO UPDATE SET resolved = omp_work.project_questions.resolved",
                    (workspace_id, project_id, question),
                )
            for ref in refs:
                cur.execute(
                    "INSERT INTO omp_work.project_refs(workspace_id, project_id, kind, ref, title)"
                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (
                        workspace_id,
                        project_id,
                        ref["kind"],
                        ref["ref"],
                        ref["title"],
                    ),
                )
            for repository in repositories:
                repository_id = self._upsert_repository(cur, workspace_id, repository)
                cur.execute(
                    "INSERT INTO omp_work.project_repositories"
                    "(workspace_id, project_id, repository_id, default_branch, protected_branches, automation_ci_secret_free)"
                    " VALUES (%s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (workspace_id, project_id, repository_id) DO UPDATE SET"
                    " default_branch = EXCLUDED.default_branch,"
                    " protected_branches = EXCLUDED.protected_branches,"
                    " automation_ci_secret_free = EXCLUDED.automation_ci_secret_free",
                    (
                        workspace_id,
                        project_id,
                        repository_id,
                        repository["default_branch"],
                        list(repository.get("protected_branches") or ()),
                        bool(repository.get("automation_ci_secret_free", False)),
                    ),
                )

    def link_mission(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID,
        objective: str,
        status: str,
        *,
        basis_mandate_id: UUID | None = None,
        basis_decision_id: UUID | None = None,
    ) -> None:
        """Attach or refresh a mission on the project."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            cur.execute(
                "INSERT INTO omp_work.project_missions"
                "(workspace_id, mission_id, project_id, objective, status, basis_mandate_id, basis_decision_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (workspace_id, mission_id) DO UPDATE SET"
                " objective = EXCLUDED.objective,"
                " status = EXCLUDED.status,"
                " basis_mandate_id = EXCLUDED.basis_mandate_id,"
                " basis_decision_id = EXCLUDED.basis_decision_id",
                (
                    workspace_id,
                    mission_id,
                    project_id,
                    objective,
                    status,
                    basis_mandate_id,
                    basis_decision_id,
                ),
            )

    def append_history(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        kind: str,
        summary: str,
        *,
        mission_id: UUID | None = None,
    ) -> UUID:
        """Append one project history row and return its id."""
        history_id = uuid4()
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            cur.execute(
                "INSERT INTO omp_work.project_history"
                "(workspace_id, history_id, project_id, mission_id, kind, summary)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (workspace_id, history_id, project_id, mission_id, kind, summary),
            )
        return history_id

    def record_disposable_resource(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        resource_id: str,
        policy_id: UUID | None,
    ) -> None:
        """Append a project_history row naming a disposable resource.

        A tier 2 delete may target only a resource the control plane created
        under a standing policy and recorded here when it was created
        (OMP-403): the registry is project_history rows of kind
        ``disposable_resource``, so no migration is needed.
        """
        if not resource_id:
            raise ProjectAuthorityRefused("resource_required")
        summary = _disposable_summary(resource_id, policy_id)
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            self._write_history(
                cur,
                workspace_id,
                project_id,
                "disposable_resource",
                summary,
                mission_id=mission_id,
            )

    def is_disposable_resource(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        resource_id: str,
    ) -> bool:
        """Whether the project recorded a disposable resource with this exact id.

        Matching is equality on the recorded resource id. ``LIKE`` is not used:
        ``_`` and ``%`` in an id are literal, and a shorter id does not match a
        longer one that continues after a space.
        """
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            cur.execute(
                "SELECT summary FROM omp_work.project_history"
                " WHERE workspace_id=%s AND project_id=%s"
                " AND kind='disposable_resource'",
                (workspace_id, project_id),
            )
            return any(
                _summary_names_resource(str(row["summary"]), resource_id)
                for row in cur.fetchall()
            )

    def count_missing_records(
        self, workspace_id: UUID, actor_id: UUID
    ) -> tuple[int, int]:
        """(projects, missing): ledger projects and those with no project_records row."""
        with self._transaction(workspace_id, actor_id) as cur:
            cur.execute(
                "SELECT count(*) FROM omp_work.projects WHERE workspace_id=%s",
                (workspace_id,),
            )
            projects = int((cur.fetchone() or {"count": 0})["count"])
            cur.execute(
                "SELECT count(*) FROM omp_work.projects p"
                " WHERE p.workspace_id=%s AND NOT EXISTS ("
                " SELECT 1 FROM omp_work.project_records r"
                " WHERE r.workspace_id=p.workspace_id AND r.project_id=p.project_id)",
                (workspace_id,),
            )
            missing = int((cur.fetchone() or {"count": 0})["count"])
            return projects, missing

    def read_project(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        """The whole project: record, profile, missions, mission progress and history."""
        with self._transaction(workspace_id, actor_id) as cur:
            cur.execute(
                f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            )
            project = cur.fetchone()
            if project is None:
                raise ProjectNotFound(f"id {project_id}")

            cur.execute(
                f"SELECT {_RECORD_FIELDS} FROM omp_work.project_records"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            )
            record = cur.fetchone()

            cur.execute(
                f"SELECT {_GOAL_FIELDS} FROM omp_work.project_goals"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s ORDER BY position",
                (workspace_id, project_id),
            )
            goals = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_QUESTION_FIELDS} FROM omp_work.project_questions"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s ORDER BY question",
                (workspace_id, project_id),
            )
            questions = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_REF_FIELDS} FROM omp_work.project_refs"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s ORDER BY kind, ref",
                (workspace_id, project_id),
            )
            refs = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_REPOSITORY_FIELDS} FROM omp_work.project_repositories pr"  # nosec B608 - static column list
                " JOIN omp_work.repositories r ON r.workspace_id=pr.workspace_id"
                " AND r.repository_id=pr.repository_id"
                " WHERE pr.workspace_id=%s AND pr.project_id=%s ORDER BY r.key",
                (workspace_id, project_id),
            )
            repositories = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_MISSION_FIELDS} FROM omp_work.project_missions"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s ORDER BY mission_id",
                (workspace_id, project_id),
            )
            missions = [dict(row) for row in cur.fetchall()]
            mission_progress = project_mission_progress(cur, workspace_id, project_id)

            cur.execute(
                f"SELECT {_HISTORY_FIELDS} FROM omp_work.project_history"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s ORDER BY at, history_id",
                (workspace_id, project_id),
            )
            history = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_MANDATE_FIELDS} FROM omp_work.standing_mandates"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s AND active",
                (workspace_id, project_id),
            )
            mandate = cur.fetchone()

            cur.execute(
                f"SELECT {_POLICY_FIELDS} FROM omp_work.standing_policies"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s AND active ORDER BY policy_id",
                (workspace_id, project_id),
            )
            policies = [dict(row) for row in cur.fetchall()]

            cur.execute(
                f"SELECT {_BUDGET_FIELDS} FROM omp_work.spend_budgets"  # nosec B608 - static column list
                " WHERE workspace_id=%s AND project_id=%s AND active AND mission_id IS NULL",
                (workspace_id, project_id),
            )
            budget = cur.fetchone()

            record_view = row_json(record)
            view = row_json(project) or {}
            view["purpose"] = record_view["purpose"] if record_view else None
            view["updated_at"] = record_view["updated_at"] if record_view else None
            view["goals"] = [row_json(row) for row in goals]
            view["questions"] = [row_json(row) for row in questions]
            view["refs"] = [row_json(row) for row in refs]
            view["repositories"] = [row_json(row) for row in repositories]
            view["missions"] = [row_json(row) for row in missions]
            view["open_missions"] = open_missions(cur, workspace_id, project_id)
            view["mission_progress"] = [row_json(row) for row in mission_progress]
            view["history"] = [row_json(row) for row in history]
            view["standing_mandate"] = row_json(mandate)
            view["standing_policies"] = [row_json(row) for row in policies]
            view["standing_budget"] = row_json(budget)
            return view

    def project_context(
        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
    ) -> dict[str, object]:
        """The JSON project context the stage compiler consumes (OMP-419-s01)."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)

            cur.execute(
                "SELECT kind, ref, title FROM omp_work.project_refs"
                " WHERE workspace_id=%s AND project_id=%s"
                " AND kind IN ('decision', 'roadmap') ORDER BY kind, ref",
                (workspace_id, project_id),
            )
            refs = [row_json(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT mission_id, objective, status FROM omp_work.project_missions"
                " WHERE workspace_id=%s AND project_id=%s"
                " AND status IN ('completed', 'failed', 'abandoned')"
                " ORDER BY mission_id",
                (workspace_id, project_id),
            )
            missions = [row_json(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT mission_id, kind, summary FROM omp_work.project_history"
                " WHERE workspace_id=%s AND project_id=%s AND mission_id IS NOT NULL"
                " ORDER BY at, history_id",
                (workspace_id, project_id),
            )
            history = [row_json(row) for row in cur.fetchall()]

            cur.execute(
                "SELECT c.campaign_id, c.domain, c.outcome, c.outcome_reason"
                " FROM omp_research.campaigns c"
                " JOIN omp_work.work_items wi ON wi.workspace_id=c.workspace_id"
                " AND wi.work_id=c.work_id"
                " WHERE c.workspace_id=%s AND wi.project_id=%s AND c.concluded_at IS NOT NULL"
                " ORDER BY c.concluded_at, c.campaign_id",
                (workspace_id, project_id),
            )
            research = [row_json(row) for row in cur.fetchall()]

            return {
                "refs": refs,
                "missions": missions,
                "history": history,
                "research": research,
            }

    def set_standing_mandate(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mandate: StandingMandate,
        authority: ChangeAuthority,
    ) -> None:
        """Replace the project's active mandate when the change is authorized."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            old = self._load_mandate(cur, workspace_id, project_id)
            kind = _gate(mandate_change_kind(old, mandate), authority)
            self._validate_mandate(mandate)
            mandate_id = _as_uuid(mandate.mandate_id)
            decision_id = _decision_uuid(mandate.decision_id)
            change_decision = _authority_decision(authority) if _opens_authority(kind) else None
            self._deactivate_mandate(cur, workspace_id, project_id)
            self._insert_mandate(cur, workspace_id, project_id, mandate_id, mandate, decision_id)
            history_kind = f"mandate_{kind.value}"
            if change_decision is not None:
                self._write_decision_ref(
                    cur, workspace_id, project_id, change_decision, history_kind
                )
            self._write_history(
                cur,
                workspace_id,
                project_id,
                history_kind,
                f"{mandate_id} {kind.value}",
            )

    def revoke_standing_mandate(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        authority: ChangeAuthority,
    ) -> None:
        """Clear the active mandate. Revoke records no decision."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            _gate(ChangeKind.revoke, authority)
            if self._load_mandate(cur, workspace_id, project_id) is None:
                raise ProjectAuthorityRefused("no_active_mandate")
            self._deactivate_mandate(cur, workspace_id, project_id)
            self._write_history(
                cur, workspace_id, project_id, "mandate_revoke", "revoke"
            )

    def put_standing_policy(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        policy: StandingPolicy,
        authority: ChangeAuthority,
    ) -> None:
        """Insert a policy version when the change against the active row is authorized."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            policy_id = _as_uuid(policy.policy_id)
            old = self._load_policy(cur, workspace_id, project_id, policy_id)
            kind = _gate(policy_change_kind(old, policy), authority)
            self._validate_policy(policy, self._project_repos(cur, workspace_id, project_id))
            decision_id = _decision_uuid(policy.decision_id)
            change_decision = _authority_decision(authority) if _opens_authority(kind) else None
            self._deactivate_policy(cur, workspace_id, project_id, policy_id)
            self._insert_policy(cur, workspace_id, project_id, policy_id, policy, decision_id)
            history_kind = f"policy_{kind.value}"
            if change_decision is not None:
                self._write_decision_ref(
                    cur, workspace_id, project_id, change_decision, history_kind
                )
            self._write_history(
                cur,
                workspace_id,
                project_id,
                history_kind,
                f"{policy_id} {kind.value}",
            )

    def revoke_standing_policy(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        policy_id: UUID | str,
        authority: ChangeAuthority,
    ) -> None:
        """Clear one active policy. Revoke records no decision."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            _gate(ChangeKind.revoke, authority)
            parsed_id = _as_uuid(policy_id)
            if self._load_policy(cur, workspace_id, project_id, parsed_id) is None:
                raise ProjectAuthorityRefused("no_active_policy")
            self._deactivate_policy(cur, workspace_id, project_id, parsed_id)
            self._write_history(
                cur,
                workspace_id,
                project_id,
                "policy_revoke",
                f"{parsed_id} revoke",
            )

    def set_spend_budget(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        budget: SpendBudget,
        authority: ChangeAuthority,
    ) -> None:
        """Replace the active budget for the project, or for one mission."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            old, old_decision = self._load_budget(cur, workspace_id, project_id, mission_id)
            kind = _gate(budget_change_kind(old, budget), authority)
            if _opens_authority(kind):
                decision_id = _authority_decision(authority)
            elif old_decision is None:
                raise ProjectAuthorityRefused("missing_decision")
            else:
                decision_id = old_decision
            self._deactivate_budget(cur, workspace_id, project_id, mission_id)
            self._insert_budget(
                cur, workspace_id, project_id, mission_id, budget, decision_id
            )
            history_kind = f"budget_{kind.value}"
            if _opens_authority(kind):
                self._write_decision_ref(
                    cur, workspace_id, project_id, decision_id, history_kind
                )
            self._write_history(
                cur,
                workspace_id,
                project_id,
                history_kind,
                f"{budget.budget_id} {kind.value}",
                mission_id=mission_id,
            )

    def admit_mission(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID,
        objective: str,
        draft: MissionScopeDraft,
    ) -> None:
        """Admit a mission under the active mandate and the project's ceiling.

        An approved draft that names its own ceiling also gets a spend_budgets
        row citing the mandate's decision. That insert adds no decision ref.
        """
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            mandate = self._load_mandate(cur, workspace_id, project_id)
            _budget, _decision = self._load_budget(cur, workspace_id, project_id, None)
            ceiling = None if _budget is None else _budget.ceiling_usd
            verdict = mission_scope(mandate, draft, ceiling)
            cur.execute(
                "INSERT INTO omp_work.project_missions"
                "(workspace_id, mission_id, project_id, objective, status, basis_mandate_id, basis_decision_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (workspace_id, mission_id) DO UPDATE SET"
                " objective = EXCLUDED.objective,"
                " status = EXCLUDED.status,"
                " basis_mandate_id = EXCLUDED.basis_mandate_id,"
                " basis_decision_id = EXCLUDED.basis_decision_id",
                (
                    workspace_id,
                    mission_id,
                    project_id,
                    objective,
                    verdict.status,
                    _optional_uuid(verdict.basis_mandate_id),
                    _optional_uuid(verdict.basis_decision_id),
                ),
            )
            if (
                verdict.status == "approved"
                and draft.budget_ceiling_usd is not None
                and mandate is not None
            ):
                self._deactivate_budget(cur, workspace_id, project_id, mission_id)
                self._insert_budget(
                    cur,
                    workspace_id,
                    project_id,
                    mission_id,
                    SpendBudget(
                        budget_id=str(mission_id),
                        ceiling_usd=draft.budget_ceiling_usd,
                        threshold_usd=draft.budget_threshold_usd,
                    ),
                    _decision_uuid(mandate.decision_id),
                )
            self._write_history(
                cur,
                workspace_id,
                project_id,
                "mission_admitted",
                f"{verdict.status} {objective}",
                mission_id=mission_id,
            )

    def request_action(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        action: ActionRequest,
        authority: ChangeAuthority | None,
        now: datetime,
        *,
        approval: Tier3Approval | None = None,
    ) -> UUID | None:
        """Evaluate one action against its tier and record the verdict.

        Tier 1 is allowed. Tier 2 is allowed only when an active standing policy
        covers it over the project's repositories, and the record names that
        policy. Tier 3 consults the active mandate and the mission status; an
        allowed action names the owner decision, and an outside-mandate class
        moves the mission to awaiting_confirmation. A tier 3 action may instead
        present a Tier3Approval — a signed owner decision record found through
        find_decision; when it verifies, authority is ignored and the allowed
        row names the approval's decision_id. Every verdict appends a
        project_action_records row and a history row; a refusal then raises
        ProjectAuthorityRefused with the refusal code. An allowed tier 2 action
        returns the covering policy's id so the caller can attribute a
        disposable resource to it; every other verdict returns None.
        """
        code: str | None = None
        policy_id: UUID | None = None
        decision_id: UUID | None = None
        signed = False
        tier = tier_of(action.action_class)
        with self._transaction(workspace_id, actor_id) as cur:
            if read_stop_state(cur, workspace_id)["stopped"]:
                raise ProjectAuthorityRefused("agent_stop_engaged")
            self._require_project(cur, workspace_id, project_id)
            if tier == 1:
                pass
            elif tier == 2:
                found = self._covering_policy(cur, workspace_id, project_id, action, now)
                if found is None:
                    code = "standing_policy_required"
                else:
                    policy_id = found
            elif approval is not None:
                signed, decision_id, code = self._check_tier3_approval(
                    cur, workspace_id, action, approval
                )
            else:
                signed = authority is not None and owner_signed(authority)
            if tier == 3 and code is None:
                status = self._mission_status(cur, workspace_id, project_id, mission_id)
                mandate = self._load_mandate(cur, workspace_id, project_id)
                new_status, tier3_outcome = encounter_tier3(
                    mandate,
                    status,
                    action.action_class,
                    signed,
                )
                if new_status != status and mission_id is not None:
                    cur.execute(
                        "UPDATE omp_work.project_missions SET status=%s"
                        " WHERE workspace_id=%s AND mission_id=%s",
                        (new_status, workspace_id, mission_id),
                    )
                if tier3_outcome == "allowed":
                    if decision_id is None:
                        decision_id = _authority_decision(authority)
                else:
                    code = "blocked_owner_signature"
                    decision_id = None
            outcome = "refused" if code else "allowed"
            self._insert_action_record(
                cur,
                workspace_id,
                project_id,
                mission_id,
                action,
                tier,
                outcome,
                code,
                policy_id,
                decision_id,
            )
            self._write_history(
                cur,
                workspace_id,
                project_id,
                f"action_{outcome}",
                f"{action.action_class} tier{tier}",
                mission_id=mission_id,
            )
        if code is not None:
            raise ProjectAuthorityRefused(code)
        return policy_id

    def record_spend(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        amount_usd: Decimal,
        now: datetime,
    ) -> None:
        """Authorize a spend against the mission and project budgets and record it.

        The active budgets are locked FOR UPDATE, spent is summed from
        spend_records per budget, and authorize_spend decides against the
        effective budgets and the active spend_beyond_threshold policies. A
        refusal writes nothing and raises ProjectAuthorityRefused; an allowed
        spend appends a spend_records row naming its tier and policy.
        """
        with self._transaction(workspace_id, actor_id) as cur:
            if read_stop_state(cur, workspace_id)["stopped"]:
                raise ProjectAuthorityRefused("agent_stop_engaged")
            self._require_project(cur, workspace_id, project_id)
            self._lock_mission(cur, workspace_id, project_id, mission_id)
            mission_budget, _ = self._load_budget_locked(
                cur, workspace_id, project_id, mission_id
            )
            project_budget, _ = self._load_budget_locked(
                cur, workspace_id, project_id, None
            )
            budgets = effective_budgets(mission_budget, project_budget)
            spent = self._spent_by_budget(
                cur, workspace_id, project_id, mission_id, mission_budget, project_budget
            )
            decision = authorize_spend(
                budgets,
                spent,
                amount_usd,
                self._spend_policies(cur, workspace_id, project_id),
                self._project_repos(cur, workspace_id, project_id),
                now,
            )
            if not decision.allowed:
                raise ProjectAuthorityRefused(decision.code or "spend_refused")
            cur.execute(
                "INSERT INTO omp_work.spend_records"
                " (workspace_id, record_id, project_id, mission_id, amount_usd, tier, policy_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    uuid4(),
                    project_id,
                    mission_id,
                    amount_usd,
                    decision.tier,
                    _optional_uuid(decision.policy_id),
                ),
            )

    def _existing_project_id(
        self, cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, key: str
    ) -> UUID | None:
        cur.execute(
            f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND key=%s AND NOT archived ORDER BY project_id",
            (workspace_id, key),
        )
        rows = cur.fetchall()
        if len(rows) > 1:
            raise AmbiguousProjectKey(key)
        if not rows:
            return None
        value = rows[0]["project_id"]
        return value if isinstance(value, UUID) else UUID(str(value))

    def _require_project(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> None:
        cur.execute(
            "SELECT 1 FROM omp_work.projects WHERE workspace_id=%s AND project_id=%s",
            (workspace_id, project_id),
        )
        if cur.fetchone() is None:
            raise ProjectNotFound(f"id {project_id}")

    def _upsert_repository(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        repository: dict[str, object],
    ) -> UUID:
        key = str(repository["key"])
        cur.execute(
            "INSERT INTO omp_work.repositories(repository_id, workspace_id, key, name, url)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (workspace_id, key) DO UPDATE SET"
            " name = EXCLUDED.name, url = EXCLUDED.url"
            " RETURNING repository_id",
            (
                uuid4(),
                workspace_id,
                key,
                str(repository.get("name") or key),
                str(repository["url"]),
            ),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"repository upsert returned no row for key {key}")
        value = row["repository_id"]
        return value if isinstance(value, UUID) else UUID(str(value))

    def _project_repos(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> list[RepositoryRecord]:
        cur.execute(
            f"SELECT {_REPOSITORY_FIELDS} FROM omp_work.project_repositories pr"  # nosec B608 - static column list
            " JOIN omp_work.repositories r ON r.workspace_id=pr.workspace_id"
            " AND r.repository_id=pr.repository_id"
            " WHERE pr.workspace_id=%s AND pr.project_id=%s ORDER BY r.key",
            (workspace_id, project_id),
        )
        records: list[RepositoryRecord] = []
        for row in cur.fetchall():
            protected = row["protected_branches"] or ()
            records.append(
                RepositoryRecord(
                    key=str(row["key"]),
                    default_branch=str(row["default_branch"]),
                    protected_branches=tuple(str(item) for item in protected),
                    automation_ci_secret_free=bool(row["automation_ci_secret_free"]),
                )
            )
        return records

    def _load_mandate(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> StandingMandate | None:
        cur.execute(
            f"SELECT {_MANDATE_FIELDS} FROM omp_work.standing_mandates"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active",
            (workspace_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return StandingMandate(
            mandate_id=_row_uuid(row["mandate_id"]),
            goals=row["goals"] or (),
            repositories=row["repositories"] or (),
            capabilities=row["capabilities"] or (),
            tier3_classes=row["tier3_classes"] or (),
            decision_id=_row_uuid(row["decision_id"]),
        )

    def _load_policy(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        policy_id: UUID,
    ) -> StandingPolicy | None:
        cur.execute(
            f"SELECT {_POLICY_FIELDS} FROM omp_work.standing_policies"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND policy_id=%s AND active",
            (workspace_id, project_id, policy_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return self._policy_row(row)

    def _load_budget(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> tuple[SpendBudget | None, UUID | None]:
        cur.execute(
            f"SELECT {_BUDGET_FIELDS} FROM omp_work.spend_budgets"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active"
            " AND mission_id IS NOT DISTINCT FROM %s",
            (workspace_id, project_id, mission_id),
        )
        row = cur.fetchone()
        if row is None:
            return None, None
        return (
            SpendBudget(
                budget_id=str(row["budget_id"]),
                ceiling_usd=row["ceiling_usd"],
                threshold_usd=row["threshold_usd"],
            ),
            _row_uuid(row["decision_id"]),
        )

    def _load_budget_locked(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> tuple[SpendBudget | None, UUID | None]:
        """Load one active budget FOR UPDATE so a decide-then-record spend serializes."""
        cur.execute(
            f"SELECT {_BUDGET_FIELDS} FROM omp_work.spend_budgets"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active"
            " AND mission_id IS NOT DISTINCT FROM %s FOR UPDATE",
            (workspace_id, project_id, mission_id),
        )
        row = cur.fetchone()
        if row is None:
            return None, None
        return (
            SpendBudget(
                budget_id=str(row["budget_id"]),
                ceiling_usd=row["ceiling_usd"],
                threshold_usd=row["threshold_usd"],
            ),
            _row_uuid(row["decision_id"]),
        )

    def _lock_mission(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> None:
        """Lock the mission row that the budgets and spend records hang off."""
        if mission_id is None:
            return
        cur.execute(
            "SELECT mission_id FROM omp_work.project_missions"
            " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s FOR UPDATE",
            (workspace_id, project_id, mission_id),
        )

    def _spent_by_budget(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        mission_budget: SpendBudget | None,
        project_budget: SpendBudget | None,
    ) -> dict[str, Decimal]:
        """Sum recorded spend per budget: the mission's own rows, then the project's.

        The project budget's total is every spend record on the project (mission
        rows included); the mission budget's total is only that mission's rows.
        """
        spent: dict[str, Decimal] = {}
        if project_budget is not None:
            spent[project_budget.budget_id] = self._sum_spend(
                cur, workspace_id, project_id, None
            )
        if mission_budget is not None:
            spent[mission_budget.budget_id] = self._sum_spend(
                cur, workspace_id, project_id, mission_id
            )
        return spent

    def _sum_spend(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> Decimal:
        if mission_id is None:
            cur.execute(
                "SELECT coalesce(sum(amount_usd), 0) FROM omp_work.spend_records"
                " WHERE workspace_id=%s AND project_id=%s",
                (workspace_id, project_id),
            )
        else:
            cur.execute(
                "SELECT coalesce(sum(amount_usd), 0) FROM omp_work.spend_records"
                " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
                (workspace_id, project_id, mission_id),
            )
        row = cur.fetchone()
        return _as_decimal(row["coalesce"] if row is not None else 0)

    def _covering_policy(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        action: ActionRequest,
        now: datetime,
    ) -> UUID | None:
        """The id of the first active policy that covers the action, else None."""
        repos = self._project_repos(cur, workspace_id, project_id)
        for policy in self._load_policies(cur, workspace_id, project_id):
            if covers(policy, action, repos, now):
                return _as_uuid(policy.policy_id)
        return None

    def _load_policies(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> list[StandingPolicy]:
        """Every active policy on the project, in policy_id order."""
        cur.execute(
            f"SELECT {_POLICY_FIELDS} FROM omp_work.standing_policies"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active ORDER BY policy_id",
            (workspace_id, project_id),
        )
        return [self._policy_row(row) for row in cur.fetchall()]

    def _spend_policies(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> list[StandingPolicy]:
        """The active spend_beyond_threshold policies, for authorize_spend."""
        cur.execute(
            f"SELECT {_POLICY_FIELDS} FROM omp_work.standing_policies"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active"
            " AND action_class='spend_beyond_threshold' ORDER BY policy_id",
            (workspace_id, project_id),
        )
        return [self._policy_row(row) for row in cur.fetchall()]

    def _policy_row(self, row: dict[str, object]) -> StandingPolicy:
        return StandingPolicy(
            policy_id=_row_uuid(row["policy_id"]),
            action_class=str(row["action_class"]),
            repositories=row["repositories"] or (),
            destinations=row["destinations"] or (),
            branch_patterns=row["branch_patterns"] or (),
            resource_types=row["resource_types"] or (),
            money_limit_usd=row["money_limit_usd"],
            expires_at=row["expires_at"],
            decision_id=_row_uuid(row["decision_id"]),
        )

    def _mission_status(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> str:
        """The mission's status, or 'approved' when no mission is named."""
        if mission_id is None:
            return "approved"
        cur.execute(
            "SELECT status FROM omp_work.project_missions"
            " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s",
            (workspace_id, project_id, mission_id),
        )
        row = cur.fetchone()
        if row is None:
            raise ProjectAuthorityRefused("mission_not_found")
        return str(row["status"])

    def _check_tier3_approval(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        action: ActionRequest,
        approval: Tier3Approval,
    ) -> tuple[bool, UUID | None, str | None]:
        """Verify one signed owner decision offered for a tier 3 action.

        A transaction-scoped advisory lock on the decision id serializes two
        actions that present the same approval, so one signed decision can never
        authorize two actions. Returns (signed, decision_id, code): signed True
        with the decision_id when the approval is a valid, unused owner answer;
        otherwise signed False with the first refusal code that applies.
        """
        try:
            decision_id = _as_uuid(approval.decision_id)
        except ValueError:
            return False, None, "authorization_missing"
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"omp_work.tier3_approval:{workspace_id}:{decision_id}",),
        )
        record = find_decision(cur, workspace_id, decision_id)
        if (
            record is None
            or record["status"] != "answered"
            or record["answer"] != "approve"
        ):
            return False, None, "authorization_missing"
        if record["action_class"] != action.action_class:
            return False, None, "authorization_class_mismatch"
        target = record["target_sha256"]
        if target is None or str(target) != approval.target_sha256:
            return False, None, "authorization_target_mismatch"
        expires_at = record["expires_at"]
        if expires_at is None:
            return False, None, "authorization_expired"
        cur.execute("SELECT clock_timestamp() AS now")
        row = cur.fetchone()
        if row is None or _as_datetime(expires_at) <= _as_datetime(row["now"]):
            return False, None, "authorization_expired"
        cur.execute(
            "SELECT 1 FROM omp_work.project_action_records"
            " WHERE workspace_id=%s AND decision_id=%s AND outcome='allowed' LIMIT 1",
            (workspace_id, decision_id),
        )
        if cur.fetchone() is not None:
            return False, None, "authorization_used"
        return True, decision_id, None

    def _insert_action_record(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        action: ActionRequest,
        tier: int,
        outcome: str,
        code: str | None,
        policy_id: UUID | None,
        decision_id: UUID | None,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.project_action_records"
            " (workspace_id, record_id, project_id, mission_id, action_class,"
            " repository, branch, destination, resource_type, amount_usd, tier,"
            " outcome, code, policy_id, decision_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                workspace_id,
                uuid4(),
                project_id,
                mission_id,
                action.action_class,
                action.repository,
                action.branch,
                action.destination,
                action.resource_type,
                action.amount_usd,
                tier,
                outcome,
                code,
                policy_id,
                decision_id,
            ),
        )

    def _deactivate_mandate(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
    ) -> None:
        cur.execute(
            "UPDATE omp_work.standing_mandates SET active = false"
            " WHERE workspace_id=%s AND project_id=%s AND active",
            (workspace_id, project_id),
        )

    def _deactivate_policy(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        policy_id: UUID,
    ) -> None:
        cur.execute(
            "UPDATE omp_work.standing_policies"
            " SET active = false, revoked_at = clock_timestamp()"
            " WHERE workspace_id=%s AND project_id=%s AND policy_id=%s AND active",
            (workspace_id, project_id, policy_id),
        )

    def _deactivate_budget(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
    ) -> None:
        cur.execute(
            "UPDATE omp_work.spend_budgets SET active = false"
            " WHERE workspace_id=%s AND project_id=%s AND active"
            " AND mission_id IS NOT DISTINCT FROM %s",
            (workspace_id, project_id, mission_id),
        )

    def _insert_mandate(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mandate_id: UUID,
        mandate: StandingMandate,
        decision_id: UUID,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.standing_mandates"
            " (workspace_id, record_id, project_id, mandate_id, goals, repositories,"
            " capabilities, tier3_classes, decision_id, active)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
            (
                workspace_id,
                uuid4(),
                project_id,
                mandate_id,
                _strings(mandate.goals),
                _strings(mandate.repositories),
                _strings(mandate.capabilities),
                _strings(mandate.tier3_classes),
                decision_id,
            ),
        )

    def _insert_policy(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        policy_id: UUID,
        policy: StandingPolicy,
        decision_id: UUID,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.standing_policies"
            " (workspace_id, record_id, project_id, policy_id, action_class, repositories,"
            " destinations, branch_patterns, resource_types, money_limit_usd, expires_at,"
            " decision_id, active)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true)",
            (
                workspace_id,
                uuid4(),
                project_id,
                policy_id,
                policy.action_class,
                _strings(policy.repositories),
                _strings(policy.destinations),
                _strings(policy.branch_patterns),
                _strings(policy.resource_types),
                policy.money_limit_usd,
                policy.expires_at,
                decision_id,
            ),
        )

    def _insert_budget(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        mission_id: UUID | None,
        budget: SpendBudget,
        decision_id: UUID,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.spend_budgets"
            " (workspace_id, record_id, project_id, budget_id, mission_id,"
            " ceiling_usd, threshold_usd, decision_id, active)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true)",
            (
                workspace_id,
                uuid4(),
                project_id,
                budget.budget_id,
                mission_id,
                budget.ceiling_usd,
                budget.threshold_usd,
                decision_id,
            ),
        )

    def _write_decision_ref(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        decision_id: UUID,
        title: str,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.project_refs(workspace_id, project_id, kind, ref, title)"
            " VALUES (%s, %s, 'decision', %s, %s) ON CONFLICT DO NOTHING",
            (workspace_id, project_id, str(decision_id), title),
        )

    def _write_history(
        self,
        cur: psycopg.Cursor[dict[str, object]],
        workspace_id: UUID,
        project_id: UUID,
        kind: str,
        summary: str,
        *,
        mission_id: UUID | None = None,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.project_history"
            "(workspace_id, history_id, project_id, mission_id, kind, summary)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (workspace_id, uuid4(), project_id, mission_id, kind, summary),
        )

    def _validate_mandate(self, mandate: StandingMandate) -> None:
        try:
            validate_mandate(mandate)
        except MandateRefused as exc:
            raise ProjectAuthorityRefused(exc.code) from exc

    def _validate_policy(self, policy: StandingPolicy, repos: list[RepositoryRecord]) -> None:
        try:
            validate_policy(policy, repos)
        except PolicyRefused as exc:
            raise ProjectAuthorityRefused(exc.code) from exc


def _gate(kind: ChangeKind | str, authority: ChangeAuthority) -> ChangeKind:
    try:
        parsed = kind if isinstance(kind, ChangeKind) else ChangeKind(str(kind))
    except ValueError as exc:
        raise ProjectAuthorityRefused("unknown_kind") from exc
    try:
        authorize_standing_change(parsed, authority)
    except StandingChangeRefused as exc:
        raise ProjectAuthorityRefused(exc.code) from exc
    return parsed


def _opens_authority(kind: ChangeKind) -> bool:
    return kind in (ChangeKind.create, ChangeKind.widen, ChangeKind.extend)


def _as_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _optional_uuid(value: UUID | str | None) -> UUID | None:
    if value is None or value == "":
        return None
    return _as_uuid(value)


def _decision_uuid(value: UUID | str | None) -> UUID:
    if value is None or value == "":
        raise ProjectAuthorityRefused("missing_decision")
    try:
        return _as_uuid(value)
    except ValueError as exc:
        raise ProjectAuthorityRefused("missing_decision") from exc


def _authority_decision(authority: ChangeAuthority) -> UUID:
    return _decision_uuid(authority.decision_id)


def _row_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


def _strings(values: Iterable[object]) -> list[str]:
    return sorted(str(item) for item in values)
