# PROMPT — Session-management system v2: product-focused, Linear-like

Paste this into a fresh session (any CLI). It is self-contained.

## What this session is

Design — from the beginning — a system that solves Chris's two ADHD problems in
AI-assisted coding: **losing his place** ("what specific thing was I working
on?") AND **losing the big picture** ("what is this project even becoming?").
Both matter equally; v1 died partly because task tracking outweighed big
picture.

Direction Chris set: **product-focused and clearer — like linear.app.** Explore
that seriously: Linear itself, Linear-like tools, and any tools/skills/plugins
for his CLIs that help. This is a DISCUSSION AND RESEARCH session first.
Nothing gets built or installed without an explicit "g"/"go" after he agrees
with a direction.

## History — one paragraph

V1 (2026-08-09) was GitHub Issues as a "shared notebook" + a 49-line
`session-gate` script printing a 4-line reorientation digest at session open.
It was built, seeded into media-discovery (block-oriented: product-language
parent issues, technical sub-issues), and live-proven on all four CLIs — then
Chris rejected it the same night: not product-focused or clear enough.
Everything was torn down. Full design record (all issues, the script, README):
`~/session-system-v1-archive/`. Agent memory: `shared-notebook-trial-live-20260809`.

## Step 0 — one leftover teardown item

The GitHub repo `theturtlecsz/session-notebook` still exists (deletion needs a
scope the token lacks). Chris runs, if he wants it gone:
`! gh auth refresh -h github.com -s delete_repo` then
`! gh repo delete theturtlecsz/session-notebook --yes` — or keep it as archive.

## Requirements that SURVIVED v1 (owner-validated — carry them, don't relitigate)

1. **Top level speaks product.** Big blocks in plain product language; technical
   items live underneath, never at the surface. (Chris asked for this
   explicitly in v1 and it still holds.)
2. **Big picture is co-equal with task tracking.** A "where is this project
   going" view he can glance at, per project AND across projects.
3. **Phone/couch capture** — file an idea in ~10 seconds from the sofa, brain
   releases. This was his single hardest requirement.
4. **Bounded context reads** — agents query a slice, never re-read a growing
   file. (He spotted this anti-pattern himself.)
5. **Cross-subscription** — must work from all four CLIs: omc (Claude Code),
   omx (Codex), kimi, omp (oh-my-pi). PROVEN FACT you can reuse: all four
   auto-read repo `AGENTS.md` (each was live-tested doing it).
6. **The machine carries the maintenance.** Chris does captures and verdicts;
   organizing/triage/upkeep is agent work. ADHD systems die of maintenance tax.
7. **Enforcement by mechanism, not memory** — research showed prose rules decay
   mid-session; whatever v2 picks needs a structural way to actually happen.
   (But v1's gate cried wolf on multi-writer repos — naive "any new commit"
   staleness checks fail where lanes commit in parallel. Archive issue #8.)

## Research mandate (do this BEFORE proposing anything)

- **Linear.app**: its agent story (March 2026: Linear pivoted to agent
  orchestration; agents author ~25% of issues; it became OpenAI Symphony's
  control plane), its MCP server for coding agents, API, CLI options, pricing
  for a solo/household user, phone app quality.
- **Linear-likes**: Plane, Huly, anything newer — especially self-hostable ones
  (he has a Proxmox lab but ruled self-hosting "not a requirement" once;
  re-confirm, don't assume).
- **Tools/skills/plugins**: Linear MCP servers usable from Claude Code / codex
  / kimi / omp; existing Claude skills or plugins for Linear; what omp's
  extension system offers here.
- **MANDATORY adversarial pass**: for any tool you're about to recommend, run a
  dedicated criticism search ("problems", "gave up", "overkill", months-later
  reviews) BEFORE presenting it. V1's first recommendation (beads) reversed
  under one skeptic search; the owner now requires this.

## Process laws for this session (standing owner rulings — violating these is how trust dies)

- **Discussion before implementation.** Present research as trade-offs, not
  execution-gated plans. He was twice burned by "rushing to implementation."
- **One idea at a time, plain language.** No multi-section design dumps. No
  jargon without explanation in the same sentence. He says "it's not been
  explained to me" when output is dense — that's a full stop.
- **Every decision he must make goes through AskUserQuestion** with a
  recommended option — never buried in prose.
- **Own-project-first.** The system develops in its own home. Product repos
  (media-discovery, kavedarr) are touched ONLY on explicit, current-session
  invitation — an approved plan merely naming them is NOT that invitation.
- **Nothing installed/changed without "g".** Read-only probes are fine.
- **Simplicity tripwire**: v1's rule was "more than ~50 lines of custom script
  means we're rebuilding planner." Keep an equivalent guard.

## Owner context (so you don't re-ask)

Chris: ADHD, vibe-codes ~99% via CLIs, four AI subscriptions (Claude Max,
ChatGPT Pro, Kimi, + omp's providers). GitHub user (`zimmermanc` login;
`theturtlecsz` org). The `planner` project — his previous attempt at exactly
this problem — is PARKED; do not propose resuming it. media-discovery already
has a working big-picture page (Product-State.md in its wiki, owner-facing,
hook-enforced at session close) — whatever v2 proposes must not duplicate or
compete with surfaces like that without an explicit merge decision.

## First move of the session

Read `~/session-system-v1-archive/` (5 files, small) for the full v1 record,
then open the discussion with Chris: what specifically felt unclear /
insufficiently product-focused about v1 — get his words BEFORE researching, so
the research targets his actual objection rather than a guess.
