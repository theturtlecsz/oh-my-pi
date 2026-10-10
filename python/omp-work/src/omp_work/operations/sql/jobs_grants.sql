-- E0678: the omp_jobs grants in jobs_migrations/0001_omp_jobs_schema.sql and
-- 0003_native_research_jobs.sql did not cover omp_work_backup, and neither
-- covered omp_work_readonly on the tables a migration the migrator ran created.
-- A jobs file run by omp_work_migrator (before OMP-453 moved the DDL under
-- SET LOCAL ROLE omp_work_owner) owns the tables it creates, so 0001's owner
-- default privileges never applied to workers, operations, and outbox. This
-- file runs on every migrate (idempotent GRANT / ALTER DEFAULT PRIVILEGES),
-- after the jobs migrations, so both read-only roles can read every existing
-- table and every table a later owner-run migration adds.
GRANT USAGE ON SCHEMA omp_jobs TO omp_work_readonly, omp_work_backup;
GRANT SELECT ON ALL TABLES IN SCHEMA omp_jobs TO omp_work_readonly, omp_work_backup;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA omp_jobs TO omp_work_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT SELECT ON TABLES TO omp_work_readonly, omp_work_backup;
ALTER DEFAULT PRIVILEGES FOR ROLE omp_work_owner IN SCHEMA omp_jobs
    GRANT USAGE, SELECT ON SEQUENCES TO omp_work_backup;
