-- OMP-140: durable and resumable close authorizations.
-- Authorizations are recorded in an append-only ledger keyed by (workspace_id, authorization_ref).
-- Begin and resume uses bind the canonical attempt identity, event, and returned outcome.

ALTER TABLE omp_work.close_attempts NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.close_attempt_events NO FORCE ROW LEVEL SECURITY;

CREATE TABLE omp_work.authorization_uses (
  workspace_id uuid NOT NULL,
  authorization_ref text NOT NULL,
  use_kind text NOT NULL CHECK (use_kind IN ('begin', 'resume')),
  attempt_id uuid NOT NULL,
  identity_sha256 text NOT NULL CHECK (identity_sha256 ~ '^[0-9a-f]{64}$'),
  owner_session_id text NOT NULL,
  outcome jsonb NOT NULL,
  event_id uuid NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, authorization_ref),
  FOREIGN KEY (workspace_id, attempt_id) REFERENCES omp_work.close_attempts(workspace_id, attempt_id),
  FOREIGN KEY (workspace_id, event_id) REFERENCES omp_work.close_attempt_events(workspace_id, event_id)
);

SELECT omp_control.install_workspace_rls('omp_work.authorization_uses'::regclass, 'workspace_id');
CREATE TRIGGER immutable_authorization_uses BEFORE UPDATE OR DELETE ON omp_work.authorization_uses FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

REVOKE ALL ON omp_work.authorization_uses FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.authorization_uses TO omp_work_app;
GRANT SELECT ON omp_work.authorization_uses TO omp_work_readonly;
REVOKE DELETE, TRUNCATE ON omp_work.authorization_uses FROM omp_work_app, omp_work_readonly, omp_work_importer;

ALTER TABLE omp_work.close_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.close_attempt_events FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.authorization_uses FORCE ROW LEVEL SECURITY;
