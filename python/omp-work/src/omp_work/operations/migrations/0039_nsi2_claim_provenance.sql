-- Preserve supplied provenance while retaining legacy NULL reconstruction.
ALTER TABLE omp_work.intake_artifacts
  ADD COLUMN source_revision text NULL,
  ADD COLUMN source_actor jsonb NULL;

ALTER TABLE omp_work.intake_claims
  ADD COLUMN asserted_kind text NULL
    CHECK (asserted_kind IN ('owner', 'model', 'tool', 'rule_engine', 'service')),
  ADD COLUMN admitted_kind text NULL
    CHECK (admitted_kind IN ('owner', 'rule_engine'));

ALTER TABLE omp_work.intake_claim_evidence
  ADD COLUMN relation text NOT NULL DEFAULT 'neither'
    CHECK (relation IN ('supports', 'contradicts', 'neither'));
