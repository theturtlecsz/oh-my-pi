# PROMPT — ADHD session-management system: full review + owner walkthrough

Paste into a fresh Claude Code session at ~/. RESEARCH FIRST — read-only
probes are free; nothing installed or changed without "g".

## What you are reviewing

The owner's ADHD session-management system built around Linear
(linear.app/spec-kit, team HOME). It keeps his place across coding sessions
in 4 CLIs and surfaces "what now" without him holding state in his head.
It was assembled across several sessions and has never had a single
end-to-end review. Your job: map it, question it, and walk the owner
through what it is and what it's missing.

Known components (verify each, this list may be stale or incomplete):

- Linear workspace structure rev 2: worlds → surfaces → promises → work
  (owner page = Linear; containers get noun names, deliverables get
  sentence names)
- omp extension: `~/.omp/agent/extensions/linear-now.ts` — owns footer key
  `linear-now`, commands /now //done //capture //linear, tool `linear`,
  digest injection at session start
- Session bookends (start digest + close contract) hooked into all 4 CLIs
  — find the hook files per CLI and confirm each still fires
- /summary close ritual (Claude Code skill) — runs Linear close ritual:
  project health updates, capture triage, propose-closes, NOW handoff
- Key: `~/.config/linear.env`
- Related memory files in `~/.claude/projects/-home-thetu/memory/`:
  `linear-v2-live-20260810`, `omp-linear-weave-live-20260810`,
  `session-system-omp-only-20260810`, `omp-extension-casualties-resolved-20260810`,
  `shared-notebook-trial-live-20260809` (v1 REJECTED + torn down — do not
  resurrect v1 ideas), `planner-parked-20260809` (do not propose planner work)

Recent history that bounds the review: v1 (GitHub-Issues/session-gate
design) was rejected and deleted. The phone surface (pi-telegram) was
purged 2026-08-10. pi-btw/pi-continue dropped. Owner is building his own
web UI (HOME-26). omp-only focus stands for session-system work.

## Phase 1 — Inventory and the repo question

Locate every real artifact of the system: the extension file, hook files
in each CLI, the /summary skill, any scripts. For each: path, size, last
modified, and — the owner's explicit question — **is it under version
control anywhere?** Check git status of every containing directory. If the
answer is "the system lives as loose dotfiles with no repo, no history, no
backup", say that plainly and treat it as a finding.

## Phase 2 — Scope map

From the artifacts (not from memory files alone), reconstruct what the
system actually does end to end: session start → during-session capture →
session close → cross-session continuity. Produce a plain-language map the
owner can confirm or correct. Mark every step as VERIFIED (you ran or read
it) or ASSUMED (memory/docs only).

## Phase 3 — Gap analysis

Compare what exists against what an ADHD keep-my-place system needs.
Candidate lenses (probe each, don't assume): quick capture friction;
resume-after-interruption; time-blindness aids (timers, pomodoro — note
pi-pomodoro was DROPPED 2026-08-10 by owner verdict, HOME-33; the gap is
now real — no timer exists in omp); proactive nudges (idle-notify exists via
pi-yaml-hooks); phone/away-from-desk surface (gone since the telegram
purge — is the gap real or does the owner not miss it?); visibility of
NEEDS-CHRIS decisions; single-place truth (is Linear actually the one
page, or has state leaked back into files?). Also run one adversarial
search: what do existing ADHD task systems do that this one doesn't?
Findings become candidate Linear captures, not lectures.

## Phase 4 — Test audit

The owner asks: has any of this been tested? For each component, find
evidence of a live proof (memory files record some). Build a short matrix:
component / last proven live / how / what has drifted since. Anything
proven only at ship time is PROVEN-AT-SHIP, not live-now. Cheap read-only
re-probes (e.g. does the digest hook still fire, does /now still answer)
are allowed and encouraged — run them and record results.

## Phase 5 — Walkthrough with the owner

Only after phases 1–4: walk the owner through it in plain language, in
small rounds. Every decision goes through AskUserQuestion — recommended
option first, the deciding data inside the question text. No jargon; if a
finding needs a visual, produce a reachable artifact. Expected decision
rounds: (a) repo/backup verdict, (b) which gaps become Linear issues,
(c) which stale pieces get killed, (d) whether anything needs a live
re-proof session. Capture every accepted finding as a Linear issue in its
surface (key in `~/.config/linear.env`, team HOME).

## Rules

- Nothing installed or changed without "g". Read-only probes free.
- AskUserQuestion for anything needed from the owner; plain language
  throughout — "I don't understand" from the owner is a full stop.
- Do not propose resurrecting v1, the planner, or the purged extensions.
- Do not build anything in this session; this is a review. Fixes get
  chartered as Linear issues for later sessions.
- Close with done/remaining/parked accounting and update touched Linear
  projects per the close contract.
