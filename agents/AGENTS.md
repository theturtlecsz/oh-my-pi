# AGENTS.md — home-directory sessions (all CLIs)

## Linear bookends — session protocol (owner-ratified 2026-08-10; omp-only hooks since 2026-08-10)

Live work state lives in Linear: workspace linear.app/spec-kit, team HOME.
Design record: `~/.omc/specs/deep-interview-linear-big-blocks.md`.

1. **START**: the start digest is omp-only (linear-now extension). Other
   CLIs start without one — read Linear via GraphQL (key at
   `~/.config/linear.env`) if session state is needed.
2. **MID-SESSION**: capture, don't chase — stray ideas become Linear issues
   in their surface immediately (GraphQL, key at `~/.config/linear.env`);
   sloppy titles fine. No other mid-session Linear writes.
3. **CLOSE**: for every project touched this session write health + a
   one-line update; triage new captures into their surface; propose closes —
   the owner's verdict closes projects, agents never do; archive issues
   closed more than 1 day (free-plan 250-issue care).

Structure law (owner-ratified 2026-08-10, rev 2 — layer inversion after
by-seeing): initiatives = WORLDS (Media Discovery, Kavedarr, Home Lab,
Session System); projects = SURFACE nouns ("Live TV", "Listings") with
health colors — the daily glance; milestones = PROMISE sentences ("Guide
feels like cable"), closed only by the owner's verdict; issues = work. The
`waiting-on-chris` label is the owner decision queue — anything needing
Chris goes there.
