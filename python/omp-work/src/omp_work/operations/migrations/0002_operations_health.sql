CREATE TABLE omp_control.runtime_compatibility (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  contract_version text NOT NULL,
  contract_sha256 text NOT NULL,
  migration_set_sha256 text NOT NULL,
  postgres_major integer NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE omp_control.runtime_compatibility OWNER TO omp_work_owner;
CREATE TABLE omp_control.readiness_probe (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  checked_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE omp_control.readiness_probe OWNER TO omp_work_owner;
CREATE TABLE omp_control.operations_evidence (
  id uuid PRIMARY KEY DEFAULT uuidv7(),
  kind text NOT NULL,
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  source_timeline text,
  source_lsn pg_lsn,
  contract_sha256 text NOT NULL,
  migration_set_sha256 text NOT NULL,
  backup_id uuid,
  object_prefix text,
  encrypted_manifest_sha256 text,
  byte_count bigint,
  outcome text NOT NULL,
  duration_seconds numeric,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE omp_control.operations_evidence OWNER TO omp_work_owner;
REVOKE ALL ON ALL TABLES IN SCHEMA omp_control FROM PUBLIC;
GRANT SELECT ON omp_control.runtime_compatibility, omp_control.schema_migrations, omp_control.operations_evidence TO omp_work_app;
GRANT INSERT, UPDATE ON omp_control.readiness_probe TO omp_work_app;
GRANT SELECT ON omp_control.runtime_compatibility, omp_control.schema_migrations, omp_control.operations_evidence TO omp_work_backup;
GRANT INSERT ON omp_control.operations_evidence TO omp_work_backup;
