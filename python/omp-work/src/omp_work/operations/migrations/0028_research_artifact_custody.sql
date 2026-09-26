-- Research artifact custody (R04): a row exists only for bytes the service verified and installed.
-- Sources, datasets, cache hits, replicate claims, and receipt bindings reference those bytes.
CREATE TABLE omp_research.artifacts (
  workspace_id uuid NOT NULL,
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL CHECK (octet_length(manifest::text) <= 65536),
  registered_by uuid NOT NULL DEFAULT omp_control.current_actor_id(),
  registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, artifact_sha256),
  UNIQUE (workspace_id, manifest_sha256),
  CHECK (manifest->>'artifact_sha256' = artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.artifacts'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.artifacts FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.artifacts TO omp_work_app;
GRANT SELECT ON omp_research.artifacts TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.artifacts FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_artifacts BEFORE UPDATE OR DELETE ON omp_research.artifacts FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.sources (
  workspace_id uuid NOT NULL,
  source_id uuid NOT NULL,
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL CHECK (octet_length(manifest::text) <= 65536),
  status text NOT NULL CHECK (status IN ('ok', 'inaccessible')),
  artifact_sha256 text CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
  registered_by uuid NOT NULL DEFAULT omp_control.current_actor_id(),
  registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, source_id),
  UNIQUE (workspace_id, manifest_sha256),
  CHECK ((manifest->>'source_id') = source_id::text),
  CHECK (status = manifest->>'status'),
  CHECK (artifact_sha256 IS NULL OR artifact_sha256 = manifest->>'artifact_sha256'),
  FOREIGN KEY (workspace_id, artifact_sha256) REFERENCES omp_research.artifacts (workspace_id, artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.sources'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.sources FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.sources TO omp_work_app;
GRANT SELECT ON omp_research.sources TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.sources FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_sources BEFORE UPDATE OR DELETE ON omp_research.sources FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.datasets (
  workspace_id uuid NOT NULL,
  dataset_id uuid NOT NULL,
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  manifest jsonb NOT NULL CHECK (octet_length(manifest::text) <= 65536),
  source_id uuid NOT NULL,
  artifact_sha256 text CHECK (artifact_sha256 IS NULL OR artifact_sha256 ~ '^[0-9a-f]{64}$'),
  registered_by uuid NOT NULL DEFAULT omp_control.current_actor_id(),
  registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, dataset_id),
  UNIQUE (workspace_id, manifest_sha256),
  CHECK ((manifest->>'dataset_id') = dataset_id::text),
  CHECK ((manifest->>'source_id') = source_id::text),
  CHECK (artifact_sha256 IS NULL OR artifact_sha256 = manifest->>'artifact_sha256'),
  FOREIGN KEY (workspace_id, source_id) REFERENCES omp_research.sources (workspace_id, source_id),
  FOREIGN KEY (workspace_id, artifact_sha256) REFERENCES omp_research.artifacts (workspace_id, artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.datasets'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.datasets FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.datasets TO omp_work_app;
GRANT SELECT ON omp_research.datasets TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.datasets FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_datasets BEFORE UPDATE OR DELETE ON omp_research.datasets FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.artifact_cache (
  workspace_id uuid NOT NULL,
  cache_key text NOT NULL CHECK (char_length(cache_key) BETWEEN 1 AND 256),
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, cache_key),
  FOREIGN KEY (workspace_id, artifact_sha256) REFERENCES omp_research.artifacts (workspace_id, artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.artifact_cache'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.artifact_cache FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.artifact_cache TO omp_work_app;
GRANT SELECT ON omp_research.artifact_cache TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.artifact_cache FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_artifact_cache BEFORE UPDATE OR DELETE ON omp_research.artifact_cache FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.replicate_claims (
  workspace_id uuid NOT NULL,
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  claimed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, artifact_sha256),
  FOREIGN KEY (workspace_id, artifact_sha256) REFERENCES omp_research.artifacts (workspace_id, artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.replicate_claims'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.replicate_claims FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.replicate_claims TO omp_work_app;
GRANT SELECT ON omp_research.replicate_claims TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.replicate_claims FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_replicate_claims BEFORE UPDATE OR DELETE ON omp_research.replicate_claims FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();

CREATE TABLE omp_research.receipt_manifests (
  workspace_id uuid NOT NULL,
  receipt_id uuid NOT NULL,
  artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
  manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  bound_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (workspace_id, receipt_id),
  FOREIGN KEY (workspace_id, artifact_sha256) REFERENCES omp_research.artifacts (workspace_id, artifact_sha256)
);
SELECT omp_control.install_workspace_rls('omp_research.receipt_manifests'::regclass, 'workspace_id');
REVOKE ALL ON omp_research.receipt_manifests FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_research.receipt_manifests TO omp_work_app;
GRANT SELECT ON omp_research.receipt_manifests TO omp_work_readonly, omp_work_backup;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_research.receipt_manifests FROM omp_work_app, omp_work_readonly, omp_work_importer;
CREATE TRIGGER immutable_research_receipt_manifests BEFORE UPDATE OR DELETE ON omp_research.receipt_manifests FOR EACH ROW EXECUTE FUNCTION omp_control.reject_immutable();
