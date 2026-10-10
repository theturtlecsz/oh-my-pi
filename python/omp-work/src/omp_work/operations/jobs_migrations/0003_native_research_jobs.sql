-- Additive native research jobs on the shared omp_jobs substrate (R03, OMP-324).
-- The one substrate keeps working: a flood mirror row (source='flood_import',
-- kind IS NULL) inserts and updates untouched, while a native research row
-- (source='native') carries model|compute identity, capability routing,
-- leasing/fencing, settlement, cancellation, an idempotent operation ledger,
-- a durable outbox, worker registration, reservations, job events, and stable
-- usage identities. omp_jobs has no RLS; every query filters workspace_id.

ALTER TABLE omp_jobs.jobs
    ADD COLUMN workspace_id uuid,
    ADD COLUMN work_id uuid,
    ADD COLUMN trial_id uuid,
    ADD COLUMN parent_job_id text REFERENCES omp_jobs.jobs(job_id),
    ADD COLUMN kind text,
    ADD COLUMN required_capabilities jsonb,
    ADD COLUMN resources jsonb,
    ADD COLUMN lease_seconds integer,
    ADD COLUMN fence integer NOT NULL DEFAULT 0,
    ADD COLUMN attempt integer NOT NULL DEFAULT 0,
    ADD COLUMN worker_id text,
    ADD COLUMN lease_expires_at timestamptz,
    ADD COLUMN settlement jsonb,
    ADD COLUMN settled_at timestamptz,
    ADD COLUMN cancel_reason text,
    ADD COLUMN cancelled_at timestamptz,
    ADD CONSTRAINT jobs_native_kind_check CHECK (kind IS NULL OR kind IN ('model','compute')),
    ADD CONSTRAINT jobs_native_kind_source_check CHECK (kind IS NULL OR source = 'native'),
    ADD CONSTRAINT jobs_required_capabilities_check CHECK (
        required_capabilities IS NULL
        OR (jsonb_typeof(required_capabilities) = 'array' AND octet_length(required_capabilities::text) <= 8192)
    ),
    ADD CONSTRAINT jobs_resources_check CHECK (
        resources IS NULL
        OR (jsonb_typeof(resources) = 'object' AND octet_length(resources::text) <= 8192)
    ),
    ADD CONSTRAINT jobs_lease_seconds_check CHECK (lease_seconds IS NULL OR lease_seconds BETWEEN 1 AND 3600),
    ADD CONSTRAINT jobs_settlement_check CHECK (settlement IS NULL OR octet_length(settlement::text) <= 65536),
    ADD CONSTRAINT jobs_cancel_reason_check CHECK (cancel_reason IS NULL OR octet_length(cancel_reason) <= 4096);

CREATE TABLE omp_jobs.workers (
    worker_id text PRIMARY KEY,
    workspace_id uuid NOT NULL,
    component_sha256 text NOT NULL CHECK (component_sha256 ~ '^[0-9a-f]{64}$'),
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
        jsonb_typeof(capabilities) = 'array' AND octet_length(capabilities::text) <= 8192
    ),
    capacity integer NOT NULL CHECK (capacity >= 0),
    state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','draining')),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE omp_jobs.operations (
    operation_id text PRIMARY KEY,
    workspace_id uuid NOT NULL,
    kind text NOT NULL,
    request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{64}$'),
    result jsonb NOT NULL CHECK (octet_length(result::text) <= 65536),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE omp_jobs.outbox (
    event_id text PRIMARY KEY,
    workspace_id uuid NOT NULL,
    operation_id text NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL CHECK (octet_length(payload::text) <= 65536),
    state text NOT NULL DEFAULT 'open'
        CHECK (state IN ('open','committed','acknowledged','closed','failed')),
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    ack_token text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT outbox_ack_token_check CHECK ((state IN ('acknowledged','closed')) = (ack_token IS NOT NULL))
);

ALTER TABLE omp_jobs.reservations
    ADD COLUMN workspace_id uuid,
    ADD COLUMN worker_id text,
    ADD COLUMN fence integer NOT NULL DEFAULT 0,
    ADD COLUMN resources jsonb,
    ADD COLUMN released_at timestamptz,
    ADD COLUMN release_reason text,
    ADD CONSTRAINT reservations_resources_check CHECK (
        resources IS NULL
        OR (jsonb_typeof(resources) = 'object' AND octet_length(resources::text) <= 8192)
    ),
    ADD CONSTRAINT reservations_release_pair_check CHECK ((released_at IS NULL) = (release_reason IS NULL));

ALTER TABLE omp_jobs.job_events
    ADD COLUMN operation_id text,
    ADD COLUMN payload jsonb,
    ADD CONSTRAINT job_events_payload_check CHECK (payload IS NULL OR octet_length(payload::text) <= 65536);

ALTER TABLE omp_jobs.usage_events
    ADD COLUMN usage_id text,
    ADD COLUMN workspace_id uuid,
    ADD COLUMN work_id uuid,
    ADD COLUMN request_id text,
    ADD COLUMN model text,
    ADD COLUMN input_tokens bigint,
    ADD COLUMN output_tokens bigint,
    ADD COLUMN cache_tokens bigint,
    ADD COLUMN measurement text,
    ADD COLUMN price_usd numeric(20,10),
    ADD COLUMN price_version text,
    ADD CONSTRAINT usage_events_usage_id_key UNIQUE (usage_id);

-- Drain is permanent: no write path may move a worker back to active.
CREATE OR REPLACE FUNCTION omp_control.reject_worker_reactivation() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
    IF OLD.state = 'draining' AND NEW.state IS DISTINCT FROM 'draining' THEN
        RAISE EXCEPTION 'drained worker cannot be reactivated';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER workers_no_reactivation BEFORE UPDATE ON omp_jobs.workers
    FOR EACH ROW EXECUTE FUNCTION omp_control.reject_worker_reactivation();

-- New tables: app reads and writes native job state. The tables 0001 created
-- already carry ALL to app through its grants/default privileges, so only the
-- tables added here need an explicit grant.
GRANT SELECT, INSERT, UPDATE ON omp_jobs.workers, omp_jobs.operations, omp_jobs.outbox TO omp_work_app;
