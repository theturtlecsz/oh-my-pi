-- HOME-148: PostgreSQL authority becomes an enforced atomic state.
-- workspace_authority: presence means Work is authoritative; absence means Linear.
-- cutover_epochs: immutable candidate bytes, one live epoch per workspace,
-- transitions restricted to active -> sealed | rolled_back, and epochs can
-- never be deleted by the app role (no DELETE grant).
CREATE TABLE omp_control.workspace_authority (
  workspace_id uuid PRIMARY KEY,
  epoch_id uuid NOT NULL UNIQUE,
  activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  first_work_mutation_at timestamptz
);
ALTER TABLE omp_control.workspace_authority OWNER TO omp_work_owner;
CREATE TABLE omp_control.cutover_epochs (
  epoch_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  state text NOT NULL DEFAULT 'active' CHECK (state IN ('active','sealed','rolled_back')),
  candidate_manifest jsonb NOT NULL,
  candidate_manifest_sha256 text NOT NULL CHECK (candidate_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  final_report_sha256 text CHECK (final_report_sha256 IS NULL OR final_report_sha256 ~ '^[0-9a-f]{64}$'),
  linear_credential_sha256 text CHECK (linear_credential_sha256 IS NULL OR linear_credential_sha256 ~ '^[0-9a-f]{64}$'),
  revoked_at timestamptz,
  recovery_path text CHECK (recovery_path IS NULL OR recovery_path IN ('pre_write','post_write')),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE omp_control.cutover_epochs OWNER TO omp_work_owner;
CREATE UNIQUE INDEX cutover_epochs_one_live_per_workspace ON omp_control.cutover_epochs (workspace_id) WHERE state IN ('active','sealed');
CREATE OR REPLACE FUNCTION omp_control.guard_cutover_epoch() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
     OR NEW.candidate_manifest IS DISTINCT FROM OLD.candidate_manifest
     OR NEW.candidate_manifest_sha256 IS DISTINCT FROM OLD.candidate_manifest_sha256
     OR NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
    RAISE EXCEPTION 'cutover epoch candidate is immutable';
  END IF;
  IF NOT (OLD.state = 'active' AND NEW.state IN ('sealed','rolled_back')) THEN
    RAISE EXCEPTION 'invalid cutover epoch transition: % to %', OLD.state, NEW.state;
  END IF;
  IF NEW.state = 'rolled_back' AND (NEW.final_report_sha256 IS NOT NULL OR NEW.revoked_at IS NOT NULL) THEN
    RAISE EXCEPTION 'rolled back epoch carries no final report or revocation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER cutover_epochs_guard BEFORE UPDATE ON omp_control.cutover_epochs FOR EACH ROW EXECUTE FUNCTION omp_control.guard_cutover_epoch();
CREATE OR REPLACE FUNCTION omp_control.guard_workspace_authority() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
     OR NEW.epoch_id IS DISTINCT FROM OLD.epoch_id
     OR NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
    RAISE EXCEPTION 'workspace authority is immutable';
  END IF;
  IF NEW.first_work_mutation_at IS NULL OR (OLD.first_work_mutation_at IS NOT NULL AND NEW.first_work_mutation_at IS DISTINCT FROM OLD.first_work_mutation_at) THEN
    RAISE EXCEPTION 'first work mutation timestamp is set exactly once';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER workspace_authority_guard BEFORE UPDATE ON omp_control.workspace_authority FOR EACH ROW EXECUTE FUNCTION omp_control.guard_workspace_authority();
ALTER TABLE omp_control.operations_evidence ADD COLUMN receipt_sha256 text CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$');
ALTER TABLE omp_control.operations_evidence ALTER COLUMN receipt_sha256 SET NOT NULL;
-- The 0003 install_workspace_rls already permits omp_control; apply the same
-- forced workspace-claim policy to the authority tables.
SELECT omp_control.install_workspace_rls('omp_control.workspace_authority', 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_control.cutover_epochs', 'workspace_id');
GRANT SELECT, INSERT ON omp_control.cutover_epochs TO omp_work_app;
GRANT UPDATE (state, final_report_sha256, linear_credential_sha256, revoked_at, recovery_path) ON omp_control.cutover_epochs TO omp_work_app;
GRANT SELECT, INSERT ON omp_control.workspace_authority TO omp_work_app;
GRANT UPDATE (first_work_mutation_at) ON omp_control.workspace_authority TO omp_work_app;
GRANT SELECT ON omp_control.cutover_epochs, omp_control.workspace_authority TO omp_work_readonly, omp_work_backup;
GRANT SELECT ON omp_integration.import_batches, omp_integration.raw_exports, omp_integration.migration_anomalies TO omp_work_app;
