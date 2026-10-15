CREATE TABLE omp_work.mission_launch_bindings (
  launch_binding_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  mission_id uuid NOT NULL,
  intake_binding_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  assessment_id text NOT NULL,
  plan_receipt_id text NOT NULL,
  plan_sha256 text NOT NULL,
  item_id text NOT NULL,
  work_id uuid NOT NULL,
  candidate_id text NOT NULL,
  state text NOT NULL CHECK (state IN ('bound', 'held')),
  hold_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, mission_id)
    REFERENCES omp_work.project_missions(workspace_id, mission_id),
  FOREIGN KEY (intake_binding_id)
    REFERENCES omp_work.mission_intake_bindings(binding_id)
);

SELECT omp_control.install_workspace_rls('omp_work.mission_launch_bindings'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.mission_launch_bindings FROM PUBLIC, omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE (state, hold_code) ON omp_work.mission_launch_bindings TO omp_work_app;
GRANT SELECT ON omp_work.mission_launch_bindings TO omp_work_readonly, omp_work_backup;
