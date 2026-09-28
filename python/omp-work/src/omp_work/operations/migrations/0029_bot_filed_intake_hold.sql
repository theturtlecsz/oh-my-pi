-- OMP-407: record who filed each item and track owner intake decisions.

ALTER TABLE omp_work.work_items NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.work_items ADD COLUMN filed_by_kind text NULL;

CREATE TABLE omp_work.intake_decisions (
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  answer text NOT NULL CHECK (answer = 'approve'),
  answered_by text NOT NULL,
  answered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  operation_id uuid NOT NULL,
  PRIMARY KEY (workspace_id, work_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id)
);

SELECT omp_control.install_workspace_rls('omp_work.intake_decisions'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.intake_decisions FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.intake_decisions TO omp_work_app;
GRANT SELECT ON omp_work.intake_decisions TO omp_work_readonly;
REVOKE DELETE, TRUNCATE ON omp_work.intake_decisions FROM omp_work_app, omp_work_readonly, omp_work_importer;

ALTER TABLE omp_work.work_items FORCE ROW LEVEL SECURITY;
