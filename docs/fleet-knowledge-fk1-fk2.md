# Fleet knowledge FK-1 + FK-2 closeout (OMP-279)

OMP-279 delivered the FK-1 read surface and the FK-2 knowledge-source
foundation for the Work Ledger. This document is the closeout record: it
describes what shipped, how it behaves, and where the acceptance boundary
sits. It is **not** a release claim.

## FK-1: the four exact reads

The FK-1 reads are the last four entries of `_READS` in
`python/omp-work/src/omp_work/__init__.py`. `tests/test_fk_read_contract.py`
pins the declared reads to the GET routes the service actually registers and
to `contract.json`, so the set cannot drift silently.

| Route | Scope | Notes |
| --- | --- | --- |
| `GET /v1/work-items/{key}/revisions/{selector}` | `work.read` | Selector is a revision number or revision id. |
| `GET /v1/receipts/{receipt_id}` | `work.read` | Exact receipt projection. |
| `GET /v1/workspaces/{workspace_id}/work-items` | `work.read` | Keyset enumeration (below). |
| `GET /v1/workspaces/{workspace_id}/events` | `work.read` | Domain event cursor (below). |

All four require the caller's principal to include the target workspace.
The first three accept any principal holding `work.read`. The events read is
stricter: it additionally refuses any principal with a non-`None`
`candidate_ids` allowlist (i.e. candidate-scoped readers) with
`403 forbidden`, because a candidate-bounded reader holds no workspace-wide
view. Enforcement lives in `v1/service.py`; behavior is covered by
`tests/test_exact_reads.py` and
`tests/test_domain_event_cursor.py::test_empty_page_and_permissions`.

## Domain event cursor and commit watermark

`GET /v1/workspaces/{workspace_id}/events` pages `omp_audit.domain_events` by
`sequence` under a per-workspace commit watermark (`v1/store.py`).

- Writers take `pg_advisory_xact_lock(hashtextextended('omp_audit.domain_events:' || workspace_id, 0))`
  before appending, so sequence assignment is serialized per workspace.
- The read computes `watermark_sequence = COALESCE(MAX(sequence), 0)` for the
  workspace and returns only rows with `after < sequence <= watermark_sequence`.
  Events written by an in-flight transaction are therefore invisible: the
  reader never observes a gap and never blocks the writer.
- The page is fetched with `LIMIT limit + 1`; the extra row is a probe for
  `has_more`.
- Response fields: `watermark_sequence` (the committed high-water mark),
  `next_after_sequence` (the last returned sequence, or the input `after` when
  the page is empty), and `has_more`.
- `limit` is validated to 1–500; `after` must be non-negative.

`tests/test_domain_event_cursor.py::test_regression_uncommitted_lock_reader_watermark_and_paging`
proves the semantics end to end: a raw uncommitted sequence N is not returned,
the service command is observed waiting on `wait_event_type='Lock'`, the page
ends before N, and after commit paging returns N followed by the command event.

## Work-item keyset order

`GET /v1/workspaces/{workspace_id}/work-items` enumerates work items with a
stable keyset cursor ordered by `(created_at, work_id)` (`v1/store.py`):

- The predicate is `(i.created_at, i.work_id) > (after_created_at, after_work_id)`
  with `ORDER BY i.created_at, i.work_id LIMIT limit + 1`.
- The cursor is paired: `after_created_at` and `after_work_id` must be supplied
  together. The server (`v1/server.py`) rejects a half pair with
  `400 invalid_request`.
- `next_created_at` / `next_work_id` are set only when a further page exists,
  and are `null` otherwise.
- `limit` is validated to 1–500. The keyset order is stable under concurrent
  inserts and pages past the 1,000-item `/tree` cap.

## FK-2: `knowledge_contracts.py`

`python/omp-work/src/omp_work/knowledge_contracts.py` defines the strict
knowledge models: `RepositoryIdentity`, `SourceRef`, `ManifestFile`,
`CodeSnapshotManifest`, `EnolaFact`, `IngestionJob`, `IngestionReceipt`,
`KnowledgeQuery`, `KnowledgeHit`, and `ProviderRoute`.

Two canonical hash preimages are defined:

- **Code snapshot** — `CODE_SNAPSHOT_ALGORITHM = "work.omp.dev/v1/code-snapshot"`.
  `CodeSnapshotManifest.snapshot_id()` hashes
  `{algorithm, base_commit, files: [{path, kind, sha256, size}] sorted by UTF-8
  path bytes, repo, ws}`.
- **Child fact** — `CHILD_FACT_ALGORITHM = "work.omp.dev/v1/child-fact"`.
  `EnolaFact.child_fact_id(snap)` hashes
  `{algorithm, fact_id, file, snap}`.

Golden vectors live in `fixtures/knowledge_vectors.json` and are exercised by
`tests/test_knowledge_contracts.py`. The TypeScript mirror
(`codeSnapshotId` / `childFactId`) lives in `packages/work-client/src/index.ts`.

## FK-2: `knowledge_source.py`

`python/omp-work/src/omp_work/knowledge_source.py` resolves a local checkout to
an existing repository row and captures its working-tree bytes. Both operations
are **read-only**.

Identity resolution (`resolve_repository_identity`):

1. Verify the checkout root is the git toplevel (`rev-parse --show-toplevel`).
2. Read `origin` and normalize it (`normalize_remote_url`): SCP-style
   `git@host:path` becomes `ssh://git@host/path`, scheme and host are
   lowercased, ports 22/443 are dropped, and trailing `/` and `.git` are
   stripped.
3. Collect root commits with `rev-list --max-parents=0 HEAD` and verify each
   with `cat-file -e <sha>^{commit}`.
4. Match against non-archived `omp_work.repositories` rows in the workspace by
   normalized URL. Zero matches raise `repository_unresolved`; more than one
   raises `repository_ambiguous`.

Manifest capture (`capture_manifest`):

- `base_commit` is `HEAD`.
- Base files come from `ls-tree -r -z HEAD`; blob bytes are read with
  `cat-file --batch` and recorded as `kind="base"` with sha256 and size.
- The working-tree overlay comes from
  `status --porcelain=v1 -z --untracked-files=all`: modified and untracked
  files are hashed from their bytes, deleted files are recorded with
  `sha256=null` and `size=null`, and a rename marks the original path
  `deleted`. Ignored files (`!!`) are excluded.

## Acceptance boundary

- Acceptance for OMP-279 is **source delivery plus candidate qualification
  only**.
- The OMP-279 owner approval was committed in s06/s07 at digest
  `46b6ecc8cde78f2f0832d03c5fb9ae36f49150b0c8ec5a109e1b41a725e6d268`
  (commit `4e7cc237e8`), since superseded by the live OMP-283 re-approval
  (`contracts/v1/approval.json` records issue `OMP-283` with digest
  `cb9dde61ebdca590e478e06cff2cac033ef9f4c736b8a37578a4918ad42d334d`).
- Deploying the contract, restarting the service, and live activation are
  **owner mutation-window actions** and were not performed here.
- FK-3 (omp-knowledge, Cognee, A/B snapshot isolation) is moved to **OMP-288**.
- No FK release claim is made by this closeout.
