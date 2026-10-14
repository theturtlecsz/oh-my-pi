-- OMP-603-s02: bind a mission to the intake revision and PASS assessment it
-- was admitted on. Rows are append-only history per mission; advancement of
-- the intake head holds every open row without rewriting its evidence.
CREATE TABLE omp_work.mission_intake_bindings (
  binding_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  mission_id uuid NOT NULL,
  run_id text NOT NULL CHECK (run_id <> ''),
  revision integer NOT NULL CHECK (revision >= 0),
  assessment_id text,
  state text NOT NULL CHECK (state IN ('prepared', 'admitted', 'held')),
  hold_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, mission_id)
    REFERENCES omp_work.project_missions(workspace_id, mission_id),
  FOREIGN KEY (workspace_id, run_id, revision)
    REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

SELECT omp_control.install_workspace_rls('omp_work.mission_intake_bindings'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.mission_intake_bindings FROM PUBLIC, omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE (state, hold_code) ON omp_work.mission_intake_bindings TO omp_work_app;
GRANT SELECT ON omp_work.mission_intake_bindings TO omp_work_readonly, omp_work_backup;
