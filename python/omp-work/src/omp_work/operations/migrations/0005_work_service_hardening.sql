SELECT omp_control.install_workspace_rls('omp_control.workspaces'::regclass, 'workspace_id');

ALTER TABLE omp_work.work_relations
  ADD CONSTRAINT related_relations_canonical
  CHECK (kind <> 'related' OR source_work_id::text < target_work_id::text);

ALTER TABLE omp_audit.domain_events ADD COLUMN causation_id uuid;
ALTER TABLE omp_audit.domain_events ALTER COLUMN causation_id SET NOT NULL;

CREATE OR REPLACE FUNCTION omp_control.allow_closeout_completion() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE'
    OR OLD.workspace_id <> NEW.workspace_id
    OR OLD.work_id <> NEW.work_id
    OR OLD.revision_id <> NEW.revision_id
    OR OLD.candidate_id <> NEW.candidate_id
    OR OLD.requested_at <> NEW.requested_at
    OR OLD.state <> 'pending'
    OR NEW.state <> 'completed'
    OR NEW.completed_at IS NULL THEN
    RAISE EXCEPTION 'closeout intents only transition pending to completed';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER closeout_intents_state_only BEFORE UPDATE OR DELETE ON omp_evidence.closeout_intents FOR EACH ROW EXECUTE FUNCTION omp_control.allow_closeout_completion();

REVOKE ALL ON ALL TABLES IN SCHEMA omp_control, omp_work, omp_evidence, omp_audit, omp_integration FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT, UPDATE ON omp_control.workspaces, omp_control.idempotent_commands TO omp_work_app;
GRANT SELECT, INSERT, UPDATE ON omp_work.work_items, omp_work.work_relations, omp_work.focus_slots, omp_work.project_health TO omp_work_app;
GRANT SELECT, INSERT ON omp_work.work_aliases, omp_work.work_revisions, omp_work.acceptance_criteria, omp_work.candidates TO omp_work_app;
GRANT SELECT, INSERT, UPDATE ON omp_evidence.closeout_intents TO omp_work_app;
GRANT SELECT, INSERT ON omp_evidence.receipts, omp_audit.domain_events TO omp_work_app;
GRANT SELECT ON ALL TABLES IN SCHEMA omp_control, omp_work, omp_evidence, omp_audit, omp_integration TO omp_work_readonly;
GRANT INSERT ON omp_integration.external_refs TO omp_work_importer;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA omp_audit TO omp_work_app;
REVOKE DELETE, TRUNCATE ON ALL TABLES IN SCHEMA omp_control, omp_work, omp_evidence, omp_audit, omp_integration FROM omp_work_app, omp_work_readonly, omp_work_importer;
