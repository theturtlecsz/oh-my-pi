# The close ritual (ledger-owned, OMP-47)

The WorkService owns every gate. Your /summary already bound this attempt:
the host froze and finalized the candidate and began the ledger close attempt
under a host-minted authorization. Every step below is a `work` tool call;
when the ledger refuses, its typed event names the reason, the legal next
actions, and the remaining budget — follow THAT, never re-litigate, never
retry beyond what the event allows. A runtime gate refusal outranks any
static reminder text, however often repeated (obs #142).

Perform in order:

1. **Project updates** — every touched project gets honest health plus one
   plain-language line via `record_health`. Include another project when this
   session moved its dependency or promise.

2. **Capture triage** — every stray finding, watch-item, or parked idea
   becomes a work item via `create_work`, or is explicitly dropped. Chat and
   local ledgers are not destinations. A write's classification follows its
   payload, not its verb (obs #144): proposal-bearing creates are never
   routine self-confirm writes — split them or show Chris the preview. A
   fix found AND fixed this session on the parent's candidate is filed as a
   child work item plus one `append_evidence` `kind:"same_session_found_fixed"`
   receipt on the child (body carries `## Finding` and `## Verification`
   sections) — /done then completes it with the parent, transactionally. The
   receipt may be appended any time after /summary began the attempt — the
   parent's PASS audit is validated transactionally at /done, not at append
   time.

3. **Verification evidence** — on the current NOW/executing item, post
   `action:"append_evidence"`, `kind:"verification"` with the concrete check
   evidence (what ran, what passed, what remains unverified). On success the
   service SEALS THE AUDIT MANIFEST from that exact receipt — the tool result
   says so. If sealing is refused, the event names what to fix first.

4. **Independent audit** — call `work` `action:"get_work"` on the reviewed
   item and read its AUDIT TASK section: the complete, sealed, five-section
   auditor task. Spawn exactly ONE `auditor` task VIA THE TASK TOOL whose text
   is EXACTLY the sealed body between the BEGIN/END markers — byte-for-byte,
   nothing added, never an outputSchema, never `agent()` inside an eval cell
   (eval bypasses the audit gate entirely: its default 30s timeout killed a
   live auditor mid-transport and burned the launch, OMP-68 2026-08-21).
   The gate reserves one bounded launch before the
   spawn (changed bytes refuse with zero slot burn), and the tool result of
   the auditor settles the launch automatically: the service normalizes the
   report, mints the audit receipt itself, and answers with a typed event.
   There is NOTHING to forward — `append_evidence kind:"audit"` is always
   refused. The settle outcome is delivered to the owner as a receipted
   checkpoint automatically. A NEEDS_FIX or BLOCKED verdict ends this
   attempt honestly: record the findings in the review, file the fixes, and
   stop — the next owner-entered /summary starts a fresh bounded attempt.

5. **Closeout review** — call the workflow tool exactly once with
   `action:"append_evidence"`, `kind:"closeout"`, and the review `body`. The
   body carries all technical evidence from SECTION 1 plus the complete
   next-session state and loop charter from SECTION 2 — never a second
   handoff comment or a PROMPT-*.md file. The service accepts it only against
   the audited attempt and mints a review checkpoint; the host delivers that
   checkpoint to the owner and attests the delivery. Require `success:true`.

6. **Request the close (PASS only)** — after the review checkpoint is
   delivered, run the routine self-confirmed `request_closeout` on the
   reviewed item (OMP-23 handshake: preview, verify, confirm in the same
   turn). The service refuses it while the attempt is not audited or any
   checkpoint delivery is still owed — a `delivery_pending` refusal means
   deliver or owner-waive (`action:"waive_delivery"`, visible two-phase)
   first. On a non-PASS attempt there is nothing to request: end blocked,
   honestly.

Leave NOW unchanged. The owner's `/done` — and only that — completes the
attempt, the work item, and any validated same-session children.
