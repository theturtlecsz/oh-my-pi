-- Bind a finalized native candidate to the exact retained source analysis.
-- Commit, tree, source-file manifest, and snapshot publication manifest are
-- separate identities; none is inferred from another.
CREATE TABLE omp_work.candidate_source_versions (
  candidate_id uuid PRIMARY KEY,
  workspace_id uuid NOT NULL,
  work_id uuid NOT NULL,
  revision_id uuid NOT NULL,
  repository_id uuid NOT NULL,
  source_version_id text NOT NULL,
  snapshot_id text NOT NULL CHECK (snapshot_id ~ '^(?:sha256:)?[0-9a-f]{64}$'),
  base_commit text NOT NULL CHECK (base_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  analyzed_commit text CHECK (analyzed_commit IS NULL OR analyzed_commit ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  tree_sha text CHECK (tree_sha IS NULL OR tree_sha ~ '^(?:[0-9a-f]{40}|[0-9a-f]{64})$'),
  source_manifest_sha256 text NOT NULL CHECK (source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  snapshot_manifest_sha256 text NOT NULL CHECK (snapshot_manifest_sha256 ~ '^[0-9a-f]{64}$'),
  content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  association_sha256 text NOT NULL CHECK (association_sha256 ~ '^[0-9a-f]{64}$'),
  producer text NOT NULL,
  producer_receipt_sha256 text NOT NULL CHECK (producer_receipt_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  FOREIGN KEY (workspace_id, candidate_id) REFERENCES omp_work.candidates(workspace_id, candidate_id),
  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id),
  FOREIGN KEY (workspace_id, revision_id) REFERENCES omp_work.work_revisions(workspace_id, revision_id),
  UNIQUE (workspace_id, source_version_id, snapshot_id)
);

SELECT omp_control.install_workspace_rls('omp_work.candidate_source_versions'::regclass, 'workspace_id');
REVOKE ALL ON omp_work.candidate_source_versions FROM omp_work_app, omp_work_readonly, omp_work_importer;
GRANT SELECT, INSERT ON omp_work.candidate_source_versions TO omp_work_app;
GRANT SELECT ON omp_work.candidate_source_versions TO omp_work_readonly;
REVOKE UPDATE, DELETE, TRUNCATE ON omp_work.candidate_source_versions FROM omp_work_app, omp_work_readonly, omp_work_importer;
