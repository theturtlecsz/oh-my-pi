-- OMP-283: external_delivery receipts may omit a candidate binding.

ALTER TABLE omp_evidence.receipts NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_evidence.receipts ALTER COLUMN candidate_id DROP NOT NULL;
ALTER TABLE omp_evidence.receipts ADD CONSTRAINT receipts_candidate_required CHECK (candidate_id IS NOT NULL OR kind = 'external_delivery');

ALTER TABLE omp_evidence.receipts FORCE ROW LEVEL SECURITY;
