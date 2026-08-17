-- HOME-147 pre-cutover workflow amendment: candidate kinds and receipt metadata columns.
ALTER TABLE omp_work.candidates ADD COLUMN kind text NOT NULL DEFAULT 'planned';
ALTER TABLE omp_work.candidates ADD CONSTRAINT candidates_kind_check CHECK (kind IN ('planned', 'final'));
ALTER TABLE omp_work.candidates ADD CONSTRAINT candidates_final_commit_check CHECK (kind <> 'final' OR commit_sha IS NOT NULL);
CREATE INDEX candidates_work_revision_idx ON omp_work.candidates(work_id, revision_id);

ALTER TABLE omp_evidence.receipts ADD COLUMN issuer text;
ALTER TABLE omp_evidence.receipts ADD COLUMN artifact_sha256 text CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE omp_evidence.receipts ADD COLUMN candidate_sha256 text CHECK (candidate_sha256 IS NULL OR candidate_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE omp_evidence.receipts ADD COLUMN candidate_commit text;
ALTER TABLE omp_evidence.receipts ADD COLUMN verdict text CHECK (verdict IS NULL OR verdict IN ('PASS', 'NEEDS_FIX', 'BLOCKED'));
ALTER TABLE omp_evidence.receipts ADD COLUMN independent bool;
ALTER TABLE omp_evidence.receipts ADD COLUMN remote_ref text;
ALTER TABLE omp_evidence.receipts ADD COLUMN remote_commit text;
CREATE INDEX receipts_candidate_kind_idx ON omp_evidence.receipts(candidate_id, kind, issued_at, receipt_id);
