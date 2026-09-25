-- jobs.omp.dev/v1 — separate migration set (NOT work.omp.dev/v1)
-- Must NOT live under operations/migrations/ (that glob feeds migration_set_sha256).

CREATE SCHEMA IF NOT EXISTS omp_jobs AUTHORIZATION omp_work_owner;

CREATE TABLE IF NOT EXISTS omp_jobs.jobs (
    job_id text PRIMARY KEY,
    idempotency_key text UNIQUE,
    status text NOT NULL
        CHECK (status IN (
            'backlog','admitted','in_flight','returned',
            'checking','sealed','failed','cancelled'
        )),
    source text NOT NULL
        CHECK (source IN ('flood_import','native')),
    provider_partition text,
    path_lease text,
    expected_max bigint,
    mission_id text,
    depends_on jsonb,
    packet_path text,
    blocker text,
    flood_origin jsonb,
    gate_evidence jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS omp_jobs.job_events (
    job_id text NOT NULL,
    seq bigint NOT NULL,
    actor text,
    reason text,
    kind text,
    at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, seq)
);

CREATE TABLE IF NOT EXISTS omp_jobs.leases (
    path_glob text PRIMARY KEY,
    job_id text NOT NULL,
    held_until timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS omp_jobs.reservations (
    reservation_id text PRIMARY KEY,
    job_id text,
    tokens bigint,
    provider_partition text,
    expires_at timestamptz,
    deadline_at timestamptz
);

CREATE TABLE IF NOT EXISTS omp_jobs.usage_events (
    conversation_id text NOT NULL,
    step_index bigint NOT NULL,
    source_file text NOT NULL,
    tokens bigint,
    role_derived text,
    job_id_derived text,
    provider_derived text,
    recorded_at timestamptz,
    payload jsonb,
    UNIQUE (conversation_id, step_index, source_file)
);

CREATE TABLE IF NOT EXISTS omp_jobs.work_items (
    id text PRIMARY KEY,
    intent text,
    definition text,
    tests text,
    pass_criteria text,
    terminal_artifact text,
    quality_saas text,
    needs_plan boolean,
    plan_status text,
    parent_id text,
    kind text,
    workstream text,
    status text
);

CREATE TABLE IF NOT EXISTS omp_jobs.admit_heartbeat (
    singleton int PRIMARY KEY CHECK (singleton = 1),
    frozen boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO omp_jobs.admit_heartbeat (singleton, frozen)
VALUES (1, false)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS omp_jobs.schema_migrations (
    ordinal int PRIMARY KEY,
    filename text NOT NULL,
    sha256 text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- Grants
GRANT USAGE ON SCHEMA omp_jobs TO omp_work_migrator, omp_work_readonly, omp_work_app;
GRANT ALL ON ALL TABLES IN SCHEMA omp_jobs TO omp_work_migrator, omp_work_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA omp_jobs TO omp_work_migrator, omp_work_app;
GRANT SELECT ON ALL TABLES IN SCHEMA omp_jobs TO omp_work_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT ALL ON TABLES TO omp_work_migrator, omp_work_app;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT SELECT ON TABLES TO omp_work_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT ALL ON SEQUENCES TO omp_work_migrator, omp_work_app;
