-- The migration owner validates all workspaces without a workspace claim.
ALTER TABLE omp_work.project_history NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.project_missions NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.project_history
  ADD CONSTRAINT project_history_mission_fk
  FOREIGN KEY (workspace_id, mission_id)
  REFERENCES omp_work.project_missions(workspace_id, mission_id);

ALTER TABLE omp_work.project_history FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.project_missions FORCE ROW LEVEL SECURITY;
