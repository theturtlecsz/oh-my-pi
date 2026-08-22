# 0006 — Batch-completion rider authority (OMP-93, owner ruling 2026-08-22)

## Problem

Filing work is mandatory and free; closing it requires a per-item owner
ceremony (/plan → /summary with bounded audit → /done). Verified-delivered
historical items therefore accumulate as open paper — the queue grows by
design (79 open in The Bookends at ruling time, ~25 of them finished work).
The same-session child mechanism (decision OMP-52) covers only work created
inside the closing attempt's owner session.

## Decision

1. **Sealed riders.** `begin_close_attempt` accepts `riders`: explicit
   `{work_id, revision_id, evidence}` proofs. The service validates each
   rider at seal time (exists, on that exact current revision, not DONE /
   CANCELED / archived, not the primary, no duplicates, ≤32 riders,
   evidence ≤4096 bytes), snapshots title + acceptance criteria, computes
   `evidence_sha256`, and stores the sealed tuples on the attempt row.
   Membership is enumeration, never a query; the DB transition trigger makes
   the sealed riders immutable.
2. **Audited proofs.** The sealed task body gains a `Riders` section carrying
   each rider's title, acceptance criteria (AC-R<n>.<m>), evidence digest,
   and full evidence text — `task_sha256` covers it, so the accepted PASS
   report attests the riders. Manifests carrying riders are
   `manifest_version = 2`; riderless attempts and all existing rows stay v1.
   Per-rider PASS-coverage is enforced by the auditor contract (BLOCKED on
   uncovered ACs), deliberately NOT by service-side report text parsing —
   parsing prose at the settle boundary is the OMP-67 failure class.
3. **Exact-tuple completion.** `complete_work` re-validates every sealed
   rider (same revision, still open, digest intact) and completes them
   atomically with the primary; any drift refuses the whole /done with
   `rider_binding_invalid` (fresh authorization required). Each completed
   rider gets a `rider_completed` event on its own work history binding the
   primary work, task hash, evidence hash, and /done authorization.
4. **Resume identity.** A /summary authorization resumes its attempt only if
   the rider set (ids, revisions, evidence) is byte-identical; any change is
   `authorization_identity_mismatch`.
5. **Host staging.** The owner-facing batch lives OUTSIDE any repository at
   `<agent-dir>/work-rider-batches/<sha256(cwd)[:16]>.json` as
   `[{key, evidence}]`; at literal /summary the host verifies the file is a
   regular 0600 file owned by the current uid (≤256 KiB), resolves keys to
   current ids, and asks the owner to confirm the exact key list + batch
   digest. Decline archives the file and seals nothing; any read/stat error
   other than ENOENT aborts the attempt (a staged batch never silently
   shrinks). Consumption is a one-shot rename.
6. **Outcome semantics.** Riders are completions of verified-delivered work —
   DONE, never CANCELED. Cancellation remains a separate owner-gated verb for
   absorbed/duplicate/deletable items.

## Migration

`0016_close_attempt_riders.sql`: adds `close_attempts.riders jsonb NOT NULL
DEFAULT '[]'` (array-typed), replaces the transition trigger to include
riders in the immutable identity, and widens the manifest version check to
`IN (1, 2)`.
