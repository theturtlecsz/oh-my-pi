# PROMPT — owner verdict-drain loop (standing goal: "Linear runs the show")

Paste into a fresh **omp** session. Standing goal: The Bookends promise "Linear
runs the show" (Session System world, Linear team HOME). This is a LOOP charter —
drain the owner decision queue item by item with Chris; do not stop after one.

**REWIRED 2026-08-10 ~23:0xZ after the first drain run:** this charter is now
SESSION-SYSTEM ITEMS ONLY. Product items (Listings, Recordings, Live TV — the
eight below) were mixed into the first drain and Chris answered several while
believing the questions were still about session/Linear plumbing; those verdicts
were retracted on the issues. **A product drain must be its own dedicated
session, opened by telling Chris explicitly "these are product feature
decisions."** Never mix worlds in one drain again.

## Starting state (2026-08-10 ~23:0xZ, after first drain)

Drained this session (session-system items — verdicts recorded, close proposals
posted; **Chris closes the cards himself in Linear**):
- HOME-35, HOME-32 — proof/test captures, close proposed.
- HOME-30 — all three omp pieces proven (tree fix, digest, timer), close proposed.
- HOME-22 — digest present in live session, symptom gone, close proposed.
- HOME-34 — verdict: C, leave the LINEAR_API_KEY as-is (policy-only), close proposed.
- HOME-33 — verdict: DROP pi-pomodoro. EXECUTED: `bun remove` in ~/.omp/plugins
  + lock entry removed + node_modules verified absent. Timer unloads next session.
  Close proposed. Session system is now 100% first-party omp code.

Remaining queue = PRODUCT ITEMS ONLY (verdicts from the first drain RETRACTED
on HOME-14/13/11 via context-correction comments; nothing was executed):
- HOME-14 listings-failover automation retirement (still disabled on CT 109,
  nothing deleted) · HOME-13 24/7 channels in On Now · HOME-12 504 unmappable
  channels (he once typed "drop radio" under wrong context — unverified intent) ·
  HOME-11 team auto-record rules · HOME-10 Jeopardy channel · HOME-8 live pause
  & rewind close verdict · HOME-7 TiviMate guide package pick · HOME-6
  Categories build (needs his eye on the bedroom box).

- Key: `~/.config/linear.env` (reads sanctioned; writes NEVER raw — bounded
  tool only). Team HOME.
- NOW: unset. This session MAY propose a new NOW if a drain item becomes active work.
- Hardening loop charter (idle, re-enters on any new extension finding):
  `~/PROMPT-session-system-hardening.md`.
- Ruling in force: system is omp-only. No v1/planner/purged-extension revival.

## The loop, per item

1. `waiting` read → take the TOP item. `get_issue` it fresh.
2. **FRAME THE WORLD FIRST.** Before the first question of a session, say in
   plain language which world the items belong to (session plumbing vs product
   features). If the queue contains both, STOP and ask Chris which world to
   drain — never blend them.
3. Explain each item in PLAIN LANGUAGE (law 23 amended 2026-08-06): what the
   thing is, why the household cares, what each option changes — zero bare
   jargon; names only as citations after the explanation.
4. AskUserQuestion with a recommended option. **Build the options array
   carefully — every option listed, recommended one first. A malformed
   options array tainted a whole batch on 2026-08-10.** His answer IS the verdict.
5. Execute the verdict immediately:
   - CLOSE verdict → record it in a `comment` ("Owner verdict in session:
     close"), then `propose_close` (two-phase: preview shown, confirm:true
     after his yes). He closes the card in Linear himself. Never close
     unilaterally.
   - Decision verdict → `comment` the verdict, then EXECUTE the approved
     action in the same session if session-executable, proving live.
     Extension edits batch per the hardening charter's one-restart rule.
   - Deferred → leave labeled, note why in a `comment`.
6. Pull the next item. No interim reports between items.

## MUST NOTs

- Never close issues/milestones without a recorded owner verdict this session.
- Tool-initiated writes ALWAYS two-phase: first call writes nothing, preview
  shown verbatim, `confirm:true` only after Chris's yes in the transcript.
- No raw GraphQL writes. Reads fine.
- No secrets in issues/comments. Never contact a TV during household viewing.
- Never mix product items into a session-system drain (see top).

## Stop conditions

- `waiting` read returns empty or only deferred/other-world items (prove:
  fresh read), OR
- Chris ends or pauses the drain, OR
- a verdict's execution fails live proof twice honestly, OR
- context nearly exhausted.

## On stop

Rewrite this file with the refreshed queue state, then run `/skill:summary`.
If the drain emptied the session-system queue, say so with the proof read —
the standing goal then idles until new captures arrive. The product queue
drains in a dedicated, explicitly-framed product session.
