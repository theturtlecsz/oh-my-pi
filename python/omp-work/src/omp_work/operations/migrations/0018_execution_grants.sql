-- OMP-180: one-command autonomous delivery cycle execution grants.
-- Execution grants track the literal-owner authorized delivery cycle across single
-- items or finite project queues, managing criteria, plan stamping, reviews,
-- bound remote push, audit manifests with original requests, and atomic completion.

ALTER TABLE omp_work.close_attempts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.audit_manifests NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_items NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_revisions NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_evidence.receipts NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.work_revisions ADD CONSTRAINT work_revisions_workspace_id_unique UNIQUE (workspace_id, revision_id);

CREATE TABLE omp_work.execution_grants (
  grant_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  owner_id uuid NOT NULL,
  repository text NOT NULL,
  state text NOT NULL CHECK (state IN ('active', 'paused', 'stopped', 'completed', 'canceled')),
  mode text NOT NULL CHECK (mode IN ('single', 'queue')),
  grant_version integer NOT NULL DEFAULT 1 CHECK (grant_version >= 1),
  max_continuations integer NOT NULL DEFAULT 8 CHECK (max_continuations >= 1),
  max_close_attempts integer NOT NULL DEFAULT 5 CHECK (max_close_attempts >= 1),
  max_no_progress integer NOT NULL DEFAULT 3 CHECK (max_no_progress >= 1),
  continuations_scheduled integer NOT NULL DEFAULT 0 CHECK (continuations_scheduled >= 0),
  terminal_reason text,
  authorization_hash text NOT NULL CHECK (authorization_hash ~ '^[0-9a-f]{64}$'),
  provenance jsonb NOT NULL,
  judge_sha256 text NOT NULL CHECK (judge_sha256 ~ '^[0-9a-f]{64}$'),
  judge_manifest jsonb NOT NULL,
  focus_version_at_grant integer NOT NULL CHECK (focus_version_at_grant >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  expires_at timestamptz NOT NULL,
  completed_at timestamptz,
  paused_at timestamptz,
  stopped_at timestamptz,
  canceled_at timestamptz,
  FOREIGN KEY (workspace_id) REFERENCES omp_control.workspaces(workspace_id),
  UNIQUE (workspace_id, grant_id),
  CHECK ((state = 'completed') = (completed_at IS NOT NULL)),
  CHECK ((state = 'paused') = (paused_at IS NOT NULL)),
  CHECK ((state = 'stopped') = (stopped_at IS NOT NULL)),
  CHECK ((state = 'canceled') = (canceled_at IS NOT NULL)),
  CHECK ((state IN ('stopped', 'canceled')) = (terminal_reason IS NOT NULL))
);

CREATE UNIQUE INDEX execution_grants_one_active ON omp_work.execution_grants(workspace_id)
  WHERE state IN ('active', 'paused');

CREATE TABLE omp_work.execution_grant_items (
  item_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  grant_id uuid NOT NULL,
  work_id uuid NOT NULL,
  position integer NOT NULL CHECK (position >= 0),
  phase text NOT NULL CHECK (phase IN ('pending', 'criteria_pending', 'planning', 'executing', 'reviewing', 'remediating', 'awaiting_contract_approval', 'completed', 'abandoned', 'skipped')),
  claimed_revision_id uuid NOT NULL,
  project_id uuid,
  active_blocker_ids uuid[] NOT NULL DEFAULT '{}',
  initial_git_baseline text NOT NULL CHECK (initial_git_baseline ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  current_git_baseline text CHECK (current_git_baseline IS NULL OR current_git_baseline ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  criteria_revision_id uuid,
  original_request text NOT NULL,
  original_request_sha256 text NOT NULL CHECK (original_request_sha256 ~ '^[0-9a-f]{64}$'),
  criteria_sha256 text CHECK (criteria_sha256 IS NULL OR criteria_sha256 ~ '^[0-9a-f]{64}$'),
  plan_stamp_sha256 text CHECK (plan_stamp_sha256 IS NULL OR plan_stamp_sha256 ~ '^[0-9a-f]{64}$'),
  plan_stamp jsonb,
  close_attempts_started integer NOT NULL DEFAULT 0 CHECK (close_attempts_started >= 0),
  consecutive_no_progress integer NOT NULL DEFAULT 0 CHECK (consecutive_no_progress >= 0),
  last_reviewed_tree_sha text,
  last_findings_hash text,
  push_receipt_id uuid,
  closeout_receipt_id uuid,
  activated_at timestamptz,
  completed_at timestamptz,
  abandoned_at timestamptz,
  skipped_at timestamptz,
  terminal_reason text,
  FOREIGN KEY (workspace_id, grant_id) REFERENCES omp_work.execution_grants(workspace_id, grant_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, claimed_revision_id) REFERENCES omp_work.work_revisions(workspace_id, revision_id),
  FOREIGN KEY (workspace_id, criteria_revision_id) REFERENCES omp_work.work_revisions(workspace_id, revision_id),
  FOREIGN KEY (workspace_id, push_receipt_id) REFERENCES omp_evidence.receipts(workspace_id, receipt_id),
  FOREIGN KEY (workspace_id, closeout_receipt_id) REFERENCES omp_evidence.receipts(workspace_id, receipt_id),
  UNIQUE (grant_id, position),
  UNIQUE (grant_id, work_id),
  UNIQUE (workspace_id, item_id),
  CHECK ((phase = 'completed') = (completed_at IS NOT NULL)),
  CHECK ((phase = 'abandoned') = (abandoned_at IS NOT NULL)),
  CHECK ((phase = 'skipped') = (skipped_at IS NOT NULL))
);

ALTER TABLE omp_work.close_attempts
  ADD COLUMN execution_grant_id uuid,
  ADD COLUMN candidate_tree_sha text,
  ADD COLUMN original_request_sha256 text CHECK (original_request_sha256 IS NULL OR original_request_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN criteria_sha256 text CHECK (criteria_sha256 IS NULL OR criteria_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN plan_stamp_sha256 text CHECK (plan_stamp_sha256 IS NULL OR plan_stamp_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN judge_sha256 text CHECK (judge_sha256 IS NULL OR judge_sha256 ~ '^[0-9a-f]{64}$');

ALTER TABLE omp_work.close_attempts
  ADD CONSTRAINT close_attempts_execution_grant_fk
  FOREIGN KEY (workspace_id, execution_grant_id) REFERENCES omp_work.execution_grants(workspace_id, grant_id);

ALTER TABLE omp_work.close_attempts DROP CONSTRAINT close_attempts_authorization_kind_check;
ALTER TABLE omp_work.close_attempts ADD CONSTRAINT close_attempts_authorization_kind_check CHECK (authorization_kind IN ('summary', 'legacy', 'execution'));

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
     OR OLD.execution_grant_id IS DISTINCT FROM NEW.execution_grant_id
     OR OLD.candidate_tree_sha IS DISTINCT FROM NEW.candidate_tree_sha
     OR OLD.original_request_sha256 IS DISTINCT FROM NEW.original_request_sha256
     OR OLD.criteria_sha256 IS DISTINCT FROM NEW.criteria_sha256
     OR OLD.plan_stamp_sha256 IS DISTINCT FROM NEW.plan_stamp_sha256
     OR OLD.judge_sha256 IS DISTINCT FROM NEW.judge_sha256
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

ALTER TABLE omp_work.audit_manifests DROP CONSTRAINT audit_manifests_manifest_version_check;
ALTER TABLE omp_work.audit_manifests ADD CONSTRAINT audit_manifests_manifest_version_check CHECK (manifest_version IN (1, 2, 3));

SELECT omp_control.install_workspace_rls('omp_work.execution_grants'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.execution_grant_items'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.execution_grants, omp_work.execution_grant_items FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_work.execution_grants, omp_work.execution_grant_items TO omp_work_app;
GRANT SELECT ON omp_work.execution_grants, omp_work.execution_grant_items TO omp_work_readonly;
REVOKE DELETE, TRUNCATE ON omp_work.execution_grants, omp_work.execution_grant_items FROM omp_work_app, omp_work_readonly, omp_work_importer;

ALTER TABLE omp_work.close_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.audit_manifests FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_items FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_evidence.receipts FORCE ROW LEVEL SECURITY;
