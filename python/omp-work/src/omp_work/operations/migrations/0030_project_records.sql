-- OMP-418: a project owns its purpose, goals, refs, repositories, missions and history.
-- omp_work.projects.key is not unique; project_records is keyed by (workspace_id, project_id)
-- and every existing project is backfilled, then kept in step by an AFTER INSERT trigger.
--
-- The backfill reads omp_work.projects and writes project_records while both tables still
-- bypass FORCE RLS for the owner (no workspace claim exists during `ops migrate`). RLS is
-- installed after the backfill, and the trigger inserts through the normal policy.
ALTER TABLE omp_work.projects NO FORCE ROW LEVEL SECURITY;

CREATE TABLE omp_work.project_records (
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  purpose text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, project_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

INSERT INTO omp_work.project_records (workspace_id, project_id, purpose, updated_at)
SELECT workspace_id, project_id, '', clock_timestamp() FROM omp_work.projects;

ALTER TABLE omp_work.projects FORCE ROW LEVEL SECURITY;

CREATE TABLE omp_work.project_goals (
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  position integer NOT NULL CHECK (position >= 0),
  goal text NOT NULL,
  PRIMARY KEY (workspace_id, project_id, position),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE TABLE omp_work.project_questions (
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  question text NOT NULL,
  resolved bool NOT NULL DEFAULT false,
  PRIMARY KEY (workspace_id, project_id, question),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE TABLE omp_work.project_refs (
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('decision','roadmap','artifact','evidence')),
  ref text NOT NULL,
  title text NOT NULL,
  PRIMARY KEY (workspace_id, project_id, kind, ref),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE TABLE omp_work.project_repositories (
  workspace_id uuid NOT NULL,
  project_id uuid NOT NULL,
  repository_id uuid NOT NULL,
  default_branch text NOT NULL,
  protected_branches text[] NOT NULL DEFAULT '{}',
  automation_ci_secret_free bool NOT NULL DEFAULT false,
  PRIMARY KEY (workspace_id, project_id, repository_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  FOREIGN KEY (workspace_id, repository_id) REFERENCES omp_work.repositories(workspace_id, repository_id)
);

CREATE TABLE omp_work.project_missions (
  workspace_id uuid NOT NULL,
  mission_id uuid NOT NULL,
  project_id uuid NOT NULL,
  objective text NOT NULL,
  status text NOT NULL CHECK (status IN ('draft','awaiting_confirmation','approved','running','paused','blocked','completed','failed','abandoned')),
  basis_mandate_id uuid,
  basis_decision_id uuid,
  PRIMARY KEY (workspace_id, mission_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE TABLE omp_work.project_history (
  workspace_id uuid NOT NULL,
  history_id uuid NOT NULL,
  project_id uuid NOT NULL,
  mission_id uuid,
  kind text NOT NULL,
  summary text NOT NULL,
  at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, history_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE OR REPLACE FUNCTION omp_control.seed_project_record() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  INSERT INTO omp_work.project_records (workspace_id, project_id, purpose, updated_at)
  VALUES (NEW.workspace_id, NEW.project_id, '', clock_timestamp());
  RETURN NEW;
END $$;
CREATE TRIGGER seed_project_record AFTER INSERT ON omp_work.projects FOR EACH ROW EXECUTE FUNCTION omp_control.seed_project_record();

SELECT omp_control.install_workspace_rls('omp_work.project_records'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_goals'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_questions'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_refs'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_repositories'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_missions'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_history'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history FROM omp_work_app, omp_work_readonly, omp_work_importer;

GRANT SELECT, INSERT ON omp_work.project_records TO omp_work_app;
GRANT UPDATE (purpose, updated_at) ON omp_work.project_records TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.project_goals, omp_work.project_refs, omp_work.project_history TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.project_questions TO omp_work_app;
GRANT UPDATE (resolved) ON omp_work.project_questions TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.project_repositories TO omp_work_app;
GRANT UPDATE (default_branch, protected_branches, automation_ci_secret_free) ON omp_work.project_repositories TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.project_missions TO omp_work_app;
GRANT UPDATE (objective, status, basis_mandate_id, basis_decision_id) ON omp_work.project_missions TO omp_work_app;
GRANT SELECT ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history TO omp_work_readonly;

-- The importer inserts projects; the AFTER INSERT trigger runs as the inserter and seeds
-- project_records, so omp_work_importer needs INSERT as well as SELECT.
GRANT SELECT, INSERT ON omp_work.project_records TO omp_work_importer;

REVOKE DELETE, TRUNCATE ON omp_work.project_records, omp_work.project_goals, omp_work.project_questions, omp_work.project_refs, omp_work.project_repositories, omp_work.project_missions, omp_work.project_history FROM omp_work_app, omp_work_readonly, omp_work_importer;
