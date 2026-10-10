ALTER TABLE omp_jobs.jobs
    ADD CONSTRAINT jobs_native_workspace_check
    CHECK (source <> 'native' OR (workspace_id IS NOT NULL AND kind IS NOT NULL));
