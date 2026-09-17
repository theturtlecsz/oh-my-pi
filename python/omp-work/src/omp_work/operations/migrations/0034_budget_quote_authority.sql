-- Budget-quote authority: combined immutable provider account selection and worst-case quote seam (OMP-233).
CREATE TABLE omp_work.budget_quotes (
  quote_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid, candidate_id uuid, attempt_id uuid, grant_id uuid,
  role text NOT NULL CHECK (role IN ('plan','implement','frontier','audit')),
  launch_id uuid,
  account_id uuid NOT NULL REFERENCES omp_work.provider_accounts(account_id),
  account_evidence_observed_at timestamptz NOT NULL,
  provider text NOT NULL, model text NOT NULL, effort text NOT NULL,
  rate_card_id uuid NOT NULL REFERENCES omp_work.rate_cards(rate_card_id),
  rate_card_version text NOT NULL,
  currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
  usage_ceiling jsonb NOT NULL CHECK (jsonb_typeof(usage_ceiling) = 'object'),
  worst_case_amount numeric NOT NULL CHECK (worst_case_amount >= 0),
  evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
  quote_sha256 text NOT NULL CHECK (quote_sha256 ~ '^[0-9a-f]{64}$'),
  quoted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, launch_id) REFERENCES omp_work.stage_launches(workspace_id, launch_id)
);

CREATE INDEX budget_quotes_work ON omp_work.budget_quotes(workspace_id, work_id, quoted_at);

SELECT omp_control.install_workspace_rls('omp_work.budget_quotes'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.budget_quotes FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.budget_quotes TO omp_work_app;
GRANT SELECT ON omp_work.budget_quotes TO omp_work_readonly;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_work.budget_quotes FROM omp_work_app, omp_work_readonly, omp_work_importer;
ALTER TABLE omp_work.budget_quotes FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.budget_reservations NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.budget_reservations ADD COLUMN quote_id uuid REFERENCES omp_work.budget_quotes(quote_id);
CREATE UNIQUE INDEX budget_reservations_quote ON omp_work.budget_reservations(quote_id) WHERE quote_id IS NOT NULL;
ALTER TABLE omp_work.budget_reservations FORCE ROW LEVEL SECURITY;
