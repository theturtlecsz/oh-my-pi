-- NSI-2: immutable, revision-qualified intake records; only the head advances.
-- TypedValue mirrors NSI-1's closed {type, value} union, including EntityRef.
CREATE FUNCTION omp_work.intake_typed_value_ok(input jsonb) RETURNS boolean
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog AS $$
BEGIN
  IF input IS NULL OR jsonb_typeof(input) <> 'object' THEN RETURN false; END IF;
  IF NOT (input ?& ARRAY['type', 'value']) OR input - ARRAY['type', 'value'] <> '{}'::jsonb
    THEN RETURN false; END IF;
  CASE input->>'type'
    WHEN 'string' THEN RETURN jsonb_typeof(input->'value') = 'string';
    WHEN 'integer' THEN
      IF jsonb_typeof(input->'value') <> 'number' THEN RETURN false; END IF;
      RETURN (input->>'value')::numeric = trunc((input->>'value')::numeric);
    WHEN 'number' THEN RETURN jsonb_typeof(input->'value') = 'number';
    WHEN 'boolean' THEN RETURN jsonb_typeof(input->'value') = 'boolean';
    WHEN 'null' THEN RETURN jsonb_typeof(input->'value') = 'null';
    WHEN 'entity' THEN
      RETURN COALESCE(jsonb_typeof(input->'value') = 'object'
        AND (input->'value') ?& ARRAY['id', 'type']
        AND (input->'value') - ARRAY['id', 'type'] = '{}'::jsonb
        AND jsonb_typeof(input->'value'->'id') = 'string'
        AND jsonb_typeof(input->'value'->'type') = 'string'
        AND length(input->'value'->>'id') > 0
        AND length(input->'value'->>'type') > 0, false);
    ELSE RETURN false;
  END CASE;
END $$;

CREATE TABLE omp_work.intake_heads (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL CHECK (run_id <> ''),
  mission_id uuid NOT NULL,
  current_revision integer NOT NULL CHECK (current_revision >= 0),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id),
  FOREIGN KEY (workspace_id, mission_id) REFERENCES omp_work.project_missions(workspace_id, mission_id)
);

CREATE TABLE omp_work.intake_revisions (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL CHECK (revision >= 0),
  previous_revision integer GENERATED ALWAYS AS (CASE WHEN revision > 0 THEN revision - 1 END) STORED,
  state text NOT NULL CHECK (state IN ('collecting', 'reasoning', 'needs_spec', 'conflicted',
    'ready_for_ratification', 'ratified', 'published', 'stale', 'invalid', 'superseded')),
  source_hash text NOT NULL CHECK (source_hash ~ '^[a-f0-9]{64}$'),
  claim_set_hash text NOT NULL CHECK (claim_set_hash ~ '^[a-f0-9]{64}$'),
  rule_bundle_hash text NOT NULL CHECK (rule_bundle_hash ~ '^[a-f0-9]{64}$'),
  schema_hash text NOT NULL CHECK (schema_hash ~ '^[a-f0-9]{64}$'),
  contract_hash text NOT NULL CHECK (contract_hash ~ '^[a-f0-9]{64}$'),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id) REFERENCES omp_work.intake_heads(workspace_id, run_id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY (workspace_id, run_id, previous_revision)
    REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);
ALTER TABLE omp_work.intake_heads ADD FOREIGN KEY (workspace_id, run_id, current_revision)
  REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision) DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION omp_work.advance_intake_head() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'intake heads cannot be deleted'; END IF;
  IF TG_OP = 'INSERT' THEN
    IF NEW.current_revision <> 0 THEN RAISE EXCEPTION 'intake starts at revision 0'; END IF;
  ELSIF NEW.current_revision <> OLD.current_revision + 1
    OR (to_jsonb(NEW) - 'current_revision') IS DISTINCT FROM (to_jsonb(OLD) - 'current_revision') THEN
    RAISE EXCEPTION 'only intake head revision may advance by one';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER advance_intake_head BEFORE INSERT OR UPDATE OR DELETE ON omp_work.intake_heads
  FOR EACH ROW EXECUTE FUNCTION omp_work.advance_intake_head();

