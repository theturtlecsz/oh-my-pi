-- WorkService-owned stage transport-preflight reconciliation authority (OMP-233).
-- Settle uncertain dispatched preflight intents with operator-authorized provider observation evidence.

CREATE TABLE omp_work.stage_preflight_reconciliations (
  reconciliation_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  transport_attempt_id uuid NOT NULL,
  account_id uuid NOT NULL,
  observation_id uuid NOT NULL,
  disposition text NOT NULL CHECK (disposition IN ('indeterminate', 'completed', 'failed', 'confirmed_absent')),
  observed_at timestamptz NOT NULL,
  evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  provider_request_id text,
  requests integer CHECK (requests IS NULL OR requests >= 0),
  usage jsonb,
  stop_reason text,
  error text,
  reconciled_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (
    (disposition = 'indeterminate')
    OR (disposition = 'completed' AND provider_request_id IS NOT NULL AND error IS NULL)
    OR (disposition = 'failed' AND provider_request_id IS NOT NULL AND error IS NOT NULL)
    OR (disposition = 'confirmed_absent' AND provider_request_id IS NULL AND usage IS NULL AND (requests IS NULL OR requests = 0) AND error IS NOT NULL)
  ),
  UNIQUE (workspace_id, observation_id),
  FOREIGN KEY (workspace_id, transport_attempt_id) REFERENCES omp_work.stage_preflight_intents (workspace_id, transport_attempt_id),
  FOREIGN KEY (account_id) REFERENCES omp_work.provider_accounts (account_id)
);

CREATE INDEX stage_preflight_reconciliations_attempt ON omp_work.stage_preflight_reconciliations(workspace_id, transport_attempt_id);
CREATE INDEX stage_preflight_reconciliations_account ON omp_work.stage_preflight_reconciliations(workspace_id, account_id);
CREATE UNIQUE INDEX stage_preflight_reconciliations_conclusive ON omp_work.stage_preflight_reconciliations(workspace_id, transport_attempt_id) WHERE disposition IN ('completed', 'failed', 'confirmed_absent');

SELECT omp_control.install_workspace_rls('omp_work.stage_preflight_reconciliations'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.stage_preflight_reconciliations FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.stage_preflight_reconciliations TO omp_work_app;
GRANT SELECT ON omp_work.stage_preflight_reconciliations TO omp_work_readonly;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_work.stage_preflight_reconciliations FROM omp_work_app, omp_work_readonly, omp_work_importer;
ALTER TABLE omp_work.stage_preflight_reconciliations FORCE ROW LEVEL SECURITY;
