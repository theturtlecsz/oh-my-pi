# 0007 — Bookends closure amendments (OMP-147, owner-approved plan 2026-08-25)

## Problem

Three residual gaps block closing The Bookends backlog cleanly:

1. Same-session found-and-fixed filing (decision OMP-52) takes three separate
   commands from the host (create child, put parent relation, append the typed
   receipt). A crash or refusal between them strands a child without its edge
   or receipt — non-atomic authority the close ritual then trips over
   (OMP-139; OMP-133 filed the same defect).
2. A host process loaded before a contract deployment keeps issuing commands
   from its stale module constants. Today the failure surfaces as
   `invalid_request` at discriminator depth — or worse, as a semantically
   wrong but parseable command (OMP-143).
3. Sealed audit manifests fall back to `(none recorded)` when a work item has
   no structured revision criteria and no `## Acceptance criteria` section,
   even when the approved plan receipt stores explicit verification gates
   (OMP-140 follow-up).

## Decision

1. **Atomic same-session filing.** New command `create_same_session_child`
   under scope `work.close`. Payload: `parent_work_id`, `attempt_id`,
   `owner_session_id`, one `CreateWorkInput`, `finding`, `verification`
   (both non-blank). One serializable store transaction locks the parent and
   the named LIVE attempt; requires an open parent, matching attempt and
   owner session, and a final candidate; creates one BACKLOG child inheriting
   the parent's project, the active `parent` edge child→parent, and mints the
   existing `SameSessionFoundFixedPayload` receipt bound to the attempt's
   start commit, final commit, and candidate SHA. Any validation or conflict
   failure rolls back child, edge, receipt, and alias allocation together.
   Result: the created item plus the minted receipt. Completion authority is
   unchanged — the child still closes only through the parent's owner /done
   (decision OMP-52); no generic relation or receipt authority is exposed.
2. **Fail-first contract handshake.** Every authenticated client request
   carries `X-OMP-Contract-SHA256`, the contract digest the client binary
   loaded. The server compares it with its own `contract_sha256()` BEFORE
   parsing a command body or authenticating: missing or unequal values return
   HTTP 409 with new normative error code `contract_mismatch` and diagnostics
   naming the loaded host digest (`missing` when absent), the service digest,
   and the sole recovery — `restart the OMP session`. No command, budget,
   event, or idempotency row is written. A retired stale host therefore never
   reaches discriminator validation, so no historical-command allowlist or
   compatibility shim exists. Health probes stay unauthenticated and exempt.
3. **Verification-gate fallback for sealed acceptance criteria.** Sealing
   keeps the existing precedence — structured revision criteria, then a
   revision-description `## Acceptance criteria` section, then a plan-body
   `## Acceptance criteria` section. When all three are empty, the bound plan
   receipt's stored `verification` array supplies the criteria (non-blank
   strings, stored order); `(none recorded)` remains only for legacy plans
   with no source at all. No prose outside named/stored fields is parsed.

## Consequences

- The contract digest changes; owner approval of the exact new digest under
  issue `OMP-147` is mandatory before deployment.
- Hosts ship the digest as a generated constant
  (`packages/work-client/src/contract.ts`); a process loaded before
  deployment keeps the old constant and receives the typed restart refusal on
  its next authenticated request.
- `create_same_session_child` supersedes the three-command filing path for
  same-session children; the generic `append_evidence` receipt path remains
  valid for children created by other means (decision OMP-52 semantics).