CREATE TABLE omp_work.intake_artifacts (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  artifact_id text NOT NULL,
  bytes bytea NOT NULL,
  uri text NOT NULL,
  media_type text NOT NULL,
  content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, artifact_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

CREATE TABLE omp_work.intake_ontology (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  definition_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('predicate', 'dimension')),
  title text NOT NULL,
  subject_types text[] NOT NULL DEFAULT '{}',
  object_types text[] NOT NULL DEFAULT '{}',
  cardinality text CHECK (cardinality IN ('one', 'many')),
  exclusivity boolean,
  transitivity boolean,
  authority text[] NOT NULL DEFAULT '{}',
  category text,
  required_premises text[] NOT NULL DEFAULT '{}',
  materiality text CHECK (materiality IN ('critical', 'material', 'minor')),
  waiver_policy text CHECK (waiver_policy IN ('never', 'owner_with_reason')),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, definition_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

CREATE TABLE omp_work.intake_entities (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  entity_id text NOT NULL CHECK (entity_id <> ''),
  entity_type text NOT NULL CHECK (entity_type <> ''),
  UNIQUE (workspace_id, run_id, revision, entity_id, entity_type),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, entity_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

CREATE TABLE omp_work.intake_claims (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  claim_id text NOT NULL,
  category text NOT NULL,
  subject_entity_id text NOT NULL,
  predicate_id text NOT NULL,
  value jsonb NOT NULL CHECK (omp_work.intake_typed_value_ok(value)),
  value_entity_id text,
  value_entity_type text,
  CHECK (CASE WHEN value->>'type' = 'entity' THEN
    value_entity_id IS NOT NULL AND value_entity_type IS NOT NULL
    AND value_entity_id = value->'value'->>'id'
    AND value_entity_type = value->'value'->>'type'
    ELSE value_entity_id IS NULL AND value_entity_type IS NULL END),
  origin text NOT NULL CHECK (origin IN ('owner_statement', 'policy_rule', 'repository_observation', 'tool_receipt', 'model_proposal', 'rule_derivation')),
  state text NOT NULL CHECK (state IN ('proposed', 'admitted', 'rejected', 'superseded')),
  modality text NOT NULL CHECK (modality IN ('observed', 'desired', 'required', 'prohibited', 'possible', 'unknown')),
  polarity text NOT NULL CHECK (polarity IN ('positive', 'negative')),
  truth text NOT NULL CHECK (truth IN ('true', 'false', 'unknown')),
  asserted_by text NOT NULL,
  admitted_by text,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, subject_entity_id) REFERENCES omp_work.intake_entities(workspace_id, run_id, revision, entity_id),
  FOREIGN KEY (workspace_id, run_id, revision, predicate_id) REFERENCES omp_work.intake_ontology(workspace_id, run_id, revision, definition_id),
  FOREIGN KEY (workspace_id, run_id, revision, value_entity_id, value_entity_type) REFERENCES omp_work.intake_entities(workspace_id, run_id, revision, entity_id, entity_type)
);

CREATE TABLE omp_work.intake_claim_evidence (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  claim_id text NOT NULL,
  artifact_id text NOT NULL,
  start_byte integer NOT NULL CHECK (start_byte >= 0),
  end_byte integer NOT NULL CHECK (end_byte >= start_byte),
  quote text,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, claim_id, artifact_id, start_byte, end_byte),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision, artifact_id) REFERENCES omp_work.intake_artifacts(workspace_id, run_id, revision, artifact_id)
);

