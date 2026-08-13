# PROMPT — session-system hardening loop (standing goal: "Linear runs the show")

Paste into a fresh **omp** session at `~`. Standing goal: The Bookends promise
"Linear runs the show" (Session System world, Linear team HOME). Design record:
`~/.omc/specs/deep-interview-linear-big-blocks.md`. This is a LOOP charter —
drain the queue item by item; do not stop after one slice.

## Starting state (2026-08-10 ~22:1xZ, queue DRAINED — all three fixes live and proven)

- Extension: `~/.omp/agent/extensions/linear-now.ts` — ALL THREE fix batches are
  LIVE AND PROVEN: the 20:00Z batch, the 20:5xZ ownerGate forceTwoPhase fix,
  and the 21:2xZ label-gap fix (create_issue `queue:true` + new `queue_issue`
  action). Proven end-to-end this session (~22:0xZ):
  - **(a) queue:true**: HOME-35 created in The Bookends with waiting-on-chris
    applied AT CREATION; two-phase preview correctly rendered the
    `+ waiting-on-chris label` line; get_issue verified the label.
  - **(b) queue_issue**: HOME-33 and HOME-34 remediated (one preview each,
    owner confirmed both in ONE batched question); get_issue verified the
    label on both (label id 6679022d-ef06-43b4-98b1-a08b96ac1cea, team HOME,
    single instance — no duplicate).
  - **(c) waiting**: full 14-item queue read lists HOME-35/34/33 at top.
  - Proof comment posted on HOME-30 (~22:0xZ) with all outputs.
- **Known behavior, NOT a bug**: a `waiting` read in the SAME SECOND as a
  label mutation can miss the just-labeled issues (Linear propagation race;
  identical raw query returned all 14 immediately, tool re-read seconds later
  agreed). Future proof passes: re-read after a beat before declaring failure.
- Bookends health one-liner for the label-gap fix: owner DECLINED (~22:0xZ,
  bookkeeping). The 20:1xZ update stands as the gate-work record.
- Raw GraphQL writes are now FULLY retired (comments and labels both have
  bounded tool actions). Reads remain sanctioned.
- NOW: UNSET (verified live 2026-08-10 ~22:1xZ via my_now; the ~21:1xZ pick did
  not persist — either cleared by Chris or never landed; session-start digest
  already showed unset). Correct state: session work complete, nothing to hand
  off. lastDone = HOME-30. Key: `~/.config/linear.env`. Team HOME.
- Ruling in force: system is omp-only. Do NOT resurrect v1, the planner, or
  purged extensions (telegram/btw/continue/web-ui).

## Queue — empty. Everything remaining is owner verdicts (on the issues):

- HOME-33 Pomodoro: drop third-party plugin, or fork in-house? (recommended:
  DROP — filed with full reasoning on the issue)
- HOME-34 LINEAR_API_KEY scoping (recommended: A — reads-only policy +
  read-only-scoped key if Linear supports it)
- HOME-35 proof capture — safe to delete (owner verdict closes)
- HOME-32 walkthrough test capture — safe to delete (owner verdict closes)
- Plus the pre-existing queue: HOME-30, HOME-22, HOME-14, HOME-13, HOME-12,
  HOME-11, HOME-10, HOME-8, HOME-7, HOME-6.

## The loop — batch extension edits per restart (amended 2026-08-10)

Extensions load only at omp startup, so every live proof of an extension edit
costs a session restart — and the restart kills the session running this
loop. Implement ALL queued code fixes in ONE edit batch, ask Chris for ONE
restart, prove the whole batch in the fresh session (this charter re-enters
via its own file). Per item: read the finding fully before editing; after
restart drive the exact action that failed and paste output as the proof
comment on the finding's issue. Real proofs only — "should work" is not
evidence. No new extension findings are open as of 2026-08-10 ~22:1xZ; the
loop is idle until the next finding surfaces.

## MUST NOTs

- Never close issues/milestones — propose + `waiting-on-chris`; owner verdict
  closes.
- Tool-initiated writes are ALWAYS two-phase — the first call must write
  NOTHING and return the preview; `confirm:true` only after Chris saw the
  preview IN THE TRANSCRIPT and said yes. The on-screen dialog is for human
  slash commands only, never the tool path (owner veto 2026-08-10 ~20:5xZ).
- Raw GraphQL writes with the key: comments are no longer a sanctioned
  exception (use the `comment` action); labels are no longer a sanctioned
  exception either (`queue:true` / `queue_issue` are live and proven).
  Reads remain fine.
- No secrets in issues/comments. omp-only ruling stands.

## Stop conditions

- Queue drained (prove: fresh HOME-30 comment scan + `waiting` read, all items
  fixed-and-proven or parked on Chris) — MET 2026-08-10 ~22:1xZ, OR
- everything remaining needs an owner verdict — ALSO MET, OR
- a fix fails live proof twice honestly, OR
- context nearly exhausted.

## On stop

Rewrite this file with the refreshed state, then run `/skill:summary` —
Phase-5 writes now go through the two-phase path (or on-screen dialog where
it works).
