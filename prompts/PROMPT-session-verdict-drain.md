# PROMPT — owner verdict-drain loop (standing goal: "Linear runs the show")

Paste into a fresh **omp** session. Standing goal: The Bookends promise "Linear
runs the show" (Session System world, Linear team HOME). This is a LOOP charter —
drain the owner decision queue item by item with Chris; do not stop after one.
Written 2026-08-10 ~22:2xZ after the hardening loop drained its own queue and
everything remaining became owner verdicts.

## Starting state (2026-08-10 ~22:2xZ)

- Extension `~/.omp/agent/extensions/linear-now.ts`: ALL fix batches live and
  proven (20:00Z tree batch, 20:5xZ ownerGate two-phase, 21:2xZ label-gap).
  Bounded `linear` tool actions: get_issue, tree, waiting, my_now, comment,
  create_issue (queue:true), queue_issue, propose_close, update_health, set_now.
- Key: `~/.config/linear.env` (reads sanctioned; writes NEVER raw — bounded
  tool only). Team HOME.
- NOW: unset (verified ~22:1xZ). Correct: prior work complete. This session MAY
  propose a new NOW if a drain item becomes active work.
- Hardening loop charter (idle, re-enters on any new extension finding):
  `~/PROMPT-session-system-hardening.md`.
- Ruling in force: system is omp-only. No v1/planner/purged-extension revival.

## The queue (fresh `waiting` read at session start is authoritative; snapshot):

Close proposals pending verdict (comment + label already posted):
- HOME-30 Prove the unproven omp pieces + fix the broken tree action — all
  proofs complete, evidence on the issue. Recommend CLOSE.
- HOME-22 omp start-hook not firing — symptom absent in current session log
  (digest present, 2026-08-10T20:44Z session). Recommend CLOSE.
Delete candidates (proof/test captures, served their purpose):
- HOME-35 Extension proof capture — recommend CLOSE (delete-equivalent).
- HOME-32 walkthrough test capture — recommend CLOSE.
Decisions with filed reasoning (read the issue body to Chris in plain language):
- HOME-33 Pomodoro: drop third-party plugin, or fork in-house? Recommend DROP.
- HOME-34 LINEAR_API_KEY scoping. Recommend A (reads-only policy + read-only
  key if Linear supports it).
Pre-existing queue (from before this drain; read each issue fresh):
- HOME-14 listings-failover automation retirement · HOME-13 24/7 channels in
  On Now · HOME-12 504 unmappable channels wording · HOME-11 team auto-record
  rules · HOME-10 Jeopardy channel · HOME-8 live pause & rewind close verdict ·
  HOME-7 TiviMate guide package pick · HOME-6 Categories build (needs his eye).

## The loop, per item

1. `waiting` read → take the TOP item. `get_issue` it fresh.
2. Explain to Chris in PLAIN LANGUAGE (law 23 amended 2026-08-06): what the
   thing is, why the household cares, what each option changes for him — zero
   bare jargon; service/file names only as citations after the explanation.
3. AskUserQuestion with a recommended option. His answer IS the verdict.
4. Execute the verdict immediately:
   - CLOSE verdict → he closes it himself in Linear, OR if he tells the session
     to close it, that instruction is the owner verdict — record it in a
     `comment` first ("Owner verdict in session: close"), then he or the
     system closes. Never close unilaterally without the recorded verdict.
   - Decision verdict (e.g. HOME-33 drop pomodoro) → `comment` the verdict on
     the issue, then EXECUTE the approved action in the same session if it's
     session-executable (uninstall plugin, scope key policy, etc.), proving
     the result live. If execution needs a restart (extension edits), batch it
     per the hardening charter's one-restart rule.
   - Deferred → leave labeled, note why in a `comment`.
5. Pull the next item. No interim reports between items.

## MUST NOTs

- Never close issues/milestones without a recorded owner verdict this session.
- Tool-initiated writes ALWAYS two-phase: first call writes nothing, preview
  shown verbatim, `confirm:true` only after Chris's yes in the transcript.
- No raw GraphQL writes. Reads fine.
- No secrets in issues/comments. Never contact a TV during household viewing
  (some verdicts may propose product work — device-safety law 19 stands).
- Extension edits, if any verdict requires one: batch ALL of them, ONE restart,
  prove in the fresh session (hardening charter re-enters).

## Stop conditions

- `waiting` read returns empty or only deferred items (prove: fresh read), OR
- Chris ends the drain, OR
- a verdict's execution fails live proof twice honestly, OR
- context nearly exhausted.

## On stop

Rewrite this file with the refreshed queue state, then run `/skill:summary`.
If the drain emptied the queue, say so with the proof read — the standing goal
then idles until new captures arrive.
