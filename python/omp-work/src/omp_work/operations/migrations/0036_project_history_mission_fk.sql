-- OMP-562: project_history.mission_id names a mission in the same workspace.
-- A null mission_id is not a reference. An orphan reference fails this migration.
-- Validation runs with FORCE RLS off: ops migrate holds no workspace claim.
ALTER TABLE omp_work.project_history NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.project_missions NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.project_history
  ADD CONSTRAINT project_history_mission_fk
  FOREIGN KEY (workspace_id, mission_id)
  REFERENCES omp_work.project_missions(workspace_id, mission_id);

ALTER TABLE omp_work.project_history FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.project_missions FORCE ROW LEVEL SECURITY;
