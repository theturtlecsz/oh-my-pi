-- Nullable support_receipt JSONB column on intake_claim_evidence with no default or backfill.
ALTER TABLE omp_work.intake_claim_evidence ADD COLUMN support_receipt jsonb;
