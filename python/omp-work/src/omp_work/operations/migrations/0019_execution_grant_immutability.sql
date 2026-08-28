-- OMP-180: execution grant and item immutability triggers.

ALTER TABLE omp_work.execution_grants NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items NO FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION omp_control.enforce_execution_grant_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'execution grants are immutable history'; END IF;
  IF OLD.grant_id <> NEW.grant_id OR OLD.workspace_id <> NEW.workspace_id OR OLD.owner_id <> NEW.owner_id
     OR OLD.repository <> NEW.repository OR OLD.mode <> NEW.mode
     OR OLD.authorization_hash <> NEW.authorization_hash OR OLD.provenance <> NEW.provenance
     OR OLD.judge_sha256 <> NEW.judge_sha256 OR OLD.judge_manifest <> NEW.judge_manifest
     OR OLD.created_at <> NEW.created_at THEN
    RAISE EXCEPTION 'execution grant identity is immutable';
  END IF;
  IF OLD.state IN ('stopped', 'completed', 'canceled') AND NEW.state <> OLD.state THEN
    RAISE EXCEPTION 'terminal execution grant is immutable';
  END IF;
  IF NEW.grant_version < OLD.grant_version THEN
    RAISE EXCEPTION 'grant_version must be monotonic';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS enforce_execution_grant_transition_trigger ON omp_work.execution_grants;
CREATE TRIGGER enforce_execution_grant_transition_trigger
  BEFORE UPDATE OR DELETE ON omp_work.execution_grants
  FOR EACH ROW EXECUTE FUNCTION omp_control.enforce_execution_grant_transition();

CREATE OR REPLACE FUNCTION omp_control.enforce_execution_grant_item_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'execution grant items are immutable history'; END IF;
  IF OLD.item_id <> NEW.item_id OR OLD.workspace_id <> NEW.workspace_id OR OLD.grant_id <> NEW.grant_id
     OR OLD.work_id <> NEW.work_id OR OLD.position <> NEW.position
     OR OLD.claimed_revision_id <> NEW.claimed_revision_id
     OR OLD.original_request <> NEW.original_request OR OLD.original_request_sha256 <> NEW.original_request_sha256 THEN
    RAISE EXCEPTION 'execution grant item identity is immutable';
  END IF;
  IF OLD.phase IN ('completed', 'abandoned', 'skipped') AND NEW.phase <> OLD.phase THEN
    RAISE EXCEPTION 'terminal execution grant item is immutable';
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS enforce_execution_grant_item_transition_trigger ON omp_work.execution_grant_items;
CREATE TRIGGER enforce_execution_grant_item_transition_trigger
  BEFORE UPDATE OR DELETE ON omp_work.execution_grant_items
  FOR EACH ROW EXECUTE FUNCTION omp_control.enforce_execution_grant_item_transition();

ALTER TABLE omp_work.execution_grants FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.execution_grant_items FORCE ROW LEVEL SECURITY;
