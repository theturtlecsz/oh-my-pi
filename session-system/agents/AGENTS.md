# AGENTS.md — home-directory sessions (all CLIs)

## Linear bookends — session protocol (owner-ratified 2026-08-10; omp-only hooks since 2026-08-10)

Live work state lives in Linear: workspace linear.app/spec-kit, team HOME.
Design record: `~/.omc/specs/deep-interview-linear-big-blocks.md`.

1. **START**: the start digest is omp-only (linear-now extension). Other
   CLIs start without one — read Linear via GraphQL (key at
   `~/.config/linear.env`) if session state is needed.
2. **MID-SESSION**: capture, don't chase — stray ideas become Linear issues
   in their surface immediately (GraphQL, key at `~/.config/linear.env`);
   sloppy titles fine. No other mid-session Linear writes, except
   owner-gated closes/archives per §3 when Chris gives his on-screen yes
   in the moment (the 2026-08-10 close-now ruling).
3. **CLOSE (explicit-command only — HOME-114)**: the close ritual — health +
   a one-line update per touched project, capture triage into surfaces,
   owner-gated closes (close_issue verdict path, propose_close async, agents
   never close without it), archiving issues closed more than 1 day
   (free-plan 250-issue care) — runs ONLY when Chris explicitly invokes it:
   /summary or /done in omp, an explicit ask in other CLIs. Never start it
   because the session seems to be ending; a keep-open verdict blocks every
   closeout action. In omp this is host-enforced: the extension refuses the
   wrap-up writes unless it saw Chris enter /summary or /done itself.
4. **HANDOFF (routing law, owner-ratified 2026-08-10)**: session handoffs go
   through Linear — the NOW pointer plus comments on the issues themselves.
   Never write `~/PROMPT-*.md`, handoff files, or any local file that tracks
   work state. Retired charters live in the session-system repo under
   `prompts/archive/`; nothing new joins them.

Structure law (owner-ratified 2026-08-10, rev 2 — layer inversion after
by-seeing): initiatives = WORLDS (Media Discovery, Kavedarr, Home Lab,
Session System); projects = SURFACE nouns ("Live TV", "Listings") with
health colors — the daily glance; milestones = PROMISE sentences ("Guide
feels like cable"), closed only by the owner's verdict; issues = work. The
`waiting-on-chris` label is the owner decision queue — anything needing
Chris goes there.
