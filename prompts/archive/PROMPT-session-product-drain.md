# PROMPT — owner PRODUCT verdict-drain loop (dedicated product session)

Paste into a fresh **omp** session. This is a LOOP charter — drain the PRODUCT
decision queue item by item with Chris; do not stop after one. Written
2026-08-10 ~23:1xZ after the first drain mixed worlds and product verdicts had
to be retracted. Owner chose this dedicated session explicitly.

## OPEN THE SESSION BY SAYING THIS TO CHRIS, VERBATIM-ISH

"Everything in this session is a PRODUCT FEATURE decision — what the household
sees on the TVs. No session-plumbing questions. Eight items, one at a time."

This framing line is mandatory. Its absence is what broke the first drain.

## Starting state (2026-08-10 ~23:1xZ)

- Queue source: `waiting` read (bounded `linear` tool, team HOME). The product
  items below all carry `waiting-on-chris`. Verdicts recorded in the first
  drain on HOME-14/13/11 were RETRACTED on the issues via context-correction
  comments — treat them as never-answered; `get_issue` each fresh.
- The eight product items:
  1. **HOME-14** Retire the old listings-failover automation? — the backup
     automation that filled missing TV listings; made junk NFL entries and
     crash-looped 168×/day. Currently DISABLED on CT 109 in both schedule
     sources (settings.py commented + django-celery-beat DB row id 59
     enabled=False; nothing deleted). RETIRE verdict = delete task +
     controller via `ssh root@super` → `pct exec 109 -- ...`; the code lives
     under `/opt/dispatcharr-v0272/dispatcharr/` in CT 109. Prove: beat clean,
     no check-epg-failover anywhere, no crash-loop in logs.
  2. **HOME-13** 24/7 channels in On Now: drop or keep? — real episode titles
     make loop-channels naturally fall out of On Now rows. Default = let them
     drop (no code change).
  3. **HOME-12** 504 unmappable channels (441 satellite-radio + 63 music):
     label wording. Proposal on issue: "Satellite radio – no listings". Chris
     once typed "drop radio" under the wrong context — possibly wants them
     REMOVED from the guide entirely. Unverified intent — ask cleanly.
  4. **HOME-11** Team auto-record rules (Tigers/Lions/Wolverines) stay?
     Default = kept.
  5. **HOME-10** Jeopardy: keep WABC 7PM or switch to WDIV 7:30 local? Same
     episode either way. Default = keep WABC. (Seeded rule id 5 pins WABC.)
  6. **HOME-8** Close verdict: live pause & rewind — proven on hardware
     08-09. His yes = the household has it; closes the issue AND he closes
     the Live TV project himself. By-seeing law: he must have SEEN it work.
  7. **HOME-7** TiviMate-class guide: pick package 1 (reads-like-cable,
     cheapest), 2 (+ record/reminder markers), or 3 (full multi-day + genre
     colors + density toggle).
  8. **HOME-6** Categories build: on the bedroom box; "ship it" merges the
     30+ commit UX branch to main. By-seeing law: only after "I can see it".
     If he hasn't looked → defer, stay labeled.
- Key: `~/.config/linear.env` (reads sanctioned; writes NEVER raw). Team HOME.
- Session-system drain charter (separate world, separate session):
  `~/PROMPT-session-verdict-drain.md`. Session-system queue remainder:
  HOME-27 (session system in one private repo) + 6 close-proposed cards
  awaiting his click in Linear.

## The loop, per item

1. `waiting` read → take the top PRODUCT item. `get_issue` it fresh.
2. Explain in PLAIN LANGUAGE (law 23 amended 2026-08-06): what the thing is,
   why the household cares, what each option changes for him — zero bare
   jargon; names only as citations after the explanation.
3. AskUserQuestion with a recommended option — every option listed,
   recommended first. (A malformed options array tainted a batch on
   2026-08-10.) His answer IS the verdict.
4. Execute the verdict immediately:
   - CLOSE verdict → `comment` ("Owner verdict in session: close"), then
     `propose_close` two-phase (preview shown, confirm:true after his yes).
     He closes the card in Linear himself.
   - Decision verdict → `comment` the verdict, then EXECUTE the approved
     action in the same session if session-executable, proving live.
   - Deferred (e.g. HOME-6 unseen) → leave labeled, note why in a `comment`.
5. Pull the next item. No interim reports between items.

## MUST NOTs

- Never close issues/milestones/projects without a recorded owner verdict
  this session. He closes cards in Linear; the tool proposes only.
- No session-plumbing items in this drain. If `waiting` shows them, skip
  them and say so.
- Tool-initiated writes ALWAYS two-phase. No raw GraphQL writes.
- **Device safety (law 19): never contact a TV during active household
  viewing. Wake only on exact Asleep; never poll; guarded key driving only.**
  HOME-6/HOME-8 by-seeing verdicts need HIS eyes on the bedroom box — he
  drives, not you.
- No secrets in issues/comments.

## Stop conditions

- Product queue empty or only deferred items (prove: fresh `waiting` read), OR
- Chris ends the drain, OR
- a verdict's execution fails live proof twice honestly, OR
- context nearly exhausted.

## On stop

Rewrite this file with refreshed queue state, then run `/skill:summary`.
