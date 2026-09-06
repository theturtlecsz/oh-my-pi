# 0004 — HOME-147 pre-cutover workflow amendment

Status: owner-approved 2026-08-16 under HOME-147. Public version stays
`work.omp.dev/v1`; the contract is not authoritative until HOME-148 activates
the WorkService backend. No external v1 consumers exist, so the amendment
leaves no compatibility shim.

Changes:

1. `create_work_batch` takes rich items (`client_ref`, title, description,
   scope, acceptance criteria, state, optional project) plus same-request
   relations; the batch is one serializable transaction and any invalid item,
   relation, alias, cycle, or project reference rolls back everything.
2. `EvidenceReceipt.payload` carries the canonical caller payload body (model
   validated to 1 MiB, matching the existing database check). The store
   recomputes `payload_sha256` from the body and rejects mismatches as
   `invalid_request`. Receipt metadata (issuer, verdict, independence,
   candidate binding, remote push binding) moves to dedicated columns added by
   migration 0008; the `payload` column stores the body only. Receipt rows
   written before 0008 are development-only, are not backfilled (receipts are
   immutable), and are not read by the amended projections.
3. Evidence kind `handoff` records execution recovery state; it never
   satisfies verification, audit, push, or closeout blockers.
4. `finalize_candidate` (scope `work.approve`) names the planned candidate it
   finalizes, inserts a second immutable candidate of kind `final` bound to
   the exact full-length commit, derives the plan receipt onto it, and
   atomically advances `current_candidate_id`. An owner-approved plan always
   permits a new planned candidate on the same revision (OMP-124); a live
   close attempt is superseded with terminal reason
   `superseded_by_new_plan`. Revision changes still clear the current
   candidate and stale every old receipt. Completion requires a final
   candidate with a non-null full object ID and a push receipt in one of two
   exclusive shapes (OMP-99, incident 2026-08-22): exact — `remote_commit`
   equals the candidate commit; or containment — `remote_commit` records the
   newer same-branch tip, `candidate_commit` records the candidate, and the
   receipt body carries a host containment attestation re-verified against
   the remote at close time. Null never satisfies a push; branch rewinding
   and permanent per-candidate refs remain forbidden.
5. Capability records carrying `work.candidate.read` must name a non-empty
   `candidate_ids` allowlist; such principals may only read the workflow whose
   current candidate is allowlisted and cannot mutate.
6. `Candidate.candidate_sha256` is defined byte-exactly as
   `SHA-256(UTF-8(canonical_json({"algorithm": "work.omp.dev/v1/candidate-sha256",
   "commit_sha": <full lowercase hex object id, 40 or 64 chars>, "paths": [...]})))`,
   where `canonical_json` is the contract's existing primitive (sorted keys,
   compact separators, raw UTF-8) and `paths` is the candidate commit's complete
   file list as reported by `git diff-tree --no-commit-id --name-only -r <commit>`
   read with `-z` and decoded as strict UTF-8 (a freeze that meets a non-UTF-8
   path name refuses the candidate). Absent `path_basis` defaults to legacy
   commit-diff and omits the field from the canonical JSON payload;
   `"sealed-snapshot"` defines `paths` as the plan-sealed path list validated
   against the approved tree and includes `"path_basis": "sealed-snapshot"`. Paths
   are hashed exactly as stored — no
   Unicode normalization, because a Git tree may legally contain both NFC and NFD
   spellings of the same displayed name as distinct entries, and normalizing would
   map two different candidate path sets to one hash. The path list must be
   non-empty, duplicate-free, free of control characters, and free of `./`, `//`,
   `\`, and trailing `/`; it is sorted by UTF-8 byte order before hashing. The
   algorithm is implemented once per runtime: `omp_work.v1.canonical.candidate_sha256`
   (service/importer side) and `packages/work-client/src/index.ts`
   `candidateSha256` (owner extension side). Golden vectors in
   `contracts/v1/candidate-hash.json` pin both implementations; the vectors cover
   unsorted input, duplicate rejection (negative), a decomposed-Unicode path stored
   verbatim (any NFC-normalizing implementation hashes differently and fails the
   vector), an astral-plane path whose UTF-8 byte order differs from UTF-16
   code-unit order (a naive JavaScript `.sort()` fails the vector), and an
   unsorted sealed-snapshot basis vector (`sealed-snapshot-unsorted`). The store does
   not recompute the hash — it has no repository — but completion, finalization,
   receipt binding, and the smoke probe all compare the exact stored value, so an
   implementation drift is caught by the fixture tests and the end-to-end smoke.
