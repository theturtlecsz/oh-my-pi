CREATE TABLE omp_work.repositories (
  repository_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES omp_control.workspaces,
  key text NOT NULL,
  name text NOT NULL,
  url text NOT NULL,
  archived bool NOT NULL DEFAULT false,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, repository_id),
  UNIQUE (workspace_id, key)
);

CREATE TABLE omp_work.principals (
  principal_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES omp_control.workspaces,
  name text NOT NULL,
  display_name text NOT NULL,
  active bool NOT NULL DEFAULT true,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, principal_id)
);

CREATE TABLE omp_work.workflow_states (
  workflow_state_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES omp_control.workspaces,
  name text NOT NULL,
  state_type text NOT NULL,
  position integer NOT NULL,
  archived bool NOT NULL DEFAULT false,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, workflow_state_id)
);

CREATE TABLE omp_work.projects (
  project_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES omp_control.workspaces,
  key text,
  name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('world','surface','promise')),
  target_date date,
  archived bool NOT NULL DEFAULT false,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, project_id)
);

CREATE TABLE omp_work.project_relations (
  relation_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  source_project_id uuid NOT NULL,
  target_project_id uuid NOT NULL,
  kind text NOT NULL CHECK (kind IN ('initiative_project','project_milestone')),
  active bool NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, relation_id),
  FOREIGN KEY (workspace_id, source_project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  FOREIGN KEY (workspace_id, target_project_id) REFERENCES omp_work.projects(workspace_id, project_id)
);

CREATE UNIQUE INDEX project_relations_active_unique ON omp_work.project_relations(workspace_id, source_project_id, target_project_id, kind) WHERE active;

-- Parent edges are stored child -> parent (target is the parent), so one-parent-per-child
-- constrains the SOURCE. The 0003 index constrained the target (one child per parent),
-- which would have rejected ordinary siblings; replace it without editing 0003.
DROP INDEX omp_work.work_relations_parent_target;
CREATE UNIQUE INDEX work_relations_parent_source ON omp_work.work_relations(workspace_id, source_work_id) WHERE active AND kind = 'parent';

CREATE TABLE omp_work.labels (
  label_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL REFERENCES omp_control.workspaces,
  name text NOT NULL,
  color text,
  archived bool NOT NULL DEFAULT false,
  provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, label_id),
  UNIQUE (workspace_id, name)
);

CREATE TABLE omp_work.work_item_labels (
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  label_id uuid NOT NULL,
  active boolean NOT NULL DEFAULT true,
  origin text NOT NULL DEFAULT 'local',
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, work_id, label_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, label_id) REFERENCES omp_work.labels(workspace_id, label_id)
);

ALTER TABLE omp_work.work_items NO FORCE ROW LEVEL SECURITY;

ALTER TABLE omp_work.work_items
  ADD COLUMN repository_id uuid,
  ADD COLUMN project_id uuid,
  ADD COLUMN workflow_state_id uuid,
  ADD COLUMN assignee_id uuid,
  ADD COLUMN priority integer,
  ADD COLUMN source_updated_at timestamptz;

ALTER TABLE omp_work.work_items
  ADD CONSTRAINT work_items_repository_fk
    FOREIGN KEY (workspace_id, repository_id) REFERENCES omp_work.repositories(workspace_id, repository_id),
  ADD CONSTRAINT work_items_project_fk
    FOREIGN KEY (workspace_id, project_id) REFERENCES omp_work.projects(workspace_id, project_id),
  ADD CONSTRAINT work_items_workflow_state_fk
    FOREIGN KEY (workspace_id, workflow_state_id) REFERENCES omp_work.workflow_states(workspace_id, workflow_state_id),
  ADD CONSTRAINT work_items_assignee_fk
    FOREIGN KEY (workspace_id, assignee_id) REFERENCES omp_work.principals(workspace_id, principal_id);

ALTER TABLE omp_work.work_items FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_integration.external_refs
  RENAME COLUMN work_id TO local_id;

ALTER TABLE omp_integration.external_refs
  ADD COLUMN local_type text NOT NULL DEFAULT 'work_item',
  ADD COLUMN source_identifier text,
  ADD COLUMN source_url text;

ALTER TABLE omp_integration.external_refs
  ALTER COLUMN local_type DROP DEFAULT;

