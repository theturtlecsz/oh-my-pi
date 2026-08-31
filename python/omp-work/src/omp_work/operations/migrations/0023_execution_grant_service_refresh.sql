-- OMP-199: permit service-only judge rotation during service refresh on active grants.

ALTER TABLE omp_work.execution_grants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items NO FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION omp_control.enforce_execution_grant_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'execution grants are immutable history'; END IF;
  IF OLD.grant_id IS DISTINCT FROM NEW.grant_id
     OR OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
     OR OLD.owner_id IS DISTINCT FROM NEW.owner_id
     OR OLD.repository IS DISTINCT FROM NEW.repository
     OR OLD.remote_ref IS DISTINCT FROM NEW.remote_ref
     OR OLD.mode IS DISTINCT FROM NEW.mode
     OR OLD.max_continuations IS DISTINCT FROM NEW.max_continuations
     OR OLD.max_close_attempts IS DISTINCT FROM NEW.max_close_attempts
     OR OLD.max_no_progress IS DISTINCT FROM NEW.max_no_progress
     OR OLD.authorization_hash IS DISTINCT FROM NEW.authorization_hash
     OR OLD.provenance IS DISTINCT FROM NEW.provenance
     OR OLD.focus_version_at_grant IS DISTINCT FROM NEW.focus_version_at_grant
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
     OR OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
    RAISE EXCEPTION 'execution grant identity and admission parameters are immutable';
  END IF;

  -- Controlled service-only judge rotation (OMP-199)
  IF OLD.judge_sha256 IS DISTINCT FROM NEW.judge_sha256 OR OLD.judge_manifest IS DISTINCT FROM NEW.judge_manifest THEN
    IF OLD.state <> 'active' OR NEW.state <> 'active'
       OR NEW.grant_version <> OLD.grant_version + 1
       OR (OLD.judge_manifest - ARRAY['service_fingerprint', 'service_code_fingerprint', 'service_migration_sha256'])
          IS DISTINCT FROM (NEW.judge_manifest - ARRAY['service_fingerprint', 'service_code_fingerprint', 'service_migration_sha256'])
       OR (NEW.judge_manifest->>'service_fingerprint') IS NULL
       OR (NEW.judge_manifest->>'service_fingerprint') !~ '^[0-9a-f]{64}$'
       OR (NEW.judge_manifest->>'service_code_fingerprint') IS NULL
       OR (NEW.judge_manifest->>'service_code_fingerprint') !~ '^[0-9a-f]{64}$'
       OR (NEW.judge_manifest->>'service_migration_sha256') IS NULL
       OR (NEW.judge_manifest->>'service_migration_sha256') !~ '^[0-9a-f]{64}$'
       OR NEW.judge_sha256 !~ '^[0-9a-f]{64}$' THEN
      RAISE EXCEPTION 'execution grant judge rotation is restricted to service-only manifest deltas on active grants';
    END IF;
  END IF;

  IF OLD.state IN ('stopped', 'completed', 'canceled') AND NEW.state <> OLD.state THEN
    RAISE EXCEPTION 'terminal execution grant is immutable';
  END IF;
  IF NEW.grant_version < OLD.grant_version THEN
    RAISE EXCEPTION 'grant_version must be monotonic';
  END IF;
  IF NEW.continuations_scheduled < OLD.continuations_scheduled THEN
    RAISE EXCEPTION 'continuations_scheduled must be monotonic';
  END IF;

  -- Write-once / state-consistent terminal timestamps
  IF OLD.completed_at IS NOT NULL AND (NEW.completed_at IS DISTINCT FROM OLD.completed_at OR NEW.state <> 'completed') THEN
    RAISE EXCEPTION 'completed_at is write-once and requires state completed';
  END IF;
  IF OLD.stopped_at IS NOT NULL AND (NEW.stopped_at IS DISTINCT FROM OLD.stopped_at OR NEW.state <> 'stopped') THEN
    RAISE EXCEPTION 'stopped_at is write-once and requires state stopped';
  END IF;
  IF OLD.canceled_at IS NOT NULL AND (NEW.canceled_at IS DISTINCT FROM OLD.canceled_at OR NEW.state <> 'canceled') THEN
    RAISE EXCEPTION 'canceled_at is write-once and requires state canceled';
  END IF;
  IF NEW.state = 'completed' AND (NEW.completed_at IS NULL OR (OLD.completed_at IS NOT NULL AND NEW.completed_at <> OLD.completed_at)) THEN
    RAISE EXCEPTION 'entering state completed sets completed_at once';
  END IF;
  IF NEW.state = 'stopped' AND (NEW.stopped_at IS NULL OR (OLD.stopped_at IS NOT NULL AND NEW.stopped_at <> OLD.stopped_at)) THEN
    RAISE EXCEPTION 'entering state stopped sets stopped_at once';
  END IF;
  IF NEW.state = 'canceled' AND (NEW.canceled_at IS NULL OR (OLD.canceled_at IS NOT NULL AND NEW.canceled_at <> OLD.canceled_at)) THEN
    RAISE EXCEPTION 'entering state canceled sets canceled_at once';
  END IF;

  RETURN NEW;
END $$;

ALTER TABLE omp_work.execution_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items FORCE ROW LEVEL SECURITY;
