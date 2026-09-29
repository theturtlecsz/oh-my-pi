-- OMP-418: standing mandates, standing policies and spend budgets.
-- A change inserts a new row and clears active on the row it replaces.
-- omp_work_app may UPDATE only active, plus revoked_at on standing_policies.
-- decision_id is NOT NULL. One active mandate per project. One active spend
-- budget per project (mission_id NULL) and one per mission.
--
-- Rows are keyed by record_id so a later version can keep the same mandate,
-- policy or budget id. The partial unique indexes allow only one active row.

CREATE TABLE omp_work.standing_mandates (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  mandate_id uuid NOT NULL,
  goals text[] NOT NULL DEFAULT '{}',
  repositories text[] NOT NULL DEFAULT '{}',
  capabilities text[] NOT NULL DEFAULT '{}',
  tier3_classes text[] NOT NULL DEFAULT '{}',
  decision_id uuid NOT NULL,
  active bool NOT NULL DEFAULT true,
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE UNIQUE INDEX standing_mandates_one_active
  ON omp_work.standing_mandates (workspace_id, project_id)
  WHERE active;

CREATE TABLE omp_work.standing_policies (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  policy_id uuid NOT NULL,
  action_class text NOT NULL,
  repositories text[] NOT NULL DEFAULT '{}',
  destinations text[] NOT NULL DEFAULT '{}',
  branch_patterns text[] NOT NULL DEFAULT '{}',
  resource_types text[] NOT NULL DEFAULT '{}',
  money_limit_usd numeric,
  expires_at timestamptz,
  decision_id uuid NOT NULL,
  active bool NOT NULL DEFAULT true,
  revoked_at timestamptz,
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  CHECK ((active AND revoked_at IS NULL) OR (NOT active AND revoked_at IS NOT NULL))
);

CREATE UNIQUE INDEX standing_policies_one_active
  ON omp_work.standing_policies (workspace_id, project_id, policy_id)
  WHERE active;

CREATE TABLE omp_work.spend_budgets (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  budget_id text NOT NULL,
  mission_id uuid,
  ceiling_usd numeric NOT NULL CHECK (ceiling_usd > 0),
  threshold_usd numeric CHECK (threshold_usd IS NULL OR threshold_usd <= ceiling_usd),
  decision_id uuid NOT NULL,
  active bool NOT NULL DEFAULT true,
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE UNIQUE INDEX spend_budgets_one_active_project
  ON omp_work.spend_budgets (workspace_id, project_id)
  WHERE active AND mission_id IS NULL;

CREATE UNIQUE INDEX spend_budgets_one_active_mission
  ON omp_work.spend_budgets (workspace_id, project_id, mission_id)
  WHERE active AND mission_id IS NOT NULL;

SELECT omp_control.install_workspace_rls('omp_work.standing_mandates'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.standing_policies'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.spend_budgets'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.standing_mandates, omp_work.standing_policies, omp_work.spend_budgets FROM omp_work_app, omp_work_readonly, omp_work_importer;

GRANT SELECT, INSERT ON omp_work.standing_mandates, omp_work.standing_policies, omp_work.spend_budgets TO omp_work_app;
GRANT UPDATE (active) ON omp_work.standing_mandates TO omp_work_app;
GRANT UPDATE (active, revoked_at) ON omp_work.standing_policies TO omp_work_app;
GRANT UPDATE (active) ON omp_work.spend_budgets TO omp_work_app;
GRANT SELECT ON omp_work.standing_mandates, omp_work.standing_policies, omp_work.spend_budgets TO omp_work_readonly;

REVOKE DELETE, TRUNCATE ON omp_work.standing_mandates, omp_work.standing_policies, omp_work.spend_budgets FROM omp_work_app, omp_work_readonly, omp_work_importer;
