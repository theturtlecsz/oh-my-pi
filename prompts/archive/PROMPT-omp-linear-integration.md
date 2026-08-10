# PROMPT — omp as daily driver: weave Linear into the skills flow

Paste into a fresh session (any CLI; the omp-live checks need to run inside omp
itself). Supersedes `~/PROMPT-session-system-v2.md` — that session ran 2026-08-09/10
and shipped; this is the next chapter. RESEARCH AND DISCUSSION FIRST — nothing
built without an explicit "g".

## What exists now (built + live 2026-08-10 — do not re-litigate)

- **Linear is the owner page** (linear.app/spec-kit, team HOME). Structure law
  rev 2: initiatives = WORLDS (Media Discovery, Kavedarr, Home Lab, Session
  System) → projects = SURFACE nouns with health ("Live TV", "Listings") →
  milestones = PROMISE sentences ("Guide feels like cable"), owner verdict
  closes → issues = work; `waiting-on-chris` label = the decision queue.
  Reference artifact: https://claude.ai/code/artifact/9a32d2de-bd19-4128-84b1-6b0b5001267c
  Design record: `~/.omc/specs/deep-interview-linear-big-blocks.md`
- **Bookend contract**: START = injected digest (`~/.local/bin/linear-digest`,
  ~150ms, fail-open, fires only where AGENTS.md has `linear-bookends: on` —
  currently ~, media-discovery, kavedarr). MID = capture-don't-chase. CLOSE =
  health + one-line update per touched project, triage captures, propose
  closes, archive >1-day-closed. Key: `~/.config/linear.env`.
- **Hooks wired**: omc SessionStart (~/.claude/settings.json), omx
  (~/.codex/hooks.json), kimi ([[hooks]] in ~/.kimi-code/config.toml), omp
  (pi-yaml-hooks + ~/.omp/agent/hook/hooks.yaml) — **omp's is NOT firing**
  (HOME-22, audit-proven). omc Stop hook re-aimed: blocks once if vault dirty
  or no same-day Linear project update. media-discovery law 28 amended
  (commit `eaade45`, rev-2 wording `9a98ab1`).
- **First real-work audit graded B+**: close ritual + health honesty strong;
  captures still going to TASKS.md ledger only; cross-surface propagation
  missed (clean-day GREEN never touched the Trial project); omp start-hook
  dead.

## Owner direction (this session's ask, 2026-08-10)

Chris: "Skills are my main, easiest flow. I want omp as my daily driver — I
use its /plan and /summary skills. Work Linear into that workflow. omp is
highly extensible — any additional features for keeping track of WHAT PROJECT
I'm working on and WHICH SPECIFIC ISSUE would be phenomenal."

That last part is the NOW-pointer from v1 (one `attention` issue, never more
than one) — deliberately deferred in the v2 design; this session is where it
lands, omp-native.

## Research mandate (before proposing anything)

1. **Fix HOME-22 first** — inside a live omp session run `/hooks-status` and
   `/hooks-validate` (pi-yaml-hooks diagnostics). Candidate causes: global
   config path (~/.omp/agent/hook/hooks.yaml vs ~/.pi/agent/hook/hooks.yaml),
   trust gating (PI_YAML_HOOKS_ALLOW_* env vars), session.created excluded on
   resume. A TS extension via `pi.on("session_start", …)` is the fallback if
   yaml hooks can't inject context.
2. **Map omp's skill system**: where skills live (~/.pi/agent/skills and
   ~/.pi/skills exist), how /plan and /summary are defined TODAY — read their
   actual files before proposing changes. How do skills compose with
   extensions?
3. **Design the Linear weave**: /plan should read+write Linear (plan against
   the surface/milestone tree, file issues instead of loose lists); /summary
   should BE the close ritual (project updates, triage, propose-closes) so the
   contract rides the skill he already uses.
4. **Design the NOW pointer**: track current project + current issue.
   Candidates to research, then grill: a `now` label in Linear (v1 rule: never
   more than one holder); omp statusline/UI surface showing it persistently;
   `/now <issue>` + `/done` skill commands; auto-set from /plan. Bounded reads
   only — never dump the backlog into context.
5. **MANDATORY adversarial pass** (standing owner rule): criticism search on
   any mechanism before recommending ("pi yaml hooks problems", "statusline
   extension overhead", months-later reviews).

## Process laws (standing — violating these is how trust dies)

- Every owner decision through AskUserQuestion, recommended option first,
  deciding data INSIDE the dialog. Plain language; "I don't understand" = full
  stop. One idea at a time.
- Nothing installed/changed without "g". Read-only probes fine.
- Product repos (media-discovery, kavedarr) touched only on explicit
  current-session invitation.
- Simplicity tripwire: >~50 lines of custom standing script = rebuilding
  planner; stop and regroup.
- Structure law rev 2 is settled (see above) — extensions must fit it, not
  reshape it. Dialog-ratified structures are provisional until Chris SEES them
  rendered (this bit twice; artifact-first for anything visual).
- Capture-don't-chase: stray findings become Linear issues in their surface.

## Carried forward (open, not this session's core)

- Live-fire digest proofs still owed: omc, omx, kimi next launches.
- Capture-habit gap: sessions bank watch-items in TASKS.md ledger without
  mirror issues (grade C — fix via /summary weave, not nagging).
- Propagation gap: trial-blocker movement should touch the Trial project.
- First cold-open verdict from Chris (30-second test) still pending; friction
  rule active — anything annoying gets fixed same day.
- HOME-8/6/7/10/11/12/13/14 = his decision queue, untouched by this work.

## First move

Confirm you're in omp (or route the omp-live checks there), run the HOME-22
diagnostics, read the /plan and /summary skill files, THEN open the design
discussion with Chris — his words on what /plan and /summary should feel like
with Linear underneath, before any building.
