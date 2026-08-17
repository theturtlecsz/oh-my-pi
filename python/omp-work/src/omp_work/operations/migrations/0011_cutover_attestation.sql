-- HOME-148 remediation: first-mutation attestation gate + sealed completeness.
-- The activation nominates the exact request allowed to land the first WorkService
-- mutation (expected_first_request_id); the store rejects every other non-activation
-- command until that request applies, then records the winning request atomically.
-- The attestation itself is a dedicated, non-candidate-mutating receipt so the
-- nominated request can never be rejected by domain candidate rules.
ALTER TABLE omp_control.workspace_authority
    ADD COLUMN expected_first_request_id uuid,
    ADD COLUMN first_work_mutation_request_id uuid;

CREATE TABLE omp_control.cutover_plan_attestations (
    workspace_id uuid NOT NULL REFERENCES omp_control.workspace_authority (workspace_id),
    epoch_id uuid NOT NULL REFERENCES omp_control.cutover_epochs (epoch_id),
    work_id uuid NOT NULL,
    request_id uuid NOT NULL,
    plan_name text NOT NULL,
    plan_sha256 text NOT NULL CHECK (plan_sha256 ~ '^[0-9a-f]{64}$'),
    plan_artifact text NOT NULL,
    issuer text NOT NULL,
    attested_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (workspace_id, epoch_id)
);

CREATE OR REPLACE FUNCTION omp_control.guard_workspace_authority() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
     OR NEW.epoch_id IS DISTINCT FROM OLD.epoch_id
     OR NEW.activated_at IS DISTINCT FROM OLD.activated_at
     OR NEW.expected_first_request_id IS DISTINCT FROM OLD.expected_first_request_id THEN
    RAISE EXCEPTION 'workspace authority is immutable';
  END IF;
  IF NEW.first_work_mutation_at IS NULL OR (OLD.first_work_mutation_at IS NOT NULL AND NEW.first_work_mutation_at IS DISTINCT FROM OLD.first_work_mutation_at) THEN
    RAISE EXCEPTION 'first work mutation timestamp is set exactly once';
  END IF;
  IF (OLD.first_work_mutation_request_id IS NOT NULL AND NEW.first_work_mutation_request_id IS DISTINCT FROM OLD.first_work_mutation_request_id)
     OR (NEW.first_work_mutation_request_id IS NULL AND NEW.first_work_mutation_at IS NOT NULL) THEN
    RAISE EXCEPTION 'first work mutation request is set exactly once, with the timestamp';
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION omp_control.guard_cutover_epoch() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
     OR NEW.candidate_manifest IS DISTINCT FROM OLD.candidate_manifest
     OR NEW.candidate_manifest_sha256 IS DISTINCT FROM OLD.candidate_manifest_sha256
     OR NEW.linear_credential_sha256 IS DISTINCT FROM OLD.linear_credential_sha256
     OR NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
    RAISE EXCEPTION 'cutover epoch candidate is immutable';
  END IF;
  IF NOT (OLD.state = 'active' AND NEW.state IN ('sealed','rolled_back')) THEN
    RAISE EXCEPTION 'invalid cutover epoch transition: % to %', OLD.state, NEW.state;
  END IF;
  IF NEW.state = 'sealed' AND (NEW.final_report_sha256 IS NULL OR NEW.revoked_at IS NULL OR NEW.recovery_path IS NULL) THEN
    RAISE EXCEPTION 'sealed epoch requires final report, revocation timestamp, and recovery path';
  END IF;
  IF NEW.state = 'rolled_back' AND (NEW.final_report_sha256 IS NOT NULL OR NEW.revoked_at IS NOT NULL) THEN
    RAISE EXCEPTION 'rolled back epoch carries no final report or revocation';
  END IF;
  RETURN NEW;
END $$;

SELECT omp_control.install_workspace_rls('omp_control.cutover_plan_attestations', 'workspace_id');
GRANT SELECT, INSERT ON omp_control.cutover_plan_attestations TO omp_work_app;
GRANT UPDATE (first_work_mutation_request_id) ON omp_control.workspace_authority TO omp_work_app;
GRANT SELECT ON omp_control.cutover_plan_attestations TO omp_work_readonly, omp_work_backup;
