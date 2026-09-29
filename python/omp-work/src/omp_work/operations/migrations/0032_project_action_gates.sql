-- OMP-418: the action gate and spend gate audit records.
-- request_action appends one project_action_records row per evaluated action
-- (allowed or refused) and record_spend appends one spend_records row per
-- admitted spend. Neither table is ever updated: omp_work_app may only SELECT
-- and INSERT, so a recorded decision is immutable.
--
-- project_action_records keeps the whole ActionRequest plus the tier tier_of
-- resolved. A tier 2 action that was allowed names the standing policy that
-- covered it; a tier 3 action that was allowed names the owner decision_id.
-- Refused rows carry outcome 'refused' and the refusal code.
--
-- spend_records keeps an admitted spend amount (> 0): tier1 at or under the
-- budget's threshold, tier2 through its ceiling, and the covering
-- spend_beyond_threshold policy_id that the tier 2 spend named.

CREATE TABLE omp_work.project_action_records (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  mission_id uuid,
  action_class text NOT NULL,
  repository text,
  branch text,
  destination text,
  resource_type text,
  amount_usd numeric,
  tier smallint NOT NULL CHECK (tier IN (1, 2, 3)),
  outcome text NOT NULL CHECK (outcome IN ('allowed', 'refused')),
  code text,
  policy_id uuid,
  decision_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  FOREIGN KEY (workspace_id, mission_id) REFERENCES omp_work.project_missions(workspace_id, mission_id),
  CHECK (tier <> 2 OR outcome <> 'allowed' OR policy_id IS NOT NULL)
);

CREATE TABLE omp_work.spend_records (
  workspace_id uuid NOT NULL,
  record_id uuid NOT NULL,
  project_id uuid NOT NULL,
  mission_id uuid,
  amount_usd numeric NOT NULL CHECK (amount_usd > 0),
  tier text NOT NULL CHECK (tier IN ('tier1', 'tier2')),
  policy_id uuid,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, record_id),
  FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  FOREIGN KEY (workspace_id, mission_id) REFERENCES omp_work.project_missions(workspace_id, mission_id),
  CHECK ((tier = 'tier2') = (policy_id IS NOT NULL))
);

SELECT omp_control.install_workspace_rls('omp_work.project_action_records'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.spend_records'::regclass, 'workspace_id');

REVOKE ALL ON omp_work.project_action_records, omp_work.spend_records FROM omp_work_app, omp_work_readonly, omp_work_importer;

GRANT SELECT, INSERT ON omp_work.project_action_records, omp_work.spend_records TO omp_work_app;
GRANT SELECT ON omp_work.project_action_records, omp_work.spend_records TO omp_work_readonly;

REVOKE DELETE, TRUNCATE ON omp_work.project_action_records, omp_work.spend_records FROM omp_work_app, omp_work_readonly, omp_work_importer;
