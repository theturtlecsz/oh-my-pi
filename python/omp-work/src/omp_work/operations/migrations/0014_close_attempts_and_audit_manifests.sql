-- OMP-47/OMP-49/OMP-50/OMP-51: ledger-owned close attempts, sealed audit manifests,
-- bounded auditor launches, typed close-attempt events, and receipted checkpoint
-- deliveries. omp_evidence.closeout_intents is MOVED (same OID: rows, RLS, and
-- grants survive) into omp_work as close_attempts; the old pending/completed
-- two-state trigger is replaced by full legal-transition enforcement.


-- 0. Migration RLS window: the migrator holds no workspace claim and
--    omp_control.current_workspace_id() RAISES without one, so every DML
--    backfill AND every FK-validation scan against a FORCE-RLS table would
--    abort. Lift FORCE (owner-only bypass; other roles keep full RLS) on the
--    pre-existing tables this migration reads or references, and restore it
--    at the end of this same transaction.
ALTER TABLE omp_evidence.closeout_intents NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_items NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.candidates NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_evidence.receipts NO FORCE ROW LEVEL SECURITY;
-- 1. Move + rename the existing table; drop the two-state machinery.
DROP TRIGGER closeout_intents_state_only ON omp_evidence.closeout_intents;
DROP FUNCTION omp_control.allow_closeout_completion();
ALTER TABLE omp_evidence.closeout_intents SET SCHEMA omp_work;
ALTER TABLE omp_work.closeout_intents RENAME TO close_attempts;
ALTER TABLE omp_work.close_attempts RENAME COLUMN intent_id TO attempt_id;
ALTER TABLE omp_work.close_attempts DROP CONSTRAINT closeout_intents_state_check;
ALTER TABLE omp_work.close_attempts DROP CONSTRAINT closeout_intents_work_id_revision_id_candidate_id_state_key;

