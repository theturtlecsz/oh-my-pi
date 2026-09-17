-- Research contract core: campaigns, trials, observations, and deliverable bindings (R02-S1).
CREATE SCHEMA IF NOT EXISTS omp_research AUTHORIZATION omp_work_owner;
REVOKE ALL ON SCHEMA omp_research FROM PUBLIC;
GRANT USAGE ON SCHEMA omp_research TO omp_work_app, omp_work_importer, omp_work_readonly, omp_work_backup;

CREATE OR REPLACE FUNCTION omp_control.install_workspace_rls(target regclass, workspace_column name) RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog AS $$
DECLARE target_schema text; column_type regtype; nullable bool;
BEGIN
 SELECT n.nspname, a.atttypid::regtype, a.attnotnull INTO target_schema, column_type, nullable FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_attribute a ON a.attrelid=c.oid AND a.attname=workspace_column AND a.attnum>0 AND NOT a.attisdropped WHERE c.oid=target;
 IF target_schema NOT IN ('omp_control','omp_work','omp_evidence','omp_audit','omp_integration','omp_fleet','omp_research') THEN RAISE EXCEPTION 'invalid RLS schema'; END IF;
 IF column_type IS DISTINCT FROM 'uuid'::regtype OR nullable IS DISTINCT FROM true THEN RAISE EXCEPTION 'workspace column must be NOT NULL UUID'; END IF;
 EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', target);
 EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', target);
 EXECUTE format('CREATE POLICY workspace_actor_policy ON %s USING (%I = omp_control.current_workspace_id() AND omp_control.current_actor_id() IS NOT NULL) WITH CHECK (%I = omp_control.current_workspace_id() AND omp_control.current_actor_id() IS NOT NULL)', target, workspace_column, workspace_column);
END $$;

CREATE TABLE omp_research.campaigns (
  campaign_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  domain text NOT NULL CHECK (domain IN ('engineering','omp_harness','machine_learning','literature','simulation','external_instrument')),
  spec jsonb NOT NULL,
  spec_sha256 text NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
  policy_sha256 text CHECK (policy_sha256 IS NULL OR policy_sha256 ~ '^[0-9a-f]{64}$'),
  state text NOT NULL CHECK (state IN ('draft','admitted','cancelled')),
  cancel_reason text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  admitted_at timestamptz,
  cancelled_at timestamptz,
  UNIQUE (workspace_id, campaign_id),
  UNIQUE (workspace_id, campaign_id, work_id),
  UNIQUE (workspace_id, campaign_id, work_id, revision_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, revision_id) REFERENCES omp_work.work_revisions(workspace_id, revision_id),
  CHECK (octet_length(spec::text) <= 1048576),
  CHECK (state <> 'admitted' OR policy_sha256 IS NOT NULL),
  CHECK (state <> 'admitted' OR admitted_at IS NOT NULL),
  CHECK (state <> 'cancelled' OR cancelled_at IS NOT NULL)
);

SELECT omp_control.install_workspace_rls('omp_research.campaigns'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.campaigns FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_research.campaigns TO omp_work_app;
GRANT SELECT ON omp_research.campaigns TO omp_work_readonly, omp_work_backup;
REVOKE DELETE, TRUNCATE ON omp_research.campaigns FROM omp_work_app, omp_work_readonly, omp_work_importer;

CREATE TABLE omp_research.trials (
  trial_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  work_id uuid NOT NULL,
  decision_id uuid NOT NULL,
  candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
  experiment_spec_sha256 text NOT NULL CHECK (experiment_spec_sha256 ~ '^[0-9a-f]{64}$'),
  evaluator_sha256 text NOT NULL CHECK (evaluator_sha256 ~ '^[0-9a-f]{64}$'),
  environment_sha256 text NOT NULL CHECK (environment_sha256 ~ '^[0-9a-f]{64}$'),
  input_manifest_sha256 text NOT NULL CHECK (input_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  seed bigint,
  hardware_class text,
  resource_request jsonb,
  policy_sha256 text NOT NULL CHECK (policy_sha256 ~ '^[0-9a-f]{64}$'),
  state text NOT NULL CHECK (state IN ('proposed','archived')),
  archived_reason text,
  proposed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  archived_at timestamptz,
  UNIQUE (workspace_id, trial_id),
  UNIQUE (workspace_id, decision_id),
  UNIQUE (workspace_id, campaign_id, trial_id),
  UNIQUE (workspace_id, trial_id, campaign_id, work_id),
  FOREIGN KEY (workspace_id, campaign_id, work_id) REFERENCES omp_research.campaigns(workspace_id, campaign_id, work_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  CHECK (resource_request IS NULL OR octet_length(resource_request::text) <= 1048576),
  CHECK (state <> 'archived' OR archived_at IS NOT NULL)
);

SELECT omp_control.install_workspace_rls('omp_research.trials'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.trials FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_research.trials TO omp_work_app;
GRANT SELECT ON omp_research.trials TO omp_work_readonly, omp_work_backup;
REVOKE DELETE, TRUNCATE ON omp_research.trials FROM omp_work_app, omp_work_readonly, omp_work_importer;

CREATE TABLE omp_research.observations (
  observation_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  trial_id uuid,
  issuer_kind text NOT NULL CHECK (issuer_kind IN ('legacy_autoresearch','candidate_authored')),
  source_ref text NOT NULL,
  execution_status text NOT NULL CHECK (execution_status IN ('completed','crashed','timed_out','canceled','unknown')),
  commit_sha text CHECK (commit_sha IS NULL OR commit_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  payload jsonb NOT NULL,
  payload_sha256 text NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
  observed_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, observation_id),
  UNIQUE (workspace_id, issuer_kind, source_ref),
  FOREIGN KEY (workspace_id, campaign_id) REFERENCES omp_research.campaigns(workspace_id, campaign_id),
  FOREIGN KEY (workspace_id, campaign_id, trial_id) REFERENCES omp_research.trials(workspace_id, campaign_id, trial_id),
  CHECK (octet_length(payload::text) <= 1048576)
);

SELECT omp_control.install_workspace_rls('omp_research.observations'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.observations FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.observations TO omp_work_app;
GRANT SELECT ON omp_research.observations TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.observations FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_observations BEFORE UPDATE OR DELETE ON omp_research.observations FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.deliverable_bindings (
  trial_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  campaign_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  candidate_digest text NOT NULL CHECK (candidate_digest ~ '^[0-9a-f]{64}$'),
  native_candidate_id uuid NOT NULL,
  binding_sha256 text NOT NULL CHECK (binding_sha256 ~ '^[0-9a-f]{64}$'),
  bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, trial_id),
  FOREIGN KEY (workspace_id, trial_id, campaign_id, work_id) REFERENCES omp_research.trials(workspace_id, trial_id, campaign_id, work_id),
  FOREIGN KEY (workspace_id, campaign_id, work_id, revision_id) REFERENCES omp_research.campaigns(workspace_id, campaign_id, work_id, revision_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, revision_id) REFERENCES omp_work.work_revisions(workspace_id, revision_id),
  FOREIGN KEY (workspace_id, native_candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id)
);

SELECT omp_control.install_workspace_rls('omp_research.deliverable_bindings'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.deliverable_bindings FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.deliverable_bindings TO omp_work_app;
GRANT SELECT ON omp_research.deliverable_bindings TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.deliverable_bindings FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_deliverable_bindings BEFORE UPDATE OR DELETE ON omp_research.deliverable_bindings FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
