-- Additive migration for native event capture (S1)

ALTER TABLE omp_knowledge.observations
    ADD COLUMN IF NOT EXISTS native_payload_sha256 text CHECK (native_payload_sha256 IS NULL OR native_payload_sha256 ~ '^[0-9a-f]{64}$');

ALTER TABLE omp_knowledge.observations
    ADD COLUMN IF NOT EXISTS relevance_tags text[] NOT NULL DEFAULT '{}';

ALTER TABLE omp_knowledge.observations
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT clock_timestamp();

ALTER TABLE omp_knowledge.native_event_checkpoints
    ADD COLUMN IF NOT EXISTS pending_gaps jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE omp_knowledge.native_event_checkpoints
    ADD COLUMN IF NOT EXISTS workspace_id uuid;

ALTER TABLE omp_knowledge.native_event_checkpoints
    ADD COLUMN IF NOT EXISTS pending_gap_metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_observations_workspace ON omp_knowledge.observations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_observations_native_payload_sha ON omp_knowledge.observations(native_payload_sha256) WHERE native_payload_sha256 IS NOT NULL;