CREATE TABLE omp_work.intake_claim_premises (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  claim_id text NOT NULL,
  premise_id text NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, claim_id, premise_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision, premise_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

CREATE TABLE omp_work.intake_claim_supersessions (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  claim_id text NOT NULL,
  superseded_claim_id text NOT NULL,
  CHECK (claim_id <> superseded_claim_id),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, claim_id, superseded_claim_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision, superseded_claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

CREATE TABLE omp_work.intake_issues (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  issue_id text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('conflict', 'gap', 'question')),
  reason text NOT NULL,
  category text,
  predicate_id text,
  dimension_id text,
  premise_id text,
  question text,
  materiality text CHECK (materiality IN ('critical', 'material', 'minor')),
  waiver_policy text CHECK (waiver_policy IN ('never', 'owner_with_reason')),
  CHECK (materiality IS DISTINCT FROM 'critical' OR waiver_policy = 'never'),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, issue_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, predicate_id) REFERENCES omp_work.intake_ontology(workspace_id, run_id, revision, definition_id),
  FOREIGN KEY (workspace_id, run_id, revision, dimension_id) REFERENCES omp_work.intake_ontology(workspace_id, run_id, revision, definition_id),
  FOREIGN KEY (workspace_id, run_id, revision, premise_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

CREATE TABLE omp_work.intake_issue_claims (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  issue_id text NOT NULL,
  claim_id text NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, issue_id, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, issue_id) REFERENCES omp_work.intake_issues(workspace_id, run_id, revision, issue_id),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

CREATE TABLE omp_work.intake_question_issues (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  question_id text NOT NULL,
  issue_id text NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, question_id, issue_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, question_id) REFERENCES omp_work.intake_issues(workspace_id, run_id, revision, issue_id),
  FOREIGN KEY (workspace_id, run_id, revision, issue_id) REFERENCES omp_work.intake_issues(workspace_id, run_id, revision, issue_id)
);

CREATE TABLE omp_work.intake_owner_decisions (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  decision_id text NOT NULL,
  question_id text NOT NULL,
  owner_id text NOT NULL,
  reason text NOT NULL,
  created_at timestamptz NOT NULL,
  answer jsonb NOT NULL CHECK (omp_work.intake_typed_value_ok(answer)),
  value_entity_id text,
  value_entity_type text,
  CHECK (CASE WHEN answer->>'type' = 'entity' THEN
    value_entity_id IS NOT NULL AND value_entity_type IS NOT NULL
    AND value_entity_id = answer->'value'->>'id'
    AND value_entity_type = answer->'value'->>'type'
    ELSE value_entity_id IS NULL AND value_entity_type IS NULL END),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, decision_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, question_id) REFERENCES omp_work.intake_issues(workspace_id, run_id, revision, issue_id),
  FOREIGN KEY (workspace_id, run_id, revision, value_entity_id, value_entity_type) REFERENCES omp_work.intake_entities(workspace_id, run_id, revision, entity_id, entity_type)
);

CREATE TABLE omp_work.intake_settled_claims (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  decision_id text NOT NULL,
  claim_id text NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, decision_id, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, decision_id) REFERENCES omp_work.intake_owner_decisions(workspace_id, run_id, revision, decision_id),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

CREATE TABLE omp_work.intake_instruction_provenances (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  provenance_id text NOT NULL,
  decision_id text NOT NULL,
  claim_id text NOT NULL,
  artifact_id text NOT NULL,
  instruction text NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, provenance_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, decision_id) REFERENCES omp_work.intake_owner_decisions(workspace_id, run_id, revision, decision_id),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id),
  FOREIGN KEY (workspace_id, run_id, revision, artifact_id) REFERENCES omp_work.intake_artifacts(workspace_id, run_id, revision, artifact_id)
);

