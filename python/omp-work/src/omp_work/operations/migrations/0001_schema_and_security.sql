CREATE SCHEMA omp_control AUTHORIZATION omp_work_owner;
CREATE SCHEMA omp_work AUTHORIZATION omp_work_owner;
CREATE SCHEMA omp_evidence AUTHORIZATION omp_work_owner;
CREATE SCHEMA omp_audit AUTHORIZATION omp_work_owner;
CREATE SCHEMA omp_integration AUTHORIZATION omp_work_owner;
CREATE SCHEMA omp_fleet AUTHORIZATION omp_work_owner;
REVOKE ALL ON DATABASE omp_work FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE omp_work TO omp_work_migrator, omp_work_app, omp_work_importer, omp_work_readonly, omp_work_backup;
REVOKE ALL ON SCHEMA omp_control, omp_work, omp_evidence, omp_audit, omp_integration, omp_fleet FROM PUBLIC;
GRANT USAGE ON SCHEMA omp_control TO omp_work_migrator, omp_work_app, omp_work_importer, omp_work_readonly, omp_work_backup;
GRANT USAGE ON SCHEMA omp_work, omp_evidence, omp_audit, omp_integration, omp_fleet TO omp_work_app, omp_work_importer, omp_work_readonly, omp_work_backup;
CREATE TABLE omp_control.schema_migrations (
  ordinal integer PRIMARY KEY,
  filename text UNIQUE NOT NULL,
  sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  contract_version text NOT NULL,
  contract_sha256 text NOT NULL CHECK (contract_sha256 ~ '^[0-9a-f]{64}$'),
  postgres_major integer NOT NULL,
  applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE omp_control.schema_migrations OWNER TO omp_work_owner;
CREATE OR REPLACE FUNCTION omp_control.current_workspace_id() RETURNS uuid LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE value text;
BEGIN value := current_setting('omp.workspace_id', true); IF value IS NULL OR value = '' THEN RAISE EXCEPTION 'workspace claim required'; END IF; RETURN value::uuid; END $$;
CREATE OR REPLACE FUNCTION omp_control.current_actor_id() RETURNS uuid LANGUAGE plpgsql STABLE SET search_path = pg_catalog AS $$
DECLARE value text;
BEGIN value := current_setting('omp.actor_id', true); IF value IS NULL OR value = '' THEN RAISE EXCEPTION 'actor claim required'; END IF; RETURN value::uuid; END $$;
CREATE OR REPLACE FUNCTION omp_control.install_workspace_rls(target regclass, workspace_column name) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE target_schema text; nullable text;
BEGIN
 SELECT n.nspname INTO target_schema FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=target;
 IF target_schema NOT IN ('omp_work','omp_evidence','omp_audit','omp_integration','omp_fleet') THEN RAISE EXCEPTION 'invalid RLS schema'; END IF;
 SELECT is_nullable INTO nullable FROM information_schema.columns WHERE table_schema=target_schema AND table_name=(target::text::regclass)::text AND column_name=workspace_column;
 IF nullable IS DISTINCT FROM 'NO' THEN RAISE EXCEPTION 'workspace column must be NOT NULL UUID'; END IF;
 EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
 EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
 EXECUTE format('CREATE POLICY workspace_actor_policy ON %s USING (%I = omp_control.current_workspace_id() AND omp_control.current_actor_id() IS NOT NULL) WITH CHECK (%I = omp_control.current_workspace_id() AND omp_control.current_actor_id() IS NOT NULL)', target, workspace_column, workspace_column);
END $$;
