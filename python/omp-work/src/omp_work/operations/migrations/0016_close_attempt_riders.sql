-- OMP-93: batch-completion rider authority (owner ruling 2026-08-22, close
-- asymmetry). Riders are sealed as canonical tuples on the close attempt at
-- /summary; the audited task body carries their titles and evidence verbatim.
-- The transition trigger is replaced to make the sealed riders immutable at
-- the database, alongside the rest of the attempt identity.
ALTER TABLE omp_work.close_attempts ADD COLUMN riders jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(riders) = 'array');

-- Manifest v2 = v1 five sections plus the Riders section; v1 stays the shape
-- of every riderless attempt and all existing rows.
ALTER TABLE omp_work.audit_manifests DROP CONSTRAINT audit_manifests_manifest_version_check;
ALTER TABLE omp_work.audit_manifests ADD CONSTRAINT audit_manifests_manifest_version_check CHECK (manifest_version IN (1, 2));

CREATE OR REPLACE FUNCTION omp_control.enforce_close_attempt_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'close attempts are immutable history'; END IF;
  IF OLD.attempt_id <> NEW.attempt_id OR OLD.workspace_id <> NEW.workspace_id OR OLD.work_id <> NEW.work_id
     OR OLD.revision_id <> NEW.revision_id OR OLD.candidate_id <> NEW.candidate_id
     OR OLD.plan_receipt_id IS DISTINCT FROM NEW.plan_receipt_id
     OR OLD.candidate_sha256 IS DISTINCT FROM NEW.candidate_sha256
     OR OLD.candidate_commit IS DISTINCT FROM NEW.candidate_commit
     OR OLD.owner_session_id IS DISTINCT FROM NEW.owner_session_id
     OR OLD.owner_session_started_at IS DISTINCT FROM NEW.owner_session_started_at
     OR OLD.owner_session_start_commit IS DISTINCT FROM NEW.owner_session_start_commit
     OR OLD.repository IS DISTINCT FROM NEW.repository
     OR OLD.diff_sha256 IS DISTINCT FROM NEW.diff_sha256
     OR OLD.starting_dirty_paths IS DISTINCT FROM NEW.starting_dirty_paths
     OR OLD.riders IS DISTINCT FROM NEW.riders
     OR OLD.authorization_kind <> NEW.authorization_kind OR OLD.authorization_ref <> NEW.authorization_ref
     OR OLD.requested_at <> NEW.requested_at THEN
    RAISE EXCEPTION 'close attempt identity is immutable';
  END IF;
  IF OLD.state IN ('remediation_required','blocked','budget_exhausted','superseded','completed') THEN
    RAISE EXCEPTION 'terminal close attempt is immutable';
  END IF;
  IF NEW.state = OLD.state THEN
    RAISE EXCEPTION 'close attempt updates must follow a legal transition';
  END IF;
  IF NOT ((OLD.state = 'active' AND NEW.state IN ('audit_ready','superseded'))
       OR (OLD.state = 'audit_ready' AND NEW.state IN ('auditor_in_flight','budget_exhausted','superseded'))
       OR (OLD.state = 'auditor_in_flight' AND NEW.state IN ('audit_ready','audited','remediation_required','blocked','budget_exhausted','superseded'))
       OR (OLD.state = 'audited' AND NEW.state IN ('closeout_requested','superseded'))
       OR (OLD.state = 'closeout_requested' AND NEW.state IN ('completed','superseded'))) THEN
    RAISE EXCEPTION 'illegal close attempt transition % -> %', OLD.state, NEW.state;
  END IF;
  IF NEW.launch_count <> OLD.launch_count + (CASE WHEN OLD.state = 'audit_ready' AND NEW.state = 'auditor_in_flight' THEN 1 ELSE 0 END) THEN
    RAISE EXCEPTION 'launch_count changes only by reservation, by exactly one';
  END IF;
  IF NEW.cancelled_launch_count - OLD.cancelled_launch_count NOT IN (0, 1)
     OR (NEW.cancelled_launch_count > OLD.cancelled_launch_count AND NOT (OLD.state = 'auditor_in_flight' AND NEW.state = 'audit_ready')) THEN
    RAISE EXCEPTION 'cancelled_launch_count changes only by host-failure cancellation, by exactly one';
  END IF;
  IF NEW.accepted_report_count <> OLD.accepted_report_count + (CASE WHEN OLD.state = 'auditor_in_flight' AND NEW.state IN ('audited','remediation_required','blocked') THEN 1 ELSE 0 END) THEN
    RAISE EXCEPTION 'accepted_report_count changes only by an accepted report, by exactly one';
  END IF;
  IF NEW.state = 'auditor_in_flight' AND (OLD.in_flight_launch_id IS NOT NULL OR NEW.in_flight_launch_id IS NULL) THEN
    RAISE EXCEPTION 'entering auditor_in_flight sets in_flight_launch_id from NULL';
  END IF;
  IF NEW.state <> 'auditor_in_flight' AND NEW.in_flight_launch_id IS NOT NULL THEN
    RAISE EXCEPTION 'only auditor_in_flight carries an in-flight launch';
  END IF;
  IF NEW.completion_authorization_ref IS DISTINCT FROM OLD.completion_authorization_ref
     AND NOT (OLD.completion_authorization_ref IS NULL AND OLD.state = 'closeout_requested' AND NEW.state = 'completed') THEN
    RAISE EXCEPTION 'completion_authorization_ref is set only when completing a requested closeout';
  END IF;
  IF NEW.closeout_requested_at IS DISTINCT FROM OLD.closeout_requested_at
     AND NOT (OLD.closeout_requested_at IS NULL AND NEW.state = 'closeout_requested') THEN
    RAISE EXCEPTION 'closeout_requested_at is stamped once, on request';
  END IF;
  RETURN NEW;
END $$;
