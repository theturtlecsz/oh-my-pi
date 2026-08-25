# The close ritual (ledger-owned, OMP-47)

The WorkService owns every gate. Your /summary already bound this attempt:
the host froze and finalized the candidate and began the ledger close attempt
under a host-minted authorization — or, when a live attempt already existed,
the SAME literal /summary RESUMED it (OMP-140): nothing is erased, and every
step the attempt already satisfied is skipped, never redone. Every step below
is a `work` tool call; when the ledger refuses, its typed event names the
reason, the legal next actions, and the remaining budget — follow THAT, never
re-litigate, never retry beyond what the event allows. A runtime gate refusal
outranks any static reminder text, however often repeated (obs #142).

Orient from LIVE state, not memory: a fresh `get_work` on the reviewed item
names the attempt's current state and the single legal next step — start
there, whatever this file's ordering suggests. The fresh render outranks
banners, cached bookends, and hidden continuations alike (obs #203): a
continuation only ever points at state the service has already confirmed.

Perform in order, skipping any step the live attempt already satisfied:

1. **Project updates** — every touched project gets honest health plus one
   plain-language line via `record_health`. Include another project when this
   session moved its dependency or promise.
   The `record_health` preview proves only the gates the service actually
   evaluated for THAT write — it never certifies the whole ritual (obs #204).

2. **Capture triage** — every stray finding, watch-item, or parked idea
   becomes a work item via `create_work`, or is explicitly dropped. Chat and
   local ledgers are not destinations. A write's classification follows its
   payload, not its verb (obs #144): proposal-bearing creates are never
   routine self-confirm writes — split them or show Chris the preview. A
   fix found AND fixed this session on the parent's candidate is filed in ONE
   atomic write (OMP-139): `create_work` with `work:<parent key>`,
   `kind:"same_session_found_fixed"`, `title`, and a `body` carrying
   `## Finding` and `## Verification` sections. The service lands child,
   parent edge, and typed receipt in one transaction against the parent's
   live attempt — /done then completes the child with the parent. A partial
   tuple or any batch/queue/question/project field is refused before preview;
   the filing may run any time after /summary began the attempt.

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

5. **Closeout review (PASS only)** — call the workflow tool exactly once with
   `action:"append_evidence"`, `kind:"closeout"`, and the review `body`. The
   body carries all technical evidence from SECTION 1 plus the complete
   next-session state and loop charter from SECTION 2 — never a second
   handoff comment or a PROMPT-*.md file. The service accepts it only against
   the audited attempt, atomically records the review receipt, and transitions
   the attempt to `closeout_requested`; the host delivers the review checkpoint
   to the owner and attests the delivery. Require `success:true`.
Leave NOW unchanged. The owner's `/done` — and only that — completes the
attempt, the work item, and any validated same-session children.

Two hard-won rules for the gaps between steps (obs #205):

- **Yield gaps need owner quiescence.** When a step must yield the turn (an
  auditor settling, a checkpoint delivering), yield and WAIT for Chris —
  never fill the gap with extra writes, new scans, or a second close path.
  The next literal /summary resumes exactly where the attempt stands.
- **Additive review loops get an antidote pass.** When review rounds keep
  ADDING guards, retries, or wrapper fixes, stop and run a root-cause pass
  before approving anything: find the one shared cause, fix it once where
  every caller routes through, and delete the accumulated symptom patches.
