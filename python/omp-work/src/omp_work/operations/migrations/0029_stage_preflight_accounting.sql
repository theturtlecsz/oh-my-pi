-- Native stage transport-preflight accounting is WorkService-owned state.
-- Session JSONL is telemetry; it cannot mint, record, or reconcile preflight usage.
CREATE TABLE omp_work.stage_preflights (
  preflight_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid,
  candidate_id uuid,
  attempt_id uuid,
  grant_id uuid,
  session_id text,
  role text NOT NULL CHECK (role IN ('plan','implement','frontier','audit')),
  tool_call_id text NOT NULL,
  task_sha256 text NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
  probe_sha256 text NOT NULL CHECK (probe_sha256 ~ '^[0-9a-f]{64}$'),
  transport_attempt_id uuid NOT NULL,
  ordinal integer NOT NULL CHECK (ordinal >= 0),
  requested_selector text NOT NULL,
  requested_provider text NOT NULL,
  requested_model text NOT NULL,
  requested_api text NOT NULL,
  requested_effort text NOT NULL,
  requested_wire_model text NOT NULL,
  is_fallback boolean NOT NULL DEFAULT false,
  outcome text NOT NULL CHECK (outcome IN ('selected','failed','cancelled')),
  stop_reason text,
  error text,
  requests integer CHECK (requests IS NULL OR requests >= 0),
  usage jsonb,
  provider_request_id text,
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((outcome = 'selected' AND error IS NULL) OR (outcome <> 'selected' AND error IS NOT NULL)),
  UNIQUE (workspace_id, transport_attempt_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (revision_id) REFERENCES omp_work.work_revisions(revision_id),
  FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  FOREIGN KEY (attempt_id) REFERENCES omp_work.close_attempts(attempt_id),
  FOREIGN KEY (grant_id) REFERENCES omp_work.execution_grants(grant_id)
);

CREATE INDEX stage_preflights_probe ON omp_work.stage_preflights(workspace_id, probe_sha256);
CREATE INDEX stage_preflights_work ON omp_work.stage_preflights(workspace_id, work_id, observed_at);

SELECT omp_control.install_workspace_rls('omp_work.stage_preflights'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.stage_preflights FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.stage_preflights TO omp_work_app;
GRANT SELECT ON omp_work.stage_preflights TO omp_work_readonly;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_work.stage_preflights FROM omp_work_app, omp_work_readonly, omp_work_importer;
