-- Nullable full_record JSONB columns on intake assessments and derivations.
ALTER TABLE omp_work.intake_assessments ADD COLUMN full_record jsonb;
ALTER TABLE omp_work.intake_derivations ADD COLUMN full_record jsonb;
