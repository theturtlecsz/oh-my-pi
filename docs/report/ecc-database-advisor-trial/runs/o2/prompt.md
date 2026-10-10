Review the database changes in commit b2a5ca700d2d48216134cdc074d1b611bdd0914a.

## Scope
The following files are in scope for this review:
- python/omp-work/src/omp_work/project_store.py
- python/omp-work/src/omp_work/operations/migrations/0030_project_records.sql
- python/omp-work/src/omp_work/v1/store.py

## Instructions
1. Read the scope files to understand their context and current implementation.
2. Describe the change and inspect for any database issues, concurrency bugs, migration defects, or data integrity risks.
3. Make no edits to any files. This is a read-only review.

## Diff
```diff
diff --git a/python/omp-work/src/omp_work/operations/migrations/0030_project_records.sql b/python/omp-work/src/omp_work/operations/migrations/0030_project_records.sql
new file mode 100644
index 0000000..58952a1
--- /dev/null
+++ b/python/omp-work/src/omp_work/operations/migrations/0030_project_records.sql
@@ -0,0 +1,121 @@
+-- OMP-418: a project owns its purpose, goals, refs, repositories, missions and history.
+-- omp_work.projects.key is not unique; project_records is keyed by (workspace_id, project_id)
+-- and every existing project is backfilled, then kept in step by an AFTER INSERT trigger.
+--
+-- The backfill reads omp_work.projects and writes project_records while both tables still
+-- bypass FORCE RLS for the owner (no workspace claim exists during `ops migrate`). RLS is
+-- installed after the backfill, and the trigger inserts through the normal policy.
+ALTER TABLE omp_work.projects NO FORCE ROW LEVEL SECURITY;
+
+CREATE TABLE omp_work.project_records (
+  workspace_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  purpose text NOT NULL DEFAULT '',
+  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
+  PRIMARY KEY (workspace_id, project_id),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+INSERT INTO omp_work.project_records (workspace_id, project_id, purpose, updated_at)
+SELECT workspace_id, project_id, '', clock_timestamp() FROM omp_work.projects;
+
+ALTER TABLE omp_work.projects FORCE ROW LEVEL SECURITY;
+
+CREATE TABLE omp_work.project_goals (
+  workspace_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  position integer NOT NULL CHECK (position >= 0),
+  goal text NOT NULL,
+  PRIMARY KEY (workspace_id, project_id, position),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+CREATE TABLE omp_work.project_questions (
+  workspace_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  question text NOT NULL,
+  resolved bool NOT NULL DEFAULT false,
+  PRIMARY KEY (workspace_id, project_id, question),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+CREATE TABLE omp_work.project_refs (
+  workspace_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  kind text NOT NULL CHECK (kind IN ('decision','roadmap','artifact','evidence')),
+  ref text NOT NULL,
+  title text NOT NULL,
+  PRIMARY KEY (workspace_id, project_id, kind, ref),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+CREATE TABLE omp_work.project_repositories (
+  workspace_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  repository_id uuid NOT NULL,
+  default_branch text NOT NULL,
+  protected_branches text[] NOT NULL DEFAULT '{}',
+  automation_ci_secret_free bool NOT NULL DEFAULT false,
+  PRIMARY KEY (workspace_id, project_id, repository_id),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
+  FOREIGN KEY (workspace_id, repository_id) REFERENCES omp_work.repositories(workspace_id, repository_id)
+);
+
+CREATE TABLE omp_work.project_missions (
+  workspace_id uuid NOT NULL,
+  mission_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  objective text NOT NULL,
+  status text NOT NULL CHECK (status IN ('draft','awaiting_confirmation','approved','running','paused','blocked','completed','failed','abandoned')),
+  basis_mandate_id uuid,
+  basis_decision_id uuid,
+  PRIMARY KEY (workspace_id, mission_id),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+CREATE TABLE omp_work.project_history (
+  workspace_id uuid NOT NULL,
+  history_id uuid NOT NULL,
+  project_id uuid NOT NULL,
+  mission_id uuid,
+  kind text NOT NULL,
+  summary text NOT NULL,
+  at timestamptz NOT NULL DEFAULT clock_timestamp(),
+  PRIMARY KEY (workspace_id, history_id),
+  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
+);
+
+CREATE OR REPLACE FUNCTION omp_control.seed_project_record() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
+BEGIN
+  INSERT INTO omp_work.project_records (workspace_id, project_id, purpose, updated_at)
+  VALUES (NEW.workspace_id, NEW.project_id, '', clock_timestamp());
+  RETURN NEW;
+END $$;
+CREATE TRIGGER seed_project_record AFTER INSERT ON omp_work.projects FOR EACH ROW EXECUTE FUNCTION omp_control.seed_project_record();
+
+SELECT omp_control.install_workspace_rls('omp_work.project_records'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_goals'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_questions'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_refs'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_repositories'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_missions'::regclass, 'workspace_id');
+SELECT omp_control.install_workspace_rls('omp_work.project_history'::regclass, 'workspace_id');
+
+REVOKE ALL ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history FROM omp_work_app, omp_work_readonly, omp_work_importer;
+
+GRANT SELECT, INSERT ON omp_work.project_records TO omp_work_app;
+GRANT UPDATE (purpose, updated_at) ON omp_work.project_records TO omp_work_app;
+GRANT SELECT, INSERT ON omp_work.project_goals, omp_work.project_refs, omp_work.project_history TO omp_work_app;
+GRANT SELECT, INSERT ON omp_work.project_questions TO omp_work_app;
+GRANT UPDATE (resolved) ON omp_work.project_questions TO omp_work_app;
+GRANT SELECT, INSERT ON omp_work.project_repositories TO omp_work_app;
+GRANT UPDATE (default_branch, protected_branches, automation_ci_secret_free) ON omp_work.project_repositories TO omp_work_app;
+GRANT SELECT, INSERT ON omp_work.project_missions TO omp_work_app;
+GRANT UPDATE (objective, status, basis_mandate_id, basis_decision_id) ON omp_work.project_missions TO omp_work_app;
+GRANT SELECT ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history TO omp_work_readonly;
+
+-- The importer inserts projects; the AFTER INSERT trigger runs as the inserter and seeds
+-- project_records, so omp_work_importer needs INSERT as well as SELECT.
+GRANT SELECT, INSERT ON omp_work.project_records TO omp_work_importer;
+
+REVOKE DELETE, TRUNCATE ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history FROM omp_work_app, omp_work_readonly, omp_work_importer;
diff --git a/python/omp-work/src/omp_work/project_store.py b/python/omp-work/src/omp_work/project_store.py
new file mode 100644
index 0000000..b418b44
--- /dev/null
+++ b/python/omp-work/src/omp_work/project_store.py
@@ -0,0 +1,389 @@
+"""OMP-418: project records, profile, missions and history.
+
+The store owns project_records/project_goals/project_questions/project_refs/
+project_repositories/project_missions/project_history (migration 0030) on top of
+the existing omp_work.projects and omp_work.repositories rows. PostgresWorkStore
+mixes this in; the host supplies _transaction and the workspace/actor claims.
+"""
+
+from __future__ import annotations
+
+from collections.abc import Iterable
+from typing import TYPE_CHECKING
+from uuid import UUID, uuid4
+
+import psycopg
+
+from omp_work.v1.store_shared import WorkStoreError, row_json
+
+if TYPE_CHECKING:
+    from contextlib import AbstractContextManager
+
+_PROJECT_FIELDS = "project_id,key,name,kind,archived"
+_RECORD_FIELDS = "workspace_id,project_id,purpose,updated_at"
+_GOAL_FIELDS = "workspace_id,project_id,position,goal"
+_QUESTION_FIELDS = "workspace_id,project_id,question,resolved"
+_REF_FIELDS = "workspace_id,project_id,kind,ref,title"
+_REPOSITORY_FIELDS = (
+    "pr.repository_id,r.key,r.name,r.url,pr.default_branch,"
+    "pr.protected_branches,pr.automation_ci_secret_free"
+)
+_MISSION_FIELDS = (
+    "workspace_id,mission_id,project_id,objective,status,"
+    "basis_mandate_id,basis_decision_id"
+)
+_HISTORY_FIELDS = "workspace_id,history_id,project_id,mission_id,kind,summary,at"
+
+
+class ProjectNotFound(WorkStoreError):
+    """No project carries the requested identity."""
+
+    def __init__(self, identity: str) -> None:
+        super().__init__("project_not_found", (f"no project {identity}",))
+
+
+class AmbiguousProjectKey(WorkStoreError):
+    """More than one unarchived project carries the same key."""
+
+    def __init__(self, key: str) -> None:
+        super().__init__(
+            "ambiguous_project_key",
+            (f"more than one unarchived project has key {key}",),
+        )
+
+
+class ProjectStoreMixin:
+    """Project records for PostgresWorkStore.
+
+    omp_work.projects.key is not unique; the one unarchived project with a key is
+    the project. More than one raises AmbiguousProjectKey. project_records rows
+    are seeded by migration 0030 (backfill plus an AFTER INSERT trigger on
+    omp_work.projects) so every ledger project has a record.
+    """
+
+    if TYPE_CHECKING:
+
+        def _transaction(
+            self, workspace_id: UUID, actor_id: UUID, *, serializable: bool = False
+        ) -> AbstractContextManager[psycopg.Cursor[dict[str, object]]]: ...
+
+    def ensure_project(
+        self, workspace_id: UUID, actor_id: UUID, key: str, name: str, kind: str
+    ) -> UUID:
+        """Return the unarchived project with key, creating it (and its record) if absent."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            project_id = self._existing_project_id(cur, workspace_id, key)
+            if project_id is not None:
+                return project_id
+            project_id = uuid4()
+            cur.execute(
+                "INSERT INTO omp_work.projects(project_id, workspace_id, key, name, kind)"
+                " VALUES (%s, %s, %s, %s, %s)",
+                (project_id, workspace_id, key, name, kind),
+            )
+            return project_id
+
+    def find_project(
+        self, workspace_id: UUID, actor_id: UUID, key: str
+    ) -> dict[str, object]:
+        """The one unarchived project with key; zero raises ProjectNotFound."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            cur.execute(
+                f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"
+                " WHERE workspace_id=%s AND key=%s AND NOT archived ORDER BY project_id",
+                (workspace_id, key),
+            )
+            rows = cur.fetchall()
+            if not rows:
+                raise ProjectNotFound(f"with key {key}")
+            if len(rows) > 1:
+                raise AmbiguousProjectKey(key)
+            return row_json(rows[0]) or {}
+
+    def update_profile(
+        self,
+        workspace_id: UUID,
+        actor_id: UUID,
+        project_id: UUID,
+        *,
+        purpose: str = "",
+        goals: Iterable[str] = (),
+        questions: Iterable[str] = (),
+        refs: Iterable[dict[str, str]] = (),
+        repositories: Iterable[dict[str, object]] = (),
+    ) -> None:
+        """Write the project's purpose and replace-as-possible its profile rows."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            self._require_project(cur, workspace_id, project_id)
+            cur.execute(
+                f"SELECT {_RECORD_FIELDS} FROM omp_work.project_records"
+                " WHERE workspace_id=%s AND project_id=%s",
+                (workspace_id, project_id),
+            )
+            if cur.fetchone() is None:
+                cur.execute(
+                    "INSERT INTO omp_work.project_records(workspace_id, project_id, purpose)"
+                    " VALUES (%s, %s, %s)",
+                    (workspace_id, project_id, purpose),
+                )
+            else:
+                cur.execute(
+                    "UPDATE omp_work.project_records SET purpose=%s, updated_at=clock_timestamp()"
+                    " WHERE workspace_id=%s AND project_id=%s",
+                    (purpose, workspace_id, project_id),
+                )
+            for position, goal in enumerate(goals):
+                cur.execute(
+                    "INSERT INTO omp_work.project_goals(workspace_id, project_id, position, goal)"
+                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
+                    (workspace_id, project_id, position, goal),
+                )
+            for question in questions:
+                cur.execute(
+                    "INSERT INTO omp_work.project_questions(workspace_id, project_id, question)"
+                    " VALUES (%s, %s, %s)"
+                    " ON CONFLICT (workspace_id, project_id, question)"
+                    " DO UPDATE SET resolved = omp_work.project_questions.resolved",
+                    (workspace_id, project_id, question),
+                )
+            for ref in refs:
+                cur.execute(
+                    "INSERT INTO omp_work.project_refs(workspace_id, project_id, kind, ref, title)"
+                    " VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
+                    (
+                        workspace_id,
+                        project_id,
+                        ref["kind"],
+                        ref["ref"],
+                        ref["title"],
+                    ),
+                )
+            for repository in repositories:
+                repository_id = self._upsert_repository(cur, workspace_id, repository)
+                cur.execute(
+                    "INSERT INTO omp_work.project_repositories"
+                    "(workspace_id, project_id, repository_id, default_branch, protected_branches, automation_ci_secret_free)"
+                    " VALUES (%s, %s, %s, %s, %s, %s)"
+                    " ON CONFLICT (workspace_id, project_id, repository_id) DO UPDATE SET"
+                    " default_branch = EXCLUDED.default_branch,"
+                    " protected_branches = EXCLUDED.protected_branches,"
+                    " automation_ci_secret_free = EXCLUDED.automation_ci_secret_free",
+                    (
+                        workspace_id,
+                        project_id,
+                        repository_id,
+                        repository["default_branch"],
+                        list(repository.get("protected_branches") or ()),
+                        bool(repository.get("automation_ci_secret_free", False)),
+                    ),
+                )
+
+    def link_mission(
+        self,
+        workspace_id: UUID,
+        actor_id: UUID,
+        project_id: UUID,
+        mission_id: UUID,
+        objective: str,
+        status: str,
+        *,
+        basis_mandate_id: UUID | None = None,
+        basis_decision_id: UUID | None = None,
+    ) -> None:
+        """Attach or refresh a mission on the project."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            self._require_project(cur, workspace_id, project_id)
+            cur.execute(
+                "INSERT INTO omp_work.project_missions"
+                "(workspace_id, mission_id, project_id, objective, status, basis_mandate_id, basis_decision_id)"
+                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
+                " ON CONFLICT (workspace_id, mission_id) DO UPDATE SET"
+                " objective = EXCLUDED.objective,"
+                " status = EXCLUDED.status,"
+                " basis_mandate_id = EXCLUDED.basis_mandate_id,"
+                " basis_decision_id = EXCLUDED.basis_decision_id",
+                (
+                    workspace_id,
+                    mission_id,
+                    project_id,
+                    objective,
+                    status,
+                    basis_mandate_id,
+                    basis_decision_id,
+                ),
+            )
+
+    def append_history(
+        self,
+        workspace_id: UUID,
+        actor_id: UUID,
+        project_id: UUID,
+        kind: str,
+        summary: str,
+        *,
+        mission_id: UUID | None = None,
+    ) -> UUID:
+        """Append one project history row and return its id."""
+        history_id = uuid4()
+        with self._transaction(workspace_id, actor_id) as cur:
+            self._require_project(cur, workspace_id, project_id)
+            cur.execute(
+                "INSERT INTO omp_work.project_history"
+                "(workspace_id, history_id, project_id, mission_id, kind, summary)"
+                " VALUES (%s, %s, %s, %s, %s, %s)",
+                (workspace_id, history_id, project_id, mission_id, kind, summary),
+            )
+        return history_id
+
+    def count_missing_records(
+        self, workspace_id: UUID, actor_id: UUID
+    ) -> tuple[int, int]:
+        """(projects, missing): ledger projects and those with no project_records row."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            cur.execute(
+                "SELECT count(*) FROM omp_work.projects WHERE workspace_id=%s",
+                (workspace_id,),
+            )
+            projects = int((cur.fetchone() or {"count": 0})["count"])
+            cur.execute(
+                "SELECT count(*) FROM omp_work.projects p"
+                " WHERE p.workspace_id=%s AND NOT EXISTS ("
+                " SELECT 1 FROM omp_work.project_records r"
+                " WHERE r.workspace_id=p.workspace_id AND r.project_id=p.project_id)",
+                (workspace_id,),
+            )
+            missing = int((cur.fetchone() or {"count": 0})["count"])
+            return projects, missing
+
+    def read_project(
+        self, workspace_id: UUID, actor_id: UUID, project_id: UUID
+    ) -> dict[str, object]:
+        """The whole project: record, profile, missions and history."""
+        with self._transaction(workspace_id, actor_id) as cur:
+            cur.execute(
+                f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"
+                " WHERE workspace_id=%s AND project_id=%s",
+                (workspace_id, project_id),
+            )
+            project = cur.fetchone()
+            if project is None:
+                raise ProjectNotFound(f"id {project_id}")
+
+            cur.execute(
+                f"SELECT {_RECORD_FIELDS} FROM omp_work.project_records"
+                " WHERE workspace_id=%s AND project_id=%s",
+                (workspace_id, project_id),
+            )
+            record = cur.fetchone()
+
+            cur.execute(
+                f"SELECT {_GOAL_FIELDS} FROM omp_work.project_goals"
+                " WHERE workspace_id=%s AND project_id=%s ORDER BY position",
+                (workspace_id, project_id),
+            )
+            goals = [dict(row) for row in cur.fetchall()]
+
+            cur.execute(
+                f"SELECT {_QUESTION_FIELDS} FROM omp_work.project_questions"
+                " WHERE workspace_id=%s AND project_id=%s ORDER BY question",
+                (workspace_id, project_id),
+            )
+            questions = [dict(row) for row in cur.fetchall()]
+
+            cur.execute(
+                f"SELECT {_REF_FIELDS} FROM omp_work.project_refs"
+                " WHERE workspace_id=%s AND project_id=%s ORDER BY kind, ref",
+                (workspace_id, project_id),
+            )
+            refs = [dict(row) for row in cur.fetchall()]
+
+            cur.execute(
+                f"SELECT {_REPOSITORY_FIELDS} FROM omp_work.project_repositories pr"
+                " JOIN omp_work.repositories r ON r.workspace_id=pr.workspace_id"
+                " AND r.repository_id=pr.repository_id"
+                " WHERE pr.workspace_id=%s AND pr.project_id=%s ORDER BY r.key",
+                (workspace_id, project_id),
+            )
+            repositories = [dict(row) for row in cur.fetchall()]
+
+            cur.execute(
+                f"SELECT {_MISSION_FIELDS} FROM omp_work.project_missions"
+                " WHERE workspace_id=%s AND project_id=%s ORDER BY mission_id",
+                (workspace_id, project_id),
+            )
+            missions = [dict(row) for row in cur.fetchall()]
+
+            cur.execute(
+                f"SELECT {_HISTORY_FIELDS} FROM omp_work.project_history"
+                " WHERE workspace_id=%s AND project_id=%s ORDER BY at, history_id",
+                (workspace_id, project_id),
+            )
+            history = [dict(row) for row in cur.fetchall()]
+
+            record_view = row_json(record)
+            view = row_json(project) or {}
+            view["purpose"] = record_view["purpose"] if record_view else None
+            view["updated_at"] = record_view["updated_at"] if record_view else None
+            view["goals"] = [row_json(row) for row in goals]
+            view["questions"] = [row_json(row) for row in questions]
+            view["refs"] = [row_json(row) for row in refs]
+            view["repositories"] = [row_json(row) for row in repositories]
+            view["missions"] = [row_json(row) for row in missions]
+            view["history"] = [row_json(row) for row in history]
+            return view
+
+    def _existing_project_id(
+        self, cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, key: str
+    ) -> UUID | None:
+        cur.execute(
+            f"SELECT {_PROJECT_FIELDS} FROM omp_work.projects"
+            " WHERE workspace_id=%s AND key=%s AND NOT archived ORDER BY project_id",
+            (workspace_id, key),
+        )
+        rows = cur.fetchall()
+        if len(rows) > 1:
+            raise AmbiguousProjectKey(key)
+        if not rows:
+            return None
+        value = rows[0]["project_id"]
+        return value if isinstance(value, UUID) else UUID(str(value))
+
+    def _require_project(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        project_id: UUID,
+    ) -> None:
+        cur.execute(
+            "SELECT 1 FROM omp_work.projects WHERE workspace_id=%s AND project_id=%s",
+            (workspace_id, project_id),
+        )
+        if cur.fetchone() is None:
+            raise ProjectNotFound(f"id {project_id}")
+
+    def _upsert_repository(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        repository: dict[str, object],
+    ) -> UUID:
+        key = str(repository["key"])
+        cur.execute(
+            "INSERT INTO omp_work.repositories(repository_id, workspace_id, key, name, url)"
+            " VALUES (%s, %s, %s, %s, %s)"
+            " ON CONFLICT (workspace_id, key) DO UPDATE SET"
+            " name = EXCLUDED.name, url = EXCLUDED.url"
+            " RETURNING repository_id",
+            (
+                uuid4(),
+                workspace_id,
+                key,
+                str(repository.get("name") or key),
+                str(repository["url"]),
+            ),
+        )
+        row = cur.fetchone()
+        if row is None:
+            raise ValueError(f"repository upsert returned no row for key {key}")
+        value = row["repository_id"]
+        return value if isinstance(value, UUID) else UUID(str(value))
diff --git a/python/omp-work/src/omp_work/v1/store.py b/python/omp-work/src/omp_work/v1/store.py
index 9aef941..6f1f85e 100644
--- a/python/omp-work/src/omp_work/v1/store.py
+++ b/python/omp-work/src/omp_work/v1/store.py
@@ -64,6 +64,7 @@ from .models import (
     RiderProof,
     SameSessionFoundFixedPayload,
 )
+from omp_work.project_store import ProjectStoreMixin
 from omp_work.research.store import ResearchStoreMixin
 from .store_shared import WorkStoreError
 from .store_shared import row_json as _row_json
@@ -389,7 +390,7 @@ ) -> dict[UUID, IntakeClassification]:
     return {wid: classified[wid] for wid in target_ids if wid in classified}


-class PostgresWorkStore(ResearchStoreMixin):
+class PostgresWorkStore(ProjectStoreMixin, ResearchStoreMixin):
     def __init__(self, config: OperationsConfig) -> None:
         self._config = config


```