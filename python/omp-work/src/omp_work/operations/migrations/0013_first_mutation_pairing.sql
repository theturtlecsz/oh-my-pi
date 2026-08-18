-- HOME-148 audit remediation: timestamp and request identity are one atomic stamp.
-- A table constraint covers INSERT as well as the existing immutability UPDATE trigger.
ALTER TABLE omp_control.workspace_authority
    ADD CONSTRAINT workspace_authority_first_mutation_pairing
    CHECK ((first_work_mutation_at IS NULL) = (first_work_mutation_request_id IS NULL));