CREATE TABLE omp_work.intake_criteria (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  criterion_id text NOT NULL,
  description text NOT NULL,
  verification_method text NOT NULL,
  requirement_ids text[] NOT NULL CHECK (cardinality(requirement_ids) > 0),
  expected_result jsonb NOT NULL CHECK (omp_work.intake_typed_value_ok(expected_result)),
  value_entity_id text,
  value_entity_type text,
  CHECK (CASE WHEN expected_result->>'type' = 'entity' THEN
    value_entity_id IS NOT NULL AND value_entity_type IS NOT NULL
    AND value_entity_id = expected_result->'value'->>'id'
    AND value_entity_type = expected_result->'value'->>'type'
    ELSE value_entity_id IS NULL AND value_entity_type IS NULL END),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, criterion_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, value_entity_id, value_entity_type) REFERENCES omp_work.intake_entities(workspace_id, run_id, revision, entity_id, entity_type)
);

CREATE TABLE omp_work.intake_assessments (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  assessment_id text NOT NULL,
  assessed_revision integer NOT NULL,
  status text NOT NULL CHECK (status IN ('needs_spec', 'conflicted', 'ready_for_ratification', 'invalid', 'stale')),
  assessed_by text NOT NULL,
  created_at timestamptz NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, assessment_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, assessed_revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

CREATE TABLE omp_work.intake_derivations (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  derivation_id text NOT NULL,
  rule_id text NOT NULL,
  derived_by text NOT NULL,
  created_at timestamptz NOT NULL,
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, derivation_id),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision)
);

CREATE TABLE omp_work.intake_derivation_claims (
  workspace_id uuid NOT NULL,
  run_id text NOT NULL,
  revision integer NOT NULL,
  derivation_id text NOT NULL,
  claim_id text NOT NULL,
  role text NOT NULL CHECK (role IN ('premise', 'conclusion')),
  operation_id uuid NOT NULL,
  nsi_version text NOT NULL CHECK (nsi_version = 'nsi/v1'),
  PRIMARY KEY (workspace_id, run_id, revision, derivation_id, claim_id, role),
  FOREIGN KEY (workspace_id, run_id, revision) REFERENCES omp_work.intake_revisions(workspace_id, run_id, revision),
  FOREIGN KEY (workspace_id, run_id, revision, derivation_id) REFERENCES omp_work.intake_derivations(workspace_id, run_id, revision, derivation_id),
  FOREIGN KEY (workspace_id, run_id, revision, claim_id) REFERENCES omp_work.intake_claims(workspace_id, run_id, revision, claim_id)
);

-- Every record and link uses the same workspace isolation and append-only grants.
DO $$
DECLARE name text;
BEGIN
  FOREACH name IN ARRAY ARRAY[
    'intake_heads',
    'intake_revisions',
    'intake_artifacts',
    'intake_ontology',
    'intake_entities',
    'intake_claims',
    'intake_claim_evidence',
    'intake_claim_premises',
    'intake_claim_supersessions',
    'intake_issues',
    'intake_issue_claims',
    'intake_question_issues',
    'intake_owner_decisions',
    'intake_settled_claims',
    'intake_instruction_provenances',
    'intake_criteria',
    'intake_assessments',
    'intake_derivations',
    'intake_derivation_claims'] LOOP
    PERFORM omp_control.install_workspace_rls(format('omp_work.%I', name)::regclass, 'workspace_id');
    EXECUTE format('REVOKE ALL ON omp_work.%I FROM PUBLIC, omp_work_app, omp_work_readonly, omp_work_importer', name);
    EXECUTE format('GRANT SELECT, INSERT ON omp_work.%I TO omp_work_app', name);
    EXECUTE format('GRANT SELECT ON omp_work.%I TO omp_work_readonly, omp_work_backup', name);
    IF name <> 'intake_heads' THEN
      EXECUTE format('CREATE TRIGGER immutable_intake_record BEFORE UPDATE OR DELETE ON omp_work.%I FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable()', name);
    END IF;
  END LOOP;
END $$;
GRANT UPDATE (current_revision) ON omp_work.intake_heads TO omp_work_app;
