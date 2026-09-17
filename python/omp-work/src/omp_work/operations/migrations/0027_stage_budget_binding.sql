-- Atomic stage-budget binding: workspace-safe FK to stage_launches and unique active reservation per bound launch.
ALTER TABLE omp_work.budget_reservations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.stage_launches NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.budget_reservations ADD COLUMN launch_id uuid;
ALTER TABLE omp_work.budget_reservations
  ADD CONSTRAINT budget_reservations_launch_fk
  FOREIGN KEY (workspace_id, launch_id) REFERENCES omp_work.stage_launches(workspace_id, launch_id);
CREATE UNIQUE INDEX budget_reservations_active_launch
  ON omp_work.budget_reservations(workspace_id, launch_id)
  WHERE launch_id IS NOT NULL AND state IN ('reserved_unsent','potentially_sent');

ALTER TABLE omp_work.stage_launches FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.budget_reservations FORCE ROW LEVEL SECURITY;
