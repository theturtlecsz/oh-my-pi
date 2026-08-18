# AGENTS.md — home-directory sessions (all CLIs)

## Work Ledger bookends — session protocol (owner-ratified 2026-08-10, updated 2026-08-18)

Live work state lives in the Work Ledger (`work.omp.dev/v1`).

1. **START**: OMP injects the current completion tree and canonical workflow
   sequence.
2. **SEQUENCE (OMP)**: `/intake` publishes and selects → `/plan` approves,
   stamps, and executes → execution handoff → `/summary` reviews → `/done`
   closes. Chris never moves state or re-identifies the issue between stages.
3. **MID-SESSION**: capture, don't chase — stray ideas become work items in
   their surface. Evidence receipts never advance the sequence; only typed
   handoff/review receipts do.
4. **SUMMARY (explicit-command only, HOME-114)**: run the health updates,
   capture triage, asynchronous close proposals, and one typed session
   review/handoff ONLY when Chris enters `/summary`. Leave NOW unchanged and
   never close from summary.
5. **DONE (owner verdict only)**: `/done` requires the current approved plan
   plus a later session review, asks once, then closes and clears NOW. No model
   tool exposes a direct close path.
6. **HANDOFF (routing law, owner-ratified 2026-08-10)**: session state lives on
   the issue through the NOW pointer and typed comments. Never write
   `~/PROMPT-*.md`, handoff files, or another local tracker. Retired charters
   remain under `prompts/archive/`; nothing new joins them.

Structure law (owner-ratified 2026-08-10, rev 2 — layer inversion after
by-seeing): initiatives = WORLDS (Media Discovery, Kavedarr, Home Lab,
Session System); projects = SURFACE nouns ("Live TV", "Listings") with
health colors — the daily glance; milestones = PROMISE sentences ("Guide
feels like cable"), closed only by the owner's verdict; issues = work. The
`waiting-on-chris` label is the owner decision queue — anything needing
Chris goes there.
