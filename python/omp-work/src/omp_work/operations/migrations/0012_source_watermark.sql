ALTER TABLE omp_integration.raw_exports
  ADD COLUMN source_watermark timestamptz;

DROP TRIGGER raw_exports_final_only ON omp_integration.raw_exports;
CREATE OR REPLACE FUNCTION omp_integration.advance_raw_export() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'raw export is immutable';
  END IF;
  IF OLD.workspace_id <> NEW.workspace_id OR OLD.export_id <> NEW.export_id OR OLD.team_key <> NEW.team_key OR OLD.mode <> NEW.mode OR OLD.base_export_id IS DISTINCT FROM NEW.base_export_id OR OLD.source_started_at <> NEW.source_started_at OR OLD.source_lower_bound IS DISTINCT FROM NEW.source_lower_bound OR OLD.storage_root <> NEW.storage_root OR OLD.state <> 'running' OR OLD.source_watermark IS NOT NULL THEN
    RAISE EXCEPTION 'raw export is immutable except boundary/finalization';
  END IF;
  IF NEW.state = 'running' AND OLD.source_boundary IS NULL AND NEW.source_boundary IS NOT NULL AND NEW.source_watermark IS NULL AND NEW.raw_export_sha256 IS NULL AND NEW.manifest_sha256 IS NULL AND NEW.completed_at IS NULL THEN
    RETURN NEW;
  END IF;
  IF NEW.state IN ('complete','blocked') AND OLD.source_boundary = NEW.source_boundary AND OLD.raw_export_sha256 IS NULL AND OLD.manifest_sha256 IS NULL AND OLD.completed_at IS NULL AND NEW.source_boundary IS NOT NULL AND (NEW.source_watermark IS NULL OR NEW.source_watermark <= NEW.source_boundary) AND NEW.raw_export_sha256 IS NOT NULL AND NEW.manifest_sha256 IS NOT NULL AND NEW.completed_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION 'raw export is immutable except boundary/finalization';
END $$;
CREATE TRIGGER raw_exports_final_only BEFORE UPDATE OR DELETE ON omp_integration.raw_exports FOR EACH ROW EXECUTE FUNCTION omp_integration.advance_raw_export();
GRANT UPDATE (source_boundary, source_watermark, state, raw_export_sha256, manifest_sha256, completed_at) ON omp_integration.raw_exports TO omp_work_importer;
