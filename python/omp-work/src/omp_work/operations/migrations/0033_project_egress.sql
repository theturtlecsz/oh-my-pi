-- OMP-431: the owner-signed project egress policy and its audit records.
-- A change inserts a new project_egress_policies row and clears active on the
-- row it replaces; omp_work_app may UPDATE only active, so a recorded version
-- keeps its registries, remotes and decision_id. decision_id is NOT NULL and
-- the partial unique index allows only one active row per project.
--
-- egress_records keeps one append-only row per EgressRecord field (record_id is
-- the primary-key bookkeeping column). mission_id carries no foreign key: a
-- record survives the mission row it names. omp_work_app may only SELECT and
-- INSERT, so a recorded verdict is immutable.

CREATE TABLE omp_work.project_egress_policies (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  registries text[] NOT NULL DEFAULT '{}',
  remotes text[] NOT NULL DEFAULT '{}',
  decision_id uuid NOT NULL,
  active bool NOT NULL DEFAULT true,
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE UNIQUE INDEX project_egress_one_active
  ON omp_work.project_egress_policies (workspace_id, project_id)
  WHERE active;

CREATE TABLE omp_work.egress_records (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  mission_id uuid,
  worker_id text NOT NULL,
  stage text NOT NULL CHECK (stage IN ('repository', 'research')),
  channel text NOT NULL,
  protocol text NOT NULL,
  host text NOT NULL,
  ip text,
  port integer NOT NULL,
  method text NOT NULL,
  url text NOT NULL,
  klass text NOT NULL,
  outcome text NOT NULL CHECK (outcome IN ('allowed', 'refused')),
  code text,
  policy_id text,
  at timestamptz NOT NULL,
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

SELECT omp_control.install_workspace_rls('omp_work.project_egress_policies'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.egress_records'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.project_egress_policies, omp_work.egress_records FROM omp_work_app, omp_work_readonly, omp_work_importer;

GRANT SELECT, INSERT ON omp_work.project_egress_policies, omp_work.egress_records TO omp_work_app;
GRANT UPDATE (active) ON omp_work.project_egress_policies TO omp_work_app;
GRANT SELECT ON omp_work.project_egress_policies, omp_work.egress_records TO omp_work_readonly;

REVOKE DELETE, TRUNCATE ON omp_work.project_egress_policies, omp_work.egress_records FROM omp_work_app, omp_work_readonly, omp_work_importer;
