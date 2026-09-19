-- Research component identity and campaign compatibility manifest (R02-S3b).
CREATE TABLE omp_research.components (
  workspace_id uuid NOT NULL,
  component_sha256 text NOT NULL CHECK (component_sha256 ~ '^[0-9a-f]{64}$'),
  descriptor jsonb NOT NULL CHECK (octet_length(descriptor::text) <= 65536),
  kind text GENERATED ALWAYS AS (descriptor->>'kind') STORED
       CHECK (kind IN ('worker','evaluator','policy','audit','release','environment')),
  registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, component_sha256)
);

SELECT omp_control.install_workspace_rls('omp_research.components'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.components FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.components TO omp_work_app;
GRANT SELECT ON omp_research.components TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.components FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_components BEFORE UPDATE OR DELETE ON omp_research.components FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

ALTER TABLE omp_research.campaigns
  ADD COLUMN compatibility jsonb CHECK (compatibility IS NULL OR octet_length(compatibility::text) <= 65536),
  ADD COLUMN compatibility_sha256 text CHECK (compatibility_sha256 IS NULL OR compatibility_sha256 ~ '^[0-9a-f]{64}$'),
  ADD CONSTRAINT campaigns_compatibility_pair CHECK ((compatibility IS NULL) = (compatibility_sha256 IS NULL)),
  ADD CONSTRAINT campaigns_draft_no_compatibility CHECK (state <> 'draft' OR compatibility_sha256 IS NULL);

CREATE OR REPLACE FUNCTION omp_control.enforce_research_campaign_compatibility() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;

  IF OLD.compatibility_sha256 IS NOT NULL AND (
     NEW.compatibility_sha256 IS DISTINCT FROM OLD.compatibility_sha256
     OR NEW.compatibility IS DISTINCT FROM OLD.compatibility
  ) THEN
    RAISE EXCEPTION 'campaign compatibility is immutable once set';
  END IF;

  IF OLD.compatibility_sha256 IS NULL AND NEW.compatibility_sha256 IS NOT NULL AND OLD.admitted_at IS NOT NULL THEN
    RAISE EXCEPTION 'legacy campaign cannot gain compatibility after admission';
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS enforce_research_campaign_compatibility_trigger ON omp_research.campaigns;
CREATE TRIGGER enforce_research_campaign_compatibility_trigger
  BEFORE UPDATE ON omp_research.campaigns
  FOR EACH ROW EXECUTE FUNCTION omp_control.enforce_research_campaign_compatibility();
