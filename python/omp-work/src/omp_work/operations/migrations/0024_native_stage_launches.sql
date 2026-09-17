-- Native tier launches are WorkService-owned state. Session JSONL is telemetry;
-- it cannot mint, hand off, settle, or reconcile a stage launch.
CREATE TABLE omp_work.stage_launches (
  launch_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid,
  candidate_id uuid,
  attempt_id uuid,
  grant_id uuid,
  role text NOT NULL CHECK (role IN ('plan','implement','frontier','audit')),
  request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
  tool_call_id text NOT NULL,
  task_sha256 text NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
  prepared_context_sha256 text NOT NULL CHECK (prepared_context_sha256 ~ '^[0-9a-f]{64}$'),
  requested_selector text NOT NULL,
  requested_provider text NOT NULL,
  requested_model text NOT NULL,
  requested_api text NOT NULL,
  requested_effort text NOT NULL,
  requested_wire_model text NOT NULL,
  resolved_selector text,
  resolved_provider text,
  resolved_model text,
  served_selector text,
  served_model text,
  is_fallback boolean NOT NULL DEFAULT false,
  fallback_reason text,
  status text NOT NULL CHECK (status IN ('reserved','handed_off','settled','cancelled','interrupted','superseded')),
  outcome_sha256 text CHECK (outcome_sha256 ~ '^[0-9a-f]{64}$'),
  outcome jsonb,
  reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  handed_off_at timestamptz,
  settled_at timestamptz,
  UNIQUE (workspace_id, request_sha256, tool_call_id),
  UNIQUE (workspace_id, launch_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (revision_id) REFERENCES omp_work.work_revisions(revision_id),
  FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  FOREIGN KEY (attempt_id) REFERENCES omp_work.close_attempts(attempt_id),
  FOREIGN KEY (grant_id) REFERENCES omp_work.execution_grants(grant_id)
);

SELECT omp_control.install_workspace_rls('omp_work.stage_launches'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.stage_launches FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_work.stage_launches TO omp_work_app;
GRANT SELECT ON omp_work.stage_launches TO omp_work_readonly;
REVOKE DELETE, TRUNCATE ON omp_work.stage_launches FROM omp_work_app, omp_work_readonly, omp_work_importer;
