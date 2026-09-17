-- Additive migration for context, learning, and corrections (S3-S6)

-- S3: proposals attribution, evidence binding, enrichment status, supporting lineage, invalidation
ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS attribution jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS evidence_binding text CHECK (evidence_binding IS NULL OR evidence_binding IN ('verified'));

ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS enrichment_status text CHECK (enrichment_status IS NULL OR enrichment_status IN ('applied', 'unavailable', 'degraded'));

ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS supporting_lineage jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS invalidated_at timestamptz;

ALTER TABLE omp_knowledge.procedure_proposals
    ADD COLUMN IF NOT EXISTS invalidation_id uuid;

-- S6 / S3: observations withdrawal tracking
ALTER TABLE omp_knowledge.observations
    ADD COLUMN IF NOT EXISTS withdrawn_by uuid;

-- S3: Policy registry for versioned acceptance rules
CREATE TABLE IF NOT EXISTS omp_knowledge.policy_registry (
    policy_id text NOT NULL,
    version integer NOT NULL,
    definition jsonb NOT NULL,
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY (policy_id, version)
);

INSERT INTO omp_knowledge.policy_registry (policy_id, version, definition, active)
VALUES (
    'native_acceptance',
    1,
    '{"rule": "authoritative_receipt", "issuer": "work-service/auditor-settle", "verdict": "PASS", "independent": true}'::jsonb,
    true
)
ON CONFLICT (policy_id, version) DO NOTHING;

-- S3: Applicability checks table
CREATE TABLE IF NOT EXISTS omp_knowledge.applicability_checks (
    check_id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL REFERENCES omp_knowledge.procedure_proposals(proposal_id),
    work_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    candidate_id uuid,
    policy_id text NOT NULL,
    policy_version integer NOT NULL,
    result text NOT NULL,
    native_readback_sha256 text,
    checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- S4: Context bundles immutable storage
CREATE TABLE IF NOT EXISTS omp_knowledge.context_bundles (
    bundle_id uuid PRIMARY KEY,
    bundle_sha256 text UNIQUE NOT NULL CHECK (bundle_sha256 ~ '^[0-9a-f]{64}$'),
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    work_id uuid NOT NULL,
    revision_id uuid NOT NULL,
    candidate_id uuid,
    stage text NOT NULL,
    snapshot_id text REFERENCES omp_knowledge.snapshots(snapshot_id),
    proposal_lineage jsonb NOT NULL DEFAULT '[]'::jsonb,
    receipt_lineage jsonb NOT NULL DEFAULT '[]'::jsonb,
    budget_spec jsonb NOT NULL,
    budget_actual jsonb NOT NULL,
    enrichment_status text NOT NULL,
    excluded_proposal_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    content jsonb NOT NULL,
    compiled_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION omp_knowledge.reject_bundle_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable context bundle';
END;
$$;

DROP TRIGGER IF EXISTS immutable_context_bundles ON omp_knowledge.context_bundles;
CREATE TRIGGER immutable_context_bundles
    BEFORE UPDATE OR DELETE ON omp_knowledge.context_bundles
    FOR EACH ROW EXECUTE FUNCTION omp_knowledge.reject_bundle_mutation();

-- S5: Knowledge uses additions for bundle binding, outcome receipt, and write-once outcome
ALTER TABLE omp_knowledge.knowledge_uses
    ADD COLUMN IF NOT EXISTS bundle_id uuid REFERENCES omp_knowledge.context_bundles(bundle_id);

ALTER TABLE omp_knowledge.knowledge_uses
    ADD COLUMN IF NOT EXISTS outcome_receipt_sha256 text CHECK (outcome_receipt_sha256 IS NULL OR outcome_receipt_sha256 ~ '^[0-9a-f]{64}$');

ALTER TABLE omp_knowledge.knowledge_uses
    ADD COLUMN IF NOT EXISTS outcome_lineage_key text;

ALTER TABLE omp_knowledge.knowledge_uses
    ADD COLUMN IF NOT EXISTS outcome_recorded_at timestamptz;

ALTER TABLE omp_knowledge.knowledge_uses
    ADD COLUMN IF NOT EXISTS independent boolean;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'knowledge_uses_proposal_task_bundle_unique'
    ) THEN
        ALTER TABLE omp_knowledge.knowledge_uses
            ADD CONSTRAINT knowledge_uses_proposal_task_bundle_unique
            UNIQUE NULLS NOT DISTINCT (proposal_id, task_work_id, task_revision_id, task_candidate_id, bundle_id);
    END IF;
END $$;

-- S6: Corrections table
CREATE TABLE IF NOT EXISTS omp_knowledge.corrections (
    correction_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    kind text NOT NULL CHECK (kind IN ('withdraw_evidence', 'supersede_proposal')),
    target jsonb NOT NULL,
    reason text NOT NULL,
    actor_id uuid NOT NULL,
    actor_kind text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    cleanup_operation_id uuid
);

-- Indices for rapid lookup and exclusion filtering
CREATE INDEX IF NOT EXISTS idx_procedure_proposals_invalidated_at
    ON omp_knowledge.procedure_proposals(invalidated_at);

CREATE INDEX IF NOT EXISTS idx_observations_withdrawn_by
    ON omp_knowledge.observations(withdrawn_by) WHERE withdrawn_by IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_context_bundles_lookup
    ON omp_knowledge.context_bundles(workspace_id, repository_id, work_id, stage);

CREATE INDEX IF NOT EXISTS idx_knowledge_uses_proposal_outcome
    ON omp_knowledge.knowledge_uses(proposal_id, outcome, outcome_lineage_key);
