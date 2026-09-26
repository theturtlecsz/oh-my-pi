# 0011 — Research artifact and source custody (R04)

## Problem

1. Research records reference artifact content by bare SHA-256 hashes, but a hash alone only establishes a claim of identity, not byte custody, availability, or authorized retrieval.
2. Backup archives encrypt domain-specific exports. They do not give research material workspace-scoped, content-addressed byte verification.
3. Source versions, dataset snapshots, collection paths, retention, and cache hits were unrecorded, so a cached copy could be presented as a new replicate and an escaped path or archive member could be read as research input.

## Decision

1. **Content-addressed custody is verified byte installation.** A row in `omp_research.artifacts` exists only after the service size-checks, digest-verifies, and installs the exact bytes. The path is derived from `(workspace_id, artifact_sha256)`. Files are mode 0400. Corrupted bytes refuse registration and read with `artifact_unavailable` and are never overwritten. Missing bytes may be restored by an identical re-registration.
2. **Inline ceiling, no second encryption layer.** Registration accepts inline base64 up to 4 MiB (`RESEARCH_ARTIFACT_MAX_BYTES`) or one contained collection read under that same ceiling. Recomputed byte digest, size, and `manifest_sha256 = sha256(canonical_json(manifest))` must match. Research bytes stay plaintext so the installed file hash is the artifact hash. Streaming upload and encryption at rest are not added; backup archives keep the existing gpg path, and backup verification re-hashes the installed plaintext.
3. **Sources and datasets.** `register_research_source` records version, location, status (`ok` or `inaccessible`), retention, and ACL. `register_research_dataset` records a snapshot digest bound to a source and, when bytes are held, to an installed artifact. Locations are contained relative paths or `source://name@version`. Retrieval checks project ACL and retention before reading bytes. An inaccessible source stays marked and is not returned.
4. **Collection containment.** `collect_research_artifact` reads only a relative path inside the workspace collection root. `..`, absolute paths, and symlinks are refused. A zip whose member names escape is refused and not extracted.
5. **Cache is not a replicate.** `record_research_cache` records a hit. `claim_research_replicate` refuses when that digest is already cached. `bind_research_receipt_manifest` binds one receipt to one artifact manifest; a second manifest for the same receipt conflicts.
6. **Reads fail closed.** `GET` research-artifact, source, and dataset routes require `work.read`. Expired `valid_until` or `retention_until` returns `stale_evidence` and keeps the bytes. Hash or length mismatch and missing files return `artifact_unavailable`. Custody confers no work, execution, admission, or acceptance authority. Deletion remains unassigned.

## Consequences

- Migration `0028_research_artifact_custody.sql` adds the artifact, source, dataset, cache, replicate-claim, and receipt-manifest tables with RLS and immutability triggers.
- Contract commands `register_research_artifact`, `register_research_source`, `register_research_dataset`, `collect_research_artifact`, `record_research_cache`, `claim_research_replicate`, and `bind_research_receipt_manifest` are added, plus the artifact, source, and dataset reads.
- `ResearchView` resolves referenced artifact digests that this workspace actually holds. Unregistered hashes stay absent.
