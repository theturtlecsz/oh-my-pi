CREATE TABLE omp_integration.raw_exports (
  export_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  team_key text NOT NULL CHECK(team_key = 'HOME'),
  mode text NOT NULL CHECK(mode IN ('full','delta')),
  base_export_id uuid,
  source_started_at timestamptz NOT NULL,
  source_lower_bound timestamptz,
  source_boundary timestamptz,
  state text NOT NULL CHECK(state IN ('running','complete','blocked')),
  storage_root text NOT NULL CHECK(storage_root !~ '^/'),
  raw_export_sha256 text CHECK(raw_export_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_sha256 text CHECK(manifest_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  completed_at timestamptz,
  PRIMARY KEY (export_id),
  UNIQUE (workspace_id, export_id),
  FOREIGN KEY (workspace_id, base_export_id) REFERENCES omp_integration.raw_exports(workspace_id, export_id),
  CHECK (
    (state = 'running' AND raw_export_sha256 IS NULL AND manifest_sha256 IS NULL AND completed_at IS NULL)
    OR
    (state <> 'running' AND source_boundary IS NOT NULL AND raw_export_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL AND completed_at IS NOT NULL)
  )
);
CREATE TABLE omp_integration.extraction_cursors (
  export_id uuid NOT NULL,
  workspace_id uuid NOT NULL,
  stream text NOT NULL,
  page_index integer NOT NULL CHECK(page_index >= 0),
  request_cursor text,
  end_cursor text,
  has_next_page boolean NOT NULL,
  scanned_count integer NOT NULL CHECK(scanned_count >= 0),
  retained_count integer NOT NULL CHECK(retained_count >= 0),
  cumulative_count integer NOT NULL CHECK(cumulative_count >= 0),
  plaintext_sha256 text NOT NULL CHECK(plaintext_sha256 ~ '^[0-9a-f]{64}$'),
  ciphertext_sha256 text NOT NULL CHECK(ciphertext_sha256 ~ '^[0-9a-f]{64}$'),
  artifact_path text NOT NULL CHECK(artifact_path !~ '^/'),
  variables_sha256 text NOT NULL CHECK(variables_sha256 ~ '^[0-9a-f]{64}$'),
  committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (export_id, stream, page_index),
  FOREIGN KEY (workspace_id, export_id) REFERENCES omp_integration.raw_exports(workspace_id, export_id)
);
SELECT omp_control.install_workspace_rls('omp_integration.raw_exports'::regclass, 'workspace_id');
SELECT omp_control.install_workspace_rls('omp_integration.extraction_cursors'::regclass, 'workspace_id');
CREATE TRIGGER immutable_extraction_cursors BEFORE UPDATE OR DELETE ON omp_integration.extraction_cursors FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
CREATE OR REPLACE FUNCTION omp_integration.advance_raw_export() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'raw export is immutable';
  END IF;
  IF OLD.workspace_id <> NEW.workspace_id OR OLD.export_id <> NEW.export_id OR OLD.team_key <> NEW.team_key OR OLD.mode <> NEW.mode OR OLD.base_export_id IS DISTINCT FROM NEW.base_export_id OR OLD.source_started_at <> NEW.source_started_at OR OLD.source_lower_bound IS DISTINCT FROM NEW.source_lower_bound OR OLD.storage_root <> NEW.storage_root OR OLD.state <> 'running' THEN
    RAISE EXCEPTION 'raw export is immutable except boundary/finalization';
  END IF;
  IF NEW.state = 'running' AND OLD.source_boundary IS NULL AND NEW.source_boundary IS NOT NULL AND NEW.raw_export_sha256 IS NULL AND NEW.manifest_sha256 IS NULL AND NEW.completed_at IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.state IN ('complete','blocked') AND OLD.source_boundary = NEW.source_boundary AND OLD.raw_export_sha256 IS NULL AND OLD.manifest_sha256 IS NULL AND OLD.completed_at IS NULL AND NEW.source_boundary IS NOT NULL AND NEW.raw_export_sha256 IS NOT NULL AND NEW.manifest_sha256 IS NOT NULL AND NEW.completed_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'raw export is immutable except boundary/finalization';
END $$;
CREATE TRIGGER raw_exports_final_only BEFORE UPDATE OR DELETE ON omp_integration.raw_exports FOR EACH ROW EXECUTE FUNCTION omp_integration.advance_raw_export();
REVOKE ALL ON omp_integration.raw_exports, omp_integration.extraction_cursors FROM omp_work_app, omp_work_readonly, omp_work_importer, omp_work_backup;
GRANT SELECT, INSERT ON omp_integration.raw_exports, omp_integration.extraction_cursors TO omp_work_importer;
GRANT UPDATE (source_boundary, state, raw_export_sha256, manifest_sha256, completed_at) ON omp_integration.raw_exports TO omp_work_importer;
GRANT SELECT ON omp_integration.raw_exports, omp_integration.extraction_cursors TO omp_work_readonly, omp_work_backup;
