# PROMPT — omp session-system walkthrough (live proof, owner at keyboard)

Paste into a fresh **omp** session started at `~`. Chris drives, the agent
guides one step at a time and records evidence. Goal: prove every piece of
the session system actually works, live, and leave the evidence in Linear
(HOME-30, HOME-31, HOME-22). Nothing else gets built in this session.

Rules: one step at a time — wait for Chris to say what he sees before moving
on. Every proof becomes a comment on its Linear issue quoting what was seen
(the proof bar is "live now", not "should work"). On any failure: capture the
exact error as a comment on HOME-30, don't rabbit-hole fixes. The only edit
allowed without a fresh "g" is step 2's one-line tree fix.

## Steps

1. **Digest injection (HOME-22 — the headline).** The act of starting this
   session is the test: did a "── Linear bookend ──" block appear in the
   agent's context this turn? Agent quotes it back. Seen → comment the quote
   + timestamp on HOME-22 so Chris can rule the close. Absent → HOME-22 stays
   open; run `/linear status` and note what "digest this session" says.

2. **DONE 2026-08-10 — fix applied out-of-band, verify only.** The
   `initiative`→`initiatives` tree fix was applied in the review session
   before the omp restart, so the running session already has it. Do NOT
   re-apply. Verify instead: call the `linear` tool `tree` action — clean
   `World › Project [state/health]` lines = proven, log it for HOME-30.

3. **`/linear status`.** Expect: key found · API reachable with a
   millisecond figure · now-label holder shown · digest injected. Chris
   reads it out; agent records.

4. **`/now` picker.** Bare `/now`, pick **HOME-30** from the list. Footer
   should flip to `◆ NOW · The Bookends · HOME-30 …` with a running clock.

5. **`//capture`.** `/capture walkthrough test capture — safe to delete`.
   Confirm dialog appears, issue lands in The Bookends. Then agent proposes
   archiving the test issue (owner verdict, as always).

6. **The `linear` tool, all reads.** Agent calls `tree` (now fixed),
   `waiting`, `get_issue HOME-30`, `my_now` — each returns sensible output,
   no dumps into prose.

7. **Pomodoro.** pi-pomodoro is installed but never used. Start one short
   timer, see it fire. If its commands don't exist or confuse — that's a
   finding on HOME-30, not a fix.

8. **The close ritual (HOME-31).** Run `/skill:summary`. It must load,
   walk its phases scoped to this walkthrough session, and drive Phase 5
   writes through the `linear` tool with on-screen confirms: a Bookends
   health line, triage of the test capture, and the NOW handoff. Evidence
   comment on HOME-31.

9. **Wrap.** If steps 1–8 all proved out: `//done` on HOME-30 — the close
   proposal lands in the decision queue, which is itself the last proof.
   Agent posts the final tally comment on HOME-30 (what passed, what
   failed, what drifted) and leaves HOME-22, HOME-30, HOME-31 verdicts to
   Chris.

## Context pointers

- Extension: `~/.omp/agent/extensions/linear-now.ts` · state cache
  `~/.omp/agent/linear-now.json` · plan rule `~/.omp/agent/rules/linear-plan.md`
- Key: `~/.config/linear.env` · team HOME · project "The Bookends"
- Ruling of 2026-08-10: system is omp-only — Claude/Codex/Kimi hooks and the
  shared digest script are deleted; omp is the only surface that greets with
  state. Skills load cross-harness from `~/.agents/skills` (summary,
  questionyourself, whatsmissing already there).
- Do not resurrect v1, the planner, or purged extensions (telegram/btw/
  continue/web-ui).