CREATE TABLE omp_integration.import_batches (
  batch_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  export_id uuid NOT NULL,
  base_batch_id uuid,
  transformation_version text NOT NULL,
  mapping_file_sha256 text NOT NULL CHECK (mapping_file_sha256 ~ '^[0-9a-f]{64}$'),
  state text NOT NULL CHECK (state IN ('staging','staged','reconciled','promoted','blocked')),
  reconciliation_sha256 text CHECK (reconciliation_sha256 ~ '^[0-9a-f]{64}$'),
  parity_hashes jsonb NOT NULL DEFAULT '{}'::jsonb,
  artifact_root text NOT NULL CHECK (artifact_root !~ '^/'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  staged_at timestamptz,
  reconciled_at timestamptz,
  promoted_at timestamptz,
  UNIQUE (workspace_id, batch_id),
  UNIQUE (workspace_id, export_id, transformation_version),
  FOREIGN KEY (workspace_id, export_id) REFERENCES omp_integration.raw_exports(workspace_id, export_id),
  FOREIGN KEY (workspace_id, base_batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_pages (
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  stream text NOT NULL,
  page_index integer NOT NULL CHECK (page_index >= 0),
  artifact_key text NOT NULL,
  plaintext_sha256 text NOT NULL CHECK (plaintext_sha256 ~ '^[0-9a-f]{64}$'),
  node_count integer NOT NULL CHECK (node_count >= 0),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (batch_id, stream, page_index),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_page_records (
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  stream text NOT NULL,
  page_index integer NOT NULL CHECK (page_index >= 0),
  occurrence_index integer NOT NULL CHECK (occurrence_index >= 0),
  source_id text NOT NULL,
  raw_record_sha256 text NOT NULL CHECK (raw_record_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (batch_id, stream, page_index, occurrence_index),
  FOREIGN KEY (batch_id, stream, page_index) REFERENCES omp_integration.import_pages(batch_id, stream, page_index),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_records (
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  entity_type text NOT NULL,
  source_id text NOT NULL,
  local_id uuid NOT NULL,
  local_type text NOT NULL,
  artifact_ref text NOT NULL,
  source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  logical_sha256 text NOT NULL CHECK (logical_sha256 ~ '^[0-9a-f]{64}$'),
  transformed_json jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (batch_id, entity_type, source_id),
  UNIQUE (batch_id, local_id),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_relations (
  relation_id uuid PRIMARY KEY,
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  relation_kind text NOT NULL,
  source_entity_type text NOT NULL,
  source_id text NOT NULL,
  target_entity_type text NOT NULL,
  target_id text NOT NULL,
  local_source_id uuid,
  local_target_id uuid,
  canonical_id uuid,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','validated','blocked','quarantined')),
  anomaly_code text,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, relation_id),
  UNIQUE (batch_id, relation_kind, source_id, target_id),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_record_results (
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  entity_type text NOT NULL,
  source_id text NOT NULL,
  local_id uuid NOT NULL,
  local_type text NOT NULL,
  disposition text NOT NULL CHECK (disposition IN ('created','unchanged','revised','projection_updated','legacy_untrusted','metadata_only','quarantined','blocked')),
  canonical_sha256 text CHECK (canonical_sha256 ~ '^[0-9a-f]{64}$'),
  revision_id uuid,
  resulting_row_version integer,
  promoted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (batch_id, entity_type, source_id),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.migration_anomalies (
  anomaly_id uuid PRIMARY KEY,
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  origin text NOT NULL CHECK (origin IN ('exporter','importer')),
  code text NOT NULL,
  disposition text NOT NULL CHECK (disposition IN ('informational','quarantined','blocking')),
  entity_type text,
  source_id text,
  message text NOT NULL,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, anomaly_id),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

CREATE TABLE omp_integration.import_artifacts (
  artifact_id uuid PRIMARY KEY,
  batch_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  name text NOT NULL,
  artifact_path text NOT NULL CHECK (artifact_path !~ '^/'),
  plaintext_sha256 text NOT NULL CHECK (plaintext_sha256 ~ '^[0-9a-f]{64}$'),
  ciphertext_sha256 text NOT NULL CHECK (ciphertext_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  UNIQUE (workspace_id, artifact_id),
  UNIQUE (batch_id, name),
  FOREIGN KEY (workspace_id, batch_id) REFERENCES omp_integration.import_batches(workspace_id, batch_id)
);

SELECT omp_control.install_workspace_rls('omp_work.repositories'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.principals'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.workflow_states'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.projects'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.project_relations'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.labels'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_work.work_item_labels'::regclass, 'workspace_id');

SELECT omp_control.install_workspace_rls('omp_integration.import_batches'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_pages'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_page_records'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_records'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_relations'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_record_results'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.migration_anomalies'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.import_artifacts'::regclass, 'workspace_id');

CREATE TRIGGER immutable_import_pages BEFORE UPDATE OR DELETE ON omp_integration.import_pages FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_import_page_records BEFORE UPDATE OR DELETE ON omp_integration.import_page_records FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_import_records BEFORE UPDATE OR DELETE ON omp_integration.import_records FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_import_record_results BEFORE UPDATE OR DELETE ON omp_integration.import_record_results FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_migration_anomalies BEFORE UPDATE OR DELETE ON omp_integration.migration_anomalies FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE TRIGGER immutable_import_artifacts BEFORE UPDATE OR DELETE ON omp_integration.import_artifacts FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE OR REPLACE FUNCTION omp_integration.advance_import_batch() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'import batch is immutable';
  END IF;
  IF OLD.workspace_id <> NEW.workspace_id
    OR OLD.batch_id <> NEW.batch_id
    OR OLD.export_id <> NEW.export_id
    OR OLD.base_batch_id IS DISTINCT FROM NEW.base_batch_id
    OR OLD.transformation_version <> NEW.transformation_version
    OR OLD.mapping_file_sha256 <> NEW.mapping_file_sha256
    OR OLD.artifact_root <> NEW.artifact_root THEN
    RAISE EXCEPTION 'import batch core metadata is immutable';
  END IF;

  IF NEW.state = 'blocked' AND OLD.state IN ('staging', 'staged', 'reconciled') THEN
    RETURN NEW;
  END IF;

  IF OLD.state = 'staging' AND NEW.state = 'staged' AND OLD.staged_at IS NULL AND NEW.staged_at IS NOT NULL THEN
    RETURN NEW;
  END IF;

  IF OLD.state = 'staged' AND NEW.state = 'reconciled' AND OLD.reconciled_at IS NULL AND NEW.reconciled_at IS NOT NULL AND NEW.reconciliation_sha256 IS NOT NULL THEN
    RETURN NEW;
  END IF;

  IF OLD.state = 'reconciled' AND NEW.state = 'promoted' AND OLD.promoted_at IS NULL AND NEW.promoted_at IS NOT NULL AND OLD.reconciliation_sha256 = NEW.reconciliation_sha256 THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'invalid import batch state transition';
END $$;
CREATE TRIGGER import_batches_advance BEFORE UPDATE OR DELETE ON omp_integration.import_batches FOR EACH ROW EXECUTE FUNCTION omp_integration.advance_import_batch();

CREATE OR REPLACE FUNCTION omp_integration.advance_import_relation() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'import relation is immutable';
  END IF;
  IF OLD.workspace_id <> NEW.workspace_id
    OR OLD.relation_id <> NEW.relation_id
    OR OLD.batch_id <> NEW.batch_id
    OR OLD.relation_kind <> NEW.relation_kind
    OR OLD.source_entity_type <> NEW.source_entity_type
    OR OLD.source_id <> NEW.source_id
    OR OLD.target_entity_type <> NEW.target_entity_type
    OR OLD.target_id <> NEW.target_id THEN
    RAISE EXCEPTION 'import relation endpoints are immutable';
  END IF;

  IF OLD.state = 'pending' AND NEW.state IN ('validated', 'blocked', 'quarantined') THEN
    RETURN NEW;
  END IF;

  RAISE EXCEPTION 'import relation can only transition from pending to validated, blocked, or quarantined';
END $$;
CREATE TRIGGER import_relations_advance BEFORE UPDATE OR DELETE ON omp_integration.import_relations FOR EACH ROW EXECUTE FUNCTION omp_integration.advance_import_relation();

CREATE OR REPLACE FUNCTION omp_work.protect_lookup_provenance() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND OLD.provenance IS DISTINCT FROM NEW.provenance THEN
    RAISE EXCEPTION 'provenance is immutable';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER protect_repositories_provenance BEFORE UPDATE ON omp_work.repositories FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();
CREATE TRIGGER protect_principals_provenance BEFORE UPDATE ON omp_work.principals FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();
CREATE TRIGGER protect_workflow_states_provenance BEFORE UPDATE ON omp_work.workflow_states FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();
CREATE TRIGGER protect_projects_provenance BEFORE UPDATE ON omp_work.projects FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();
CREATE TRIGGER protect_labels_provenance BEFORE UPDATE ON omp_work.labels FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();
CREATE TRIGGER protect_work_items_provenance BEFORE UPDATE ON omp_work.work_items FOR EACH ROW EXECUTE FUNCTION omp_work.protect_lookup_provenance();

GRANT USAGE, CREATE ON SCHEMA omp_integration TO omp_work_lookup;
GRANT USAGE ON SCHEMA omp_control TO omp_work_lookup;

CREATE OR REPLACE FUNCTION omp_integration.lookup_batch_workspace(target_batch_id uuid)
RETURNS uuid
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  resolved_workspace uuid;
BEGIN
  IF omp_control.current_actor_id() IS NULL THEN
    RAISE EXCEPTION 'actor claim required';
  END IF;
  SELECT workspace_id INTO resolved_workspace
  FROM omp_integration.import_batches
  WHERE batch_id = target_batch_id;
  RETURN resolved_workspace;
END $$;

ALTER FUNCTION omp_integration.lookup_batch_workspace(uuid) OWNER TO omp_work_lookup;
REVOKE ALL ON FUNCTION omp_integration.lookup_batch_workspace(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION omp_integration.lookup_batch_workspace(uuid) TO omp_work_importer;
GRANT SELECT ON omp_integration.import_batches TO omp_work_lookup;
REVOKE ALL ON
  omp_work.repositories,
  omp_work.principals,
  omp_work.workflow_states,
  omp_work.projects,
  omp_work.project_relations,
  omp_work.labels,
  omp_work.work_item_labels,
  omp_integration.import_batches,
  omp_integration.import_pages,
  omp_integration.import_page_records,
  omp_integration.import_records,
  omp_integration.import_relations,
  omp_integration.import_record_results,
  omp_integration.migration_anomalies,
  omp_integration.import_artifacts
FROM omp_work_app, omp_work_readonly, omp_work_importer, omp_work_backup;
GRANT SELECT, INSERT, UPDATE ON
  omp_work.repositories,
  omp_work.principals,
  omp_work.workflow_states,
  omp_work.projects,
  omp_work.project_relations,
  omp_work.labels,
  omp_work.work_item_labels
TO omp_work_app;

GRANT SELECT ON omp_control.workspaces TO omp_work_importer;

GRANT SELECT, INSERT ON
  omp_work.repositories,
  omp_work.principals,
  omp_work.workflow_states,
  omp_work.projects,
  omp_work.project_relations,
  omp_work.labels,
  omp_work.work_item_labels,
  omp_work.work_items,
  omp_work.project_health,
  omp_work.focus_slots,
  omp_integration.import_batches,
  omp_integration.import_relations
TO omp_work_importer;

GRANT UPDATE (name, url, archived) ON omp_work.repositories TO omp_work_importer;
GRANT UPDATE (name, display_name, active) ON omp_work.principals TO omp_work_importer;
GRANT UPDATE (name, state_type, position, archived) ON omp_work.workflow_states TO omp_work_importer;
GRANT UPDATE (key, name, target_date, archived) ON omp_work.projects TO omp_work_importer;
GRANT UPDATE (active) ON omp_work.project_relations TO omp_work_importer;
GRANT UPDATE (color, archived) ON omp_work.labels TO omp_work_importer;
GRANT UPDATE (active) ON omp_work.work_item_labels TO omp_work_importer;
GRANT UPDATE (state, current_revision_id, repository_id, project_id, workflow_state_id, assignee_id, priority, source_updated_at, archived, row_version) ON omp_work.work_items TO omp_work_importer;
GRANT UPDATE (health, updated_at) ON omp_work.project_health TO omp_work_importer;
GRANT UPDATE (work_id, version) ON omp_work.focus_slots TO omp_work_importer;
GRANT UPDATE (state, staged_at, reconciled_at, promoted_at, reconciliation_sha256, parity_hashes) ON omp_integration.import_batches TO omp_work_importer;
GRANT UPDATE (local_source_id, local_target_id, state, anomaly_code) ON omp_integration.import_relations TO omp_work_importer;
GRANT SELECT, INSERT ON
  omp_work.work_aliases,
  omp_work.work_revisions,
  omp_work.acceptance_criteria,
  omp_work.work_relations,
  omp_integration.external_refs,
  omp_integration.import_pages,
  omp_integration.import_page_records,
  omp_integration.import_records,
  omp_integration.import_record_results,
  omp_integration.migration_anomalies,
  omp_integration.import_artifacts
TO omp_work_importer;

GRANT SELECT ON
  omp_work.repositories,
  omp_work.principals,
  omp_work.workflow_states,
  omp_work.projects,
  omp_work.project_relations,
  omp_work.labels,
  omp_work.work_item_labels,
  omp_integration.import_batches,
  omp_integration.import_pages,
  omp_integration.import_page_records,
  omp_integration.import_records,
  omp_integration.import_relations,
  omp_integration.import_record_results,
  omp_integration.migration_anomalies,
  omp_integration.import_artifacts
TO omp_work_readonly, omp_work_backup;

REVOKE DELETE, TRUNCATE ON
  omp_work.repositories,
  omp_work.principals,
  omp_work.workflow_states,
  omp_work.projects,
  omp_work.project_relations,
  omp_work.labels,
  omp_work.work_item_labels,
  omp_integration.import_batches,
  omp_integration.import_pages,
  omp_integration.import_page_records,
  omp_integration.import_records,
  omp_integration.import_relations,
  omp_integration.import_record_results,
  omp_integration.migration_anomalies,
  omp_integration.import_artifacts
FROM omp_work_app, omp_work_readonly, omp_work_importer, omp_work_backup;
