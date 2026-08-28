-- OMP-180: enhanced NULL-safe admission column and counter monotonicity triggers.

ALTER TABLE omp_work.execution_grants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items NO FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION omp_control.enforce_execution_grant_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'execution grants are immutable history'; END IF;
  IF OLD.grant_id IS DISTINCT FROM NEW.grant_id
     OR OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
     OR OLD.owner_id IS DISTINCT FROM NEW.owner_id
     OR OLD.repository IS DISTINCT FROM NEW.repository
     OR OLD.mode IS DISTINCT FROM NEW.mode
     OR OLD.max_continuations IS DISTINCT FROM NEW.max_continuations
     OR OLD.max_close_attempts IS DISTINCT FROM NEW.max_close_attempts
     OR OLD.max_no_progress IS DISTINCT FROM NEW.max_no_progress
     OR OLD.authorization_hash IS DISTINCT FROM NEW.authorization_hash
     OR OLD.provenance IS DISTINCT FROM NEW.provenance
     OR OLD.judge_sha256 IS DISTINCT FROM NEW.judge_sha256
     OR OLD.judge_manifest IS DISTINCT FROM NEW.judge_manifest
     OR OLD.focus_version_at_grant IS DISTINCT FROM NEW.focus_version_at_grant
     OR OLD.created_at IS DISTINCT FROM NEW.created_at
     OR OLD.expires_at IS DISTINCT FROM NEW.expires_at THEN
    RAISE EXCEPTION 'execution grant identity and admission parameters are immutable';
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
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION omp_control.enforce_execution_grant_item_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'execution grant items are immutable history'; END IF;
  IF OLD.item_id IS DISTINCT FROM NEW.item_id
     OR OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
     OR OLD.grant_id IS DISTINCT FROM NEW.grant_id
     OR OLD.work_id IS DISTINCT FROM NEW.work_id
     OR OLD.position IS DISTINCT FROM NEW.position
     OR OLD.claimed_revision_id IS DISTINCT FROM NEW.claimed_revision_id
     OR OLD.project_id IS DISTINCT FROM NEW.project_id
     OR OLD.active_blocker_ids IS DISTINCT FROM NEW.active_blocker_ids
     OR OLD.initial_git_baseline IS DISTINCT FROM NEW.initial_git_baseline
     OR OLD.original_request IS DISTINCT FROM NEW.original_request
     OR OLD.original_request_sha256 IS DISTINCT FROM NEW.original_request_sha256 THEN
    RAISE EXCEPTION 'execution grant item identity and admission claims are immutable';
  END IF;
  IF OLD.phase IN ('completed', 'abandoned', 'skipped') AND NEW.phase <> OLD.phase THEN
    RAISE EXCEPTION 'terminal execution grant item is immutable';
  END IF;
  IF NEW.close_attempts_started < OLD.close_attempts_started THEN
    RAISE EXCEPTION 'close_attempts_started must be monotonic';
  END IF;
  RETURN NEW;
END $$;

ALTER TABLE omp_work.execution_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items FORCE ROW LEVEL SECURITY;
