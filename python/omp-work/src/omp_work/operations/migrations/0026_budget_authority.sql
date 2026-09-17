-- PR3 budget authority. Numeric values are decimal NUMERIC, never binary floats.
CREATE TABLE omp_work.budget_scopes (
  scope_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  parent_scope_id uuid REFERENCES omp_work.budget_scopes(scope_id),
  kind text NOT NULL CHECK (kind IN ('account','session','work','tournament','role')),
  policy_version text NOT NULL,
  work_id uuid,
  session_id text,
  limits jsonb NOT NULL,
  held jsonb NOT NULL DEFAULT '{}'::jsonb,
  spent jsonb NOT NULL DEFAULT '{}'::jsonb,
  unresolved jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id)
);
CREATE TABLE omp_work.provider_accounts (
  account_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  provider text NOT NULL,
  account_identity text NOT NULL,
  entitlement_evidence text NOT NULL,
  evidence_observed_at timestamptz NOT NULL,
  billing_mode text NOT NULL CHECK (billing_mode IN ('subscription','metered','purchased_credit','local')),
  rate_card_version text,
  observed_balance numeric,
  balance_provenance text NOT NULL CHECK (balance_provenance IN ('provider_observed','locally_estimated','unknown')),
  reset_at timestamptz,
  concurrency_limit integer NOT NULL CHECK (concurrency_limit > 0),
  UNIQUE (workspace_id, provider, account_identity)
);
CREATE TABLE omp_work.budget_reservations (
  reservation_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  scope_id uuid NOT NULL REFERENCES omp_work.budget_scopes(scope_id),
  account_id uuid NOT NULL REFERENCES omp_work.provider_accounts(account_id),
  logical_call_id uuid NOT NULL,
  transport_attempt_id uuid NOT NULL UNIQUE,
  fence bigint NOT NULL CHECK (fence > 0),
  state text NOT NULL CHECK (state IN ('reserved_unsent','potentially_sent','settled','cancelled_unsent','unresolved')),
  resource text NOT NULL CHECK (resource IN ('cash','included_credit','native_quota','local_compute')),
  worst_case_drawdown numeric NOT NULL CHECK (worst_case_drawdown >= 0),
  actual_drawdown numeric CHECK (actual_drawdown IS NULL OR actual_drawdown >= 0),
  provider text NOT NULL, model text NOT NULL, effort text NOT NULL,
  context_limit integer NOT NULL CHECK (context_limit > 0), output_limit integer NOT NULL CHECK (output_limit > 0),
  provider_request_id text, usage jsonb, provenance text, outcome text,
  expires_at timestamptz NOT NULL, claimed_at timestamptz, settled_at timestamptz,
  UNIQUE (workspace_id, logical_call_id, transport_attempt_id)
);
CREATE TABLE omp_work.frontier_exceptions (
  exception_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  scope_id uuid NOT NULL REFERENCES omp_work.budget_scopes(scope_id),
  question text NOT NULL, route text NOT NULL, effort text NOT NULL,
  context_limit integer NOT NULL CHECK (context_limit > 0), output_limit integer NOT NULL CHECK (output_limit > 0),
  max_attempts integer NOT NULL CHECK (max_attempts > 0), remaining_attempts integer NOT NULL CHECK (remaining_attempts >= 0),
  resource text NOT NULL, resource_limit numeric NOT NULL CHECK (resource_limit >= 0),
  expires_at timestamptz NOT NULL, issued_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
SELECT omp_control.install_workspace_rls('omp_work.budget_scopes'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.provider_accounts'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.budget_reservations'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.frontier_exceptions'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.budget_scopes, omp_work.provider_accounts, omp_work.budget_reservations, omp_work.frontier_exceptions FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_work.budget_scopes, omp_work.budget_reservations, omp_work.frontier_exceptions TO omp_work_app;
GRANT SELECT ON omp_work.provider_accounts TO omp_work_app;
GRANT REFERENCES ON omp_work.provider_accounts TO omp_work_app;
GRANT USAGE ON SCHEMA omp_work TO omp_work_app;
GRANT SELECT ON omp_work.provider_accounts, omp_work.budget_scopes, omp_work.budget_reservations, omp_work.frontier_exceptions TO omp_work_readonly;
