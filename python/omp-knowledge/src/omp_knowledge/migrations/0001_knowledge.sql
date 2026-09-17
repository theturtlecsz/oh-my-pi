-- Schema for omp-knowledge service
CREATE SCHEMA IF NOT EXISTS omp_knowledge;

-- Repository identities and aliases (Item 7)
CREATE TABLE IF NOT EXISTS omp_knowledge.repositories (
    repository_id uuid PRIMARY KEY,
    native_repository_id uuid,
    state text NOT NULL CHECK (state IN ('bound', 'unbound')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS omp_knowledge.repository_aliases (
    alias_id uuid PRIMARY KEY,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    kind text NOT NULL CHECK (kind IN ('canonical_remote', 'root_commit', 'worktree_common_dir')),
    value text NOT NULL,
    verified_by text NOT NULL,
    verified_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (kind, value)
);

-- Snapshots and staged publications (Item 5)
CREATE TABLE IF NOT EXISTS omp_knowledge.snapshots (
    snapshot_id text PRIMARY KEY,
    workspace_id uuid,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    ingest_operation_id uuid,
    manifest_sha256 text,
    base_commit text NOT NULL,
    tree_sha text NOT NULL,
    candidate_tree_sha text,
    manifest jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS omp_knowledge.snapshot_publications (
    publication_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    snapshot_id text NOT NULL REFERENCES omp_knowledge.snapshots(snapshot_id),
    status text NOT NULL CHECK (status IN ('staged', 'published', 'failed', 'retracted')),
    fact_count integer NOT NULL CHECK (fact_count >= 0),
    insight_count integer NOT NULL CHECK (insight_count >= 0),
    edge_count integer NOT NULL CHECK (edge_count >= 0),
    graph_sha256 text NOT NULL CHECK (graph_sha256 ~ '^[0-9a-f]{64}$'),
    receipt_sha256 text NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
    diagnostics text[] NOT NULL DEFAULT '{}',
    staged_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    published_at timestamptz,
    UNIQUE (workspace_id, repository_id, snapshot_id)
);

-- Content-addressed artifacts retained for exact rebuilds (Item 5, Item 9)
CREATE TABLE IF NOT EXISTS omp_knowledge.artifacts (
    content_sha256 text PRIMARY KEY CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    locator text NOT NULL,
    artifact_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Durable ingestion jobs and idempotence (Item 6)
CREATE TABLE IF NOT EXISTS omp_knowledge.ingestion_jobs (
    operation_id uuid PRIMARY KEY,
    canonical_operation_id uuid REFERENCES omp_knowledge.ingestion_jobs(operation_id),
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    snapshot_id text,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('queued', 'running', 'completed', 'failed', 'cancelled', 'no_lesson', 'partial', 'interrupted')),
    result_sha256 text CHECK (result_sha256 ~ '^[0-9a-f]{64}$'),
    response jsonb,
    error jsonb,
    attempt_count integer NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    owner_token text,
    diagnostics text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS omp_knowledge.job_checkpoints (
    checkpoint_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES omp_knowledge.ingestion_jobs(operation_id),
    step_name text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Native domain events checkpoint for exporter (Item 6)
CREATE TABLE IF NOT EXISTS omp_knowledge.native_event_checkpoints (
    consumer text PRIMARY KEY,
    after_sequence bigint NOT NULL DEFAULT 0,
    after_snapshot text,
    applied_event_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Raw observations (separately typed from proposals) (Item 1)
CREATE TABLE IF NOT EXISTS omp_knowledge.observations (
    observation_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    source jsonb NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL,
    payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Procedure proposals (derived knowledge only, never native authority) (Item 1, Item 8)
CREATE TABLE IF NOT EXISTS omp_knowledge.procedure_proposals (
    proposal_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    proposal_json jsonb NOT NULL,
    proposal_sha256 text NOT NULL CHECK (proposal_sha256 ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('proposal', 'historical_observation')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Knowledge uses and independent task outcomes (Item 1)
CREATE TABLE IF NOT EXISTS omp_knowledge.knowledge_uses (
    use_id uuid PRIMARY KEY,
    proposal_id uuid NOT NULL REFERENCES omp_knowledge.procedure_proposals(proposal_id),
    workspace_id uuid NOT NULL,
    repository_id uuid NOT NULL REFERENCES omp_knowledge.repositories(repository_id),
    task_work_id uuid NOT NULL,
    task_revision_id uuid NOT NULL,
    task_candidate_id uuid,
    supplied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    worker_used boolean NOT NULL DEFAULT false,
    outcome text NOT NULL CHECK (outcome IN ('success', 'failure', 'inconclusive', 'not_evaluated')),
    outcome_receipt_id uuid
);
