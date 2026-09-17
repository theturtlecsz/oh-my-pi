-- Native stage transport-preflight intents are WorkService-owned state.
-- Host must durably begin an intent before any credentialed probe leaves host.
CREATE TABLE omp_work.stage_preflight_intents (
  intent_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid,
  candidate_id uuid,
  attempt_id uuid,
  grant_id uuid,
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
  logical_sha256 text NOT NULL CHECK (logical_sha256 ~ '^[0-9a-f]{64}$'),
  group_sha256 text NOT NULL CHECK (group_sha256 ~ '^[0-9a-f]{64}$'),
  host_owner_id uuid,
  status text NOT NULL CHECK (status IN ('begun','settled')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  settled_at timestamptz,
  CHECK ((status = 'begun' AND settled_at IS NULL AND host_owner_id IS NOT NULL) OR (status = 'settled' AND settled_at IS NOT NULL)),
  UNIQUE (workspace_id, transport_attempt_id),
  UNIQUE (workspace_id, logical_sha256),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (revision_id) REFERENCES omp_work.work_revisions(revision_id),
  FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  FOREIGN KEY (attempt_id) REFERENCES omp_work.close_attempts(attempt_id),
  FOREIGN KEY (grant_id) REFERENCES omp_work.execution_grants(grant_id)
);

CREATE INDEX stage_preflight_intents_group ON omp_work.stage_preflight_intents(workspace_id, group_sha256);
CREATE INDEX stage_preflight_intents_work ON omp_work.stage_preflight_intents(workspace_id, work_id, created_at);

-- Backfill legacy settled intents for any preexisting preflight records.
-- Derives deterministic sentinels from transport UUID. Preserves original records.
ALTER TABLE omp_work.stage_preflights DISABLE ROW LEVEL SECURITY;

INSERT INTO omp_work.stage_preflight_intents (
  intent_id,
  workspace_id,
  work_id,
  revision_id,
  candidate_id,
  attempt_id,
  grant_id,
  role,
  tool_call_id,
  task_sha256,
  probe_sha256,
  transport_attempt_id,
  ordinal,
  requested_selector,
  requested_provider,
  requested_model,
  requested_api,
  requested_effort,
  requested_wire_model,
  is_fallback,
  logical_sha256,
  group_sha256,
  host_owner_id,
  status,
  created_at,
  settled_at
)
SELECT
  gen_random_uuid(),
  workspace_id,
  work_id,
  revision_id,
  candidate_id,
  attempt_id,
  grant_id,
  role,
  tool_call_id,
  task_sha256,
  probe_sha256,
  transport_attempt_id,
  ordinal,
  requested_selector,
  requested_provider,
  requested_model,
  requested_api,
  requested_effort,
  requested_wire_model,
  is_fallback,
  encode(sha256(('legacy:logical:' || transport_attempt_id::text)::bytea), 'hex'),
  encode(sha256(('legacy:group:' || transport_attempt_id::text)::bytea), 'hex'),
  NULL,
  'settled',
  observed_at,
  observed_at
FROM omp_work.stage_preflights;

-- Structural FK to enforce that every stage_preflight evidence row must have a parent intent row.
ALTER TABLE omp_work.stage_preflights
  ADD CONSTRAINT stage_preflights_intent_fk
  FOREIGN KEY (workspace_id, transport_attempt_id)
  REFERENCES omp_work.stage_preflight_intents (workspace_id, transport_attempt_id);

ALTER TABLE omp_work.stage_preflights ENABLE ROW LEVEL SECURITY;
ALTER TABLE omp_work.stage_preflights FORCE ROW LEVEL SECURITY;

SELECT omp_control.install_workspace_rls('omp_work.stage_preflight_intents'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.stage_preflight_intents FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.stage_preflight_intents TO omp_work_app;
GRANT UPDATE (status, settled_at) ON omp_work.stage_preflight_intents TO omp_work_app;
GRANT SELECT ON omp_work.stage_preflight_intents TO omp_work_readonly;
REVOKE DELETE, TRUNCATE ON omp_work.stage_preflight_intents FROM omp_work_app, omp_work_readonly, omp_work_importer;
