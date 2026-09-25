-- Additive S2 mirror metadata only. Does not activate jobs authority or adopt rows.
ALTER TABLE omp_jobs.jobs DROP CONSTRAINT jobs_status_check;
ALTER TABLE omp_jobs.jobs ADD CONSTRAINT jobs_status_check CHECK (status IN (
    'backlog','admitted','in_flight','returned','checking','sealed','failed','cancelled','empty_soft'
));

ALTER TABLE omp_jobs.jobs ADD COLUMN mirror_namespace text;
ALTER TABLE omp_jobs.jobs ADD COLUMN mirror_tombstoned_at timestamptz;
ALTER TABLE omp_jobs.jobs ADD CONSTRAINT jobs_mirror_source_check CHECK (
    mirror_namespace IS NULL OR source = 'flood_import'
);
ALTER TABLE omp_jobs.leases ADD COLUMN mirror_namespace text;
ALTER TABLE omp_jobs.leases ADD COLUMN mirror_tombstoned_at timestamptz;
ALTER TABLE omp_jobs.reservations ADD COLUMN mirror_namespace text;
ALTER TABLE omp_jobs.reservations ADD COLUMN mirror_tombstoned_at timestamptz;
