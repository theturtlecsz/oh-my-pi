"""OMP-431: the project egress policy and its audit records for PostgresWorkStore.

The store owns ``project_egress_policies`` (the owner-signed project egress
record: registries and repository remotes, one active row per project,
migration 0033) and ``egress_records`` (one append-only row per ``EgressRecord``).
A put inserts a new policy version once the change against the active row is
authorized, clearing ``active`` on the row it replaces; ``omp_work_app`` may
UPDATE only ``active``, so a recorded version keeps its registries, remotes and
decision.

``load_egress_policy`` compiles the active row and the active standing policies
through :func:`build_policy` — a project clone's ``.npmrc``, lockfile URLs,
``.gitmodules`` or git remotes never contribute.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from omp_work import egress_policy
from omp_work.egress_policy import (
    EgressPolicy,
    EgressRecord,
    ProjectEgress,
    build_policy,
    egress_change_kind,
)
from omp_work.project_store import ProjectAuthorityRefused, _decision_uuid, _gate
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.store_shared import row_json as _row_json

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

__all__ = ["EgressStoreMixin", "StoreEgressRecorder"]

_EGRESS_FIELDS = "registries,remotes,decision_id"
_RECORD_FIELDS = (
    "record_id,project_id,mission_id,worker_id,stage,channel,protocol,host,ip,"
    "port,method,url,klass,outcome,code,policy_id,at"
)


class EgressStoreMixin:
    """Project egress policy and egress records for PostgresWorkStore.

    The host supplies the workspace/actor claims and the project guard:
    ``_transaction``, ``_require_project``, ``_load_policies`` and ``_policy_row``.
    """

    if TYPE_CHECKING:

        def _transaction(
            self, workspace_id: UUID, actor_id: UUID, *, serializable: bool = False
        ) -> AbstractContextManager[psycopg.Cursor[dict_row]]: ...

        def _require_project(
            self,
            cur: psycopg.Cursor[dict_row],
            workspace_id: UUID,
            project_id: UUID,
        ) -> None: ...

        def _load_policies(
            self,
            cur: psycopg.Cursor[dict_row],
            workspace_id: UUID,
            project_id: UUID,
        ) -> list[StandingPolicy]: ...

    def put_project_egress(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        egress: ProjectEgress,
        authority: ChangeAuthority,
    ) -> None:
        """Insert an egress version once the change against the active row is authorized.

        The change kind comes from :func:`egress_change_kind`; a refusal raises
        ``ProjectAuthorityRefused`` and writes nothing. Remotes must already be
        a repository URL of the project (else ``unknown_remote``) and registries
        a bare https origin (else ``invalid_registry``).
        """
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            old = self._load_project_egress(cur, workspace_id, project_id)
            _gate(egress_change_kind(old, egress), authority)
            self._validate_egress(cur, workspace_id, project_id, egress)
            decision_id = _decision_uuid(egress.decision_id)
            self._deactivate_egress(cur, workspace_id, project_id)
            self._insert_egress(cur, workspace_id, project_id, egress, decision_id)

    def load_egress_policy(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        now: datetime,
        model_proxy: str,
    ) -> EgressPolicy:
        """Compile the active egress row and the active standing policies.

        ``now`` is the caller's decision clock. The compiled policy carries the
        registries and remotes of the active row only, so nothing the project's
        checkout contains can widen it.
        """
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            egress = self._load_project_egress(cur, workspace_id, project_id)
            standing = self._load_policies(cur, workspace_id, project_id)
            return build_policy(egress, standing, model_proxy)

    def record_egress(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        rec: EgressRecord,
    ) -> None:
        """Append one egress verdict record. ``mission_id`` carries no foreign key."""
        with self._transaction(workspace_id, actor_id) as cur:
            cur.execute(
                "INSERT INTO omp_work.egress_records"
                " (workspace_id, record_id, project_id, mission_id, worker_id, stage,"
                " channel, protocol, host, ip, port, method, url, klass, outcome, code,"
                " policy_id, at)"
                " VALUES (%s, %s, %s::uuid, %s::uuid, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s)",
                (
                    workspace_id,
                    uuid4(),
                    rec.project_id,
                    rec.mission_id,
                    rec.worker_id,
                    rec.stage,
                    rec.channel,
                    rec.protocol,
                    rec.host,
                    rec.ip,
                    rec.port,
                    rec.method,
                    rec.url,
                    rec.klass,
                    rec.outcome,
                    rec.code,
                    rec.policy_id,
                    rec.at,
                ),
            )

    def list_egress_records(
        self,
        workspace_id: UUID,
        actor_id: UUID,
        project_id: UUID,
        mission_id: UUID | None = None,
    ) -> list[dict[str, object]]:
        """Every egress record on the project, optionally only one mission's."""
        with self._transaction(workspace_id, actor_id) as cur:
            self._require_project(cur, workspace_id, project_id)
            if mission_id is None:
                cur.execute(
                    f"SELECT {_RECORD_FIELDS} FROM omp_work.egress_records"  # nosec B608 - static column list
                    " WHERE workspace_id=%s AND project_id=%s ORDER BY at, record_id",
                    (workspace_id, project_id),
                )
            else:
                cur.execute(
                    f"SELECT {_RECORD_FIELDS} FROM omp_work.egress_records"  # nosec B608 - static column list
                    " WHERE workspace_id=%s AND project_id=%s AND mission_id=%s"
                    " ORDER BY at, record_id",
                    (workspace_id, project_id, mission_id),
                )
            return [_row_json(row) or {} for row in cur.fetchall()]

    def _load_project_egress(
        self,
        cur: psycopg.Cursor[dict_row],
        workspace_id: UUID,
        project_id: UUID,
    ) -> ProjectEgress | None:
        cur.execute(
            f"SELECT {_EGRESS_FIELDS} FROM omp_work.project_egress_policies"  # nosec B608 - static column list
            " WHERE workspace_id=%s AND project_id=%s AND active",
            (workspace_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return ProjectEgress(
            registries=tuple(row["registries"] or ()),
            remotes=tuple(row["remotes"] or ()),
            decision_id=str(row["decision_id"]),
        )

    def _validate_egress(
        self,
        cur: psycopg.Cursor[dict_row],
        workspace_id: UUID,
        project_id: UUID,
        egress: ProjectEgress,
    ) -> None:
        """Refuse an egress whose remotes or registries are not project entries."""
        known = self._project_repository_targets(cur, workspace_id, project_id)
        for raw in egress.remotes:
            parsed = egress_policy._parse_remote(raw)
            if parsed is None or parsed not in known:
                raise ProjectAuthorityRefused("unknown_remote")
        for raw in egress.registries:
            if egress_policy._parse_registry(raw) is None:
                raise ProjectAuthorityRefused("invalid_registry")

    def _project_repository_targets(
        self,
        cur: psycopg.Cursor[dict_row],
        workspace_id: UUID,
        project_id: UUID,
    ) -> set[tuple[str, str, int, str]]:
        """The project repositories' URLs in the remote parse shape."""
        cur.execute(
            "SELECT r.url FROM omp_work.project_repositories pr"
            " JOIN omp_work.repositories r ON r.workspace_id=pr.workspace_id"
            " AND r.repository_id=pr.repository_id"
            " WHERE pr.workspace_id=%s AND pr.project_id=%s",
            (workspace_id, project_id),
        )
        targets: set[tuple[str, str, int, str]] = set()
        for row in cur.fetchall():
            parsed = egress_policy._parse_remote(row["url"])
            if parsed is not None:
                targets.add(parsed)
        return targets

    def _deactivate_egress(
        self,
        cur: psycopg.Cursor[dict_row],
        workspace_id: UUID,
        project_id: UUID,
    ) -> None:
        cur.execute(
            "UPDATE omp_work.project_egress_policies SET active = false"
            " WHERE workspace_id=%s AND project_id=%s AND active",
            (workspace_id, project_id),
        )

    def _insert_egress(
        self,
        cur: psycopg.Cursor[dict_row],
        workspace_id: UUID,
        project_id: UUID,
        egress: ProjectEgress,
        decision_id: UUID,
    ) -> None:
        cur.execute(
            "INSERT INTO omp_work.project_egress_policies"
            " (workspace_id, record_id, project_id, registries, remotes, decision_id, active)"
            " VALUES (%s, %s, %s, %s, %s, %s, true)",
            (
                workspace_id,
                uuid4(),
                project_id,
                list(egress.registries),
                list(egress.remotes),
                decision_id,
            ),
        )


class StoreEgressRecorder:
    """EgressRecorder that appends each verdict to the store."""

    def __init__(
        self, store: EgressStoreMixin, workspace_id: UUID, actor_id: UUID
    ) -> None:
        self._store = store
        self._workspace_id = workspace_id
        self._actor_id = actor_id

    def record(self, rec: EgressRecord) -> None:
        self._store.record_egress(self._workspace_id, self._actor_id, rec)
