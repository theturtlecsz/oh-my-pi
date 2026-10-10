-- OMP-564: index project mission and history reads by workspace and project.
-- 0030 was already applied, so these indexes are a new migration. They inherit
-- the tables' RLS and privileges.

CREATE INDEX project_missions_workspace_project_idx
  ON omp_work.project_missions (workspace_id, project_id);

CREATE INDEX project_history_workspace_project_idx
  ON omp_work.project_history (workspace_id, project_id);
