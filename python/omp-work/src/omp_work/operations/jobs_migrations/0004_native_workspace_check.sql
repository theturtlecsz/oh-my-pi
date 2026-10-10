-- Native rows written before this check may have null workspace_id or kind.
-- NOT VALID leaves those rows in place. New and updated rows are checked.
ALTER TABLE omp_jobs.jobs
    ADD CONSTRAINT jobs_native_workspace_check
    CHECK (source <> 'native' OR (workspace_id IS NOT NULL AND kind IS NOT NULL))
    NOT VALID;
