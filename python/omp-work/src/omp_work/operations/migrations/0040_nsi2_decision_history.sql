-- Typed decision/issue/requirement trace metadata, owned by the same revision.
CREATE TABLE omp_work.intake_decision_history (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  records jsonb NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision)
    REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);
SELECT omp_control.install_workspace_rls('omp_work.intake_decision_history'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.intake_decision_history FROM PUBLIC, omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.intake_decision_history TO omp_work_app;
GRANT SELECT ON omp_work.intake_decision_history TO omp_work_readonly, omp_work_backup;
CREATE TRIGGER immutable_intake_record BEFORE UPDATE OR DELETE ON omp_work.intake_decision_history
  FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
