DO $$
DECLARE role_name text;
BEGIN
  FOREACH role_name IN ARRAY ARRAY['omp_work_owner','omp_work_migrator','omp_work_app','omp_work_importer','omp_work_readonly','omp_work_backup','omp_work_lookup'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
      EXECUTE format('CREATE ROLE %I', role_name);
    END IF;
  END LOOP;
END $$;
ALTER ROLE omp_work_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE omp_work_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE omp_work_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE omp_work_importer LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE omp_work_readonly LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER ROLE omp_work_backup LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
ALTER ROLE omp_work_lookup NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
GRANT omp_work_owner TO omp_work_migrator;
GRANT omp_work_lookup TO omp_work_migrator;
GRANT omp_work_lookup TO omp_work_owner;
