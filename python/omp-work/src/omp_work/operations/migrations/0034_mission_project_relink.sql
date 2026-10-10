-- OMP-561: link_mission re-attaches an existing mission to its new project.
-- The ON CONFLICT update now sets project_id, so omp_work_app needs UPDATE on
-- that column in addition to the fields granted by migration 0030.

GRANT UPDATE (project_id) ON omp_work.project_missions TO omp_work_app;
