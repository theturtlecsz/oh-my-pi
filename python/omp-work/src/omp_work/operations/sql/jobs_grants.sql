-- E0678: omp_work_backup was omitted from the omp_jobs grants that
-- jobs_migrations/0001_omp_jobs_schema.sql issues, so pg_dump as the backup
-- role could not read the jobs substrate on any database where it exists.
-- Runs on every migrate (idempotent GRANT / ALTER DEFAULT PRIVILEGES), after
-- the jobs migrations have created the schema, so new tables and sequences
-- stay readable without a new pinned jobs migration file.
GRANT USAGE ON SCHEMA omp_jobs TO omp_work_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA omp_jobs TO omp_work_backup;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA omp_jobs TO omp_work_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT SELECT ON TABLES TO omp_work_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT USAGE, SELECT ON SEQUENCES TO omp_work_backup;