-- 2. Attempt identity, authorization, and budget columns.
ALTER TABLE omp_work.close_attempts
  ADD COLUMN plan_receipt_id uuid,
  ADD COLUMN candidate_sha256 text CHECK (candidate_sha256 IS NULL OR candidate_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN candidate_commit text CHECK (candidate_commit IS NULL OR candidate_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  ADD COLUMN owner_session_id text,
  ADD COLUMN owner_session_started_at timestamptz,
  ADD COLUMN owner_session_start_commit text CHECK (owner_session_start_commit IS NULL OR owner_session_start_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  ADD COLUMN repository text,
  ADD COLUMN diff_sha256 text CHECK (diff_sha256 IS NULL OR diff_sha256 ~ '^[0-9a-f]{64}$'),
  ADD COLUMN starting_dirty_paths text[],
  ADD COLUMN authorization_kind text,
  ADD COLUMN authorization_ref text,
  ADD COLUMN launch_count integer NOT NULL DEFAULT 0,
  ADD COLUMN accepted_report_count integer NOT NULL DEFAULT 0,
  ADD COLUMN in_flight_launch_id uuid,
  ADD COLUMN terminal_reason text,
  ADD COLUMN closeout_requested_at timestamptz,
  ADD COLUMN completion_authorization_ref text;


-- 3. Legacy backfill: candidate identity from the candidate row, the plan
--    receipt deterministically (earliest by issued_at, receipt_id), and a
--    per-row-unique legacy authorization reference.
UPDATE omp_work.close_attempts a
   SET candidate_sha256 = c.candidate_sha256, candidate_commit = c.commit_sha
  FROM omp_work.candidates c
 WHERE c.candidate_id = a.candidate_id;
UPDATE omp_work.close_attempts a
   SET plan_receipt_id = (
         SELECT r.receipt_id FROM omp_evidence.receipts r
          WHERE r.workspace_id = a.workspace_id AND r.work_id = a.work_id
            AND r.candidate_id = a.candidate_id AND r.kind = 'plan'
          ORDER BY r.issued_at, r.receipt_id LIMIT 1);
UPDATE omp_work.close_attempts
   SET authorization_kind = 'legacy', authorization_ref = 'legacy:' || attempt_id::text;

-- 4. Legacy state mapping (plan §2, OMP-49): EVERY existing pending owner
--    decision maps to closeout_requested; completed rows stay completed. No
--    pending row is ever silently superseded here — only a new literal owner
--    /summary may supersede a non-terminal attempt. If one work item somehow
--    carries two pending decisions, the one-live-per-work index below fails
--    this migration loudly and the transaction rolls back for operator triage.
UPDATE omp_work.close_attempts
   SET state = 'closeout_requested', closeout_requested_at = requested_at
 WHERE state = 'pending';


-- 5. Workspace-composite reference targets: every child relation below binds
--    workspace_id + id so RLS can never admit a row pointing at another
--    workspace's history (candidates/receipts gain the unique targets here).
ALTER TABLE omp_work.candidates ADD CONSTRAINT candidates_workspace_id_unique UNIQUE (workspace_id, candidate_id);
ALTER TABLE omp_evidence.receipts ADD CONSTRAINT receipts_workspace_id_unique UNIQUE (workspace_id, receipt_id);
ALTER TABLE omp_work.close_attempts ADD CONSTRAINT close_attempts_workspace_id_unique UNIQUE (workspace_id, attempt_id);

-- 6. Constraints (after backfill).
ALTER TABLE omp_work.close_attempts
  ALTER COLUMN authorization_kind SET NOT NULL,
  ALTER COLUMN authorization_ref SET NOT NULL,
  ADD CONSTRAINT close_attempts_state_check CHECK (state IN ('active','audit_ready','auditor_in_flight','audited','closeout_requested','remediation_required','blocked','budget_exhausted','superseded','completed')),
  ADD CONSTRAINT close_attempts_authorization_kind_check CHECK (authorization_kind IN ('summary','legacy')),
  ADD CONSTRAINT close_attempts_launch_budget_check CHECK (launch_count BETWEEN 0 AND 3),
  ADD CONSTRAINT close_attempts_report_budget_check CHECK (accepted_report_count BETWEEN 0 AND 2 AND accepted_report_count <= launch_count),
  ADD CONSTRAINT close_attempts_summary_identity_check CHECK (
    authorization_kind = 'legacy' OR (
      plan_receipt_id IS NOT NULL AND candidate_sha256 IS NOT NULL AND candidate_commit IS NOT NULL
      AND owner_session_id IS NOT NULL AND owner_session_started_at IS NOT NULL AND owner_session_start_commit IS NOT NULL
      AND repository IS NOT NULL AND diff_sha256 IS NOT NULL AND starting_dirty_paths IS NOT NULL)),
  ADD CONSTRAINT close_attempts_completed_at_check CHECK ((state = 'completed') = (completed_at IS NOT NULL)),
  ADD CONSTRAINT close_attempts_terminal_reason_check CHECK ((state IN ('remediation_required','blocked','budget_exhausted','superseded')) = (terminal_reason IS NOT NULL)),
  ADD CONSTRAINT close_attempts_in_flight_check CHECK ((state = 'auditor_in_flight') = (in_flight_launch_id IS NOT NULL)),
  ADD CONSTRAINT close_attempts_work_fk FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  ADD CONSTRAINT close_attempts_candidate_fk FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  ADD CONSTRAINT close_attempts_plan_receipt_fk FOREIGN KEY (workspace_id, plan_receipt_id) REFERENCES omp_evidence.receipts(workspace_id, receipt_id);

CREATE UNIQUE INDEX close_attempts_one_live_per_work ON omp_work.close_attempts(workspace_id, work_id)
  WHERE state IN ('active','audit_ready','auditor_in_flight','audited','closeout_requested');
CREATE UNIQUE INDEX close_attempts_authorization_ref_unique ON omp_work.close_attempts(workspace_id, authorization_kind, authorization_ref);
CREATE UNIQUE INDEX close_attempts_completion_authorization_unique ON omp_work.close_attempts(workspace_id, completion_authorization_ref)
  WHERE completion_authorization_ref IS NOT NULL;

-- 6. Legal-transition enforcement: identity frozen, counters monotonic and
--    transition-bound, terminal rows immutable, DELETE forbidden.
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
CREATE TRIGGER close_attempts_transition BEFORE UPDATE OR DELETE ON omp_work.close_attempts
  FOR EACH ROW EXECUTE FUNCTION omp_control.enforce_close_attempt_transition();

-- 7. Sealed audit manifests: one immutable server-constructed task per attempt.
CREATE TABLE omp_work.audit_manifests (
  manifest_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  manifest_version integer NOT NULL CHECK (manifest_version = 1),
  plan_receipt_id uuid NOT NULL,
  verification_receipt_id uuid NOT NULL,
  candidate_id uuid NOT NULL,
  candidate_sha256 text NOT NULL CHECK (candidate_sha256 ~ '^[0-9a-f]{64}$'),
  candidate_commit text NOT NULL CHECK (candidate_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  task_body text NOT NULL,
  task_sha256 text NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
  section_hashes jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES omp_work.close_attempts(workspace_id, attempt_id),
  FOREIGN KEY (workspace_id, plan_receipt_id) REFERENCES omp_evidence.receipts(workspace_id, receipt_id),
  FOREIGN KEY (workspace_id, verification_receipt_id) REFERENCES omp_evidence.receipts(workspace_id, receipt_id),
  FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  UNIQUE (workspace_id, attempt_id),
  UNIQUE (workspace_id, manifest_id),
  CHECK (octet_length(task_body) <= 1048576)
);

-- 8. Bounded auditor launches: immutable reservations; settlement lives in
--    close_attempt_events plus the attempt's own state (never a mutable status).
CREATE TABLE omp_work.auditor_launches (
  launch_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  attempt_id uuid NOT NULL,
  manifest_id uuid NOT NULL,
  launch_number integer NOT NULL CHECK (launch_number BETWEEN 1 AND 3),
  task_sha256 text NOT NULL CHECK (task_sha256 ~ '^[0-9a-f]{64}$'),
  tool_call_id text NOT NULL,
  reserved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (attempt_id, launch_number),
  UNIQUE (workspace_id, launch_id),
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES omp_work.close_attempts(workspace_id, attempt_id),
  FOREIGN KEY (workspace_id, manifest_id) REFERENCES omp_work.audit_manifests(workspace_id, manifest_id)
);

-- 9. Typed close-attempt events: server-generated, immutable, deliverable.
--    attempt_id is nullable — begin_close_attempt can refuse before an attempt exists.
CREATE TABLE omp_work.close_attempt_events (
  event_id uuid PRIMARY KEY,
  sequence bigserial UNIQUE NOT NULL,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  attempt_id uuid,
  launch_id uuid,
  event_type text NOT NULL,
  reason_code text NOT NULL,
  reason text NOT NULL,
  legal_next_actions text[] NOT NULL,
  remaining_launches integer NOT NULL CHECK (remaining_launches BETWEEN 0 AND 3),
  remaining_reports integer NOT NULL CHECK (remaining_reports BETWEEN 0 AND 2),
  requires_fresh_authorization boolean NOT NULL,
  rendered_text text NOT NULL,
  rendered_sha256 text NOT NULL CHECK (rendered_sha256 ~ '^[0-9a-f]{64}$'),
  requires_delivery boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES omp_work.close_attempts(workspace_id, attempt_id),
  FOREIGN KEY (workspace_id, launch_id) REFERENCES omp_work.auditor_launches(workspace_id, launch_id),
  UNIQUE (workspace_id, event_id)
);
CREATE UNIQUE INDEX close_attempt_events_one_settlement_per_launch ON omp_work.close_attempt_events(workspace_id, launch_id)
  WHERE launch_id IS NOT NULL AND event_type = 'auditor_launch_settled';

-- 10. Checkpoint deliveries: append-only; the latest delivery_sequence wins.
CREATE TABLE omp_work.checkpoint_deliveries (
  delivery_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  event_id uuid NOT NULL,
  delivery_sequence integer NOT NULL CHECK (delivery_sequence >= 1),
  owner_session_id text NOT NULL,
  rendered_sha256 text NOT NULL CHECK (rendered_sha256 ~ '^[0-9a-f]{64}$'),
  status text NOT NULL CHECK (status IN ('delivered','failed','waived')),
  authorization_ref text CHECK ((status = 'waived') = (authorization_ref IS NOT NULL)),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (event_id, delivery_sequence),
  FOREIGN KEY (workspace_id, event_id) REFERENCES omp_work.close_attempt_events(workspace_id, event_id)
);

-- 11. RLS + immutability + least-privilege grants for the new tables
--     (close_attempts keeps its policy and grants through SET SCHEMA; restate
--     its grant to document least privilege).
SELECT omp_control.install_workspace_rls('omp_work.audit_manifests'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.auditor_launches'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.close_attempt_events'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.checkpoint_deliveries'::regclass, 'workspace_id');
CREATE TRIGGER immutable_audit_manifests BEFORE UPDATE OR DELETE ON omp_work.audit_manifests FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_auditor_launches BEFORE UPDATE OR DELETE ON omp_work.auditor_launches FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_close_attempt_events BEFORE UPDATE OR DELETE ON omp_work.close_attempt_events FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_checkpoint_deliveries BEFORE UPDATE OR DELETE ON omp_work.checkpoint_deliveries FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
REVOKE ALL ON omp_work.close_attempts, omp_work.audit_manifests, omp_work.auditor_launches, omp_work.close_attempt_events, omp_work.checkpoint_deliveries FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_work.close_attempts TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.audit_manifests, omp_work.auditor_launches, omp_work.close_attempt_events, omp_work.checkpoint_deliveries TO omp_work_app;
GRANT SELECT ON omp_work.close_attempts, omp_work.audit_manifests, omp_work.auditor_launches, omp_work.close_attempt_events, omp_work.checkpoint_deliveries TO omp_work_readonly;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA omp_work TO omp_work_app;
REVOKE DELETE, TRUNCATE ON omp_work.close_attempts, omp_work.audit_manifests, omp_work.auditor_launches, omp_work.close_attempt_events, omp_work.checkpoint_deliveries FROM omp_work_app, omp_work_readonly, omp_work_importer;

-- 12. Close the migration RLS window (section 0): restore owner-inclusive
--     enforcement on every table it touched.
ALTER TABLE omp_work.close_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.work_items FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.candidates FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_evidence.receipts FORCE ROW LEVEL SECURITY;
