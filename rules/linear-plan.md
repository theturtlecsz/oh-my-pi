# Linear weave — planning and work tracking

The owner's work lives in Linear (team HOME): initiatives = WORLDS, projects =
SURFACE nouns with health, milestones = PROMISE sentences (owner verdict
closes), issues = work. A `linear` tool is available: reads are free, writes
always render an on-screen confirmation.

When in plan mode or asked to plan:

1. Ground the plan in the live tree first — call `linear` with `tree` (and
   `my_now` / `waiting`) before proposing structure. Plan against existing
   surfaces and promises; never invent parallel tracking structures.
2. End every accepted plan by proposing Linear issues for the planned work
   (one issue per independently shippable slice) via `create_issue` — the
   owner approves each on screen. File into the surface the work belongs to.
3. After issues exist, offer to set the NOW pointer to the first one
   (`set_now`).

Always: stray findings worth keeping become issues (capture, don't chase);
closes need the owner's on-screen yes (close_issue verdict path); otherwise
propose. Keep Linear reads bounded;
never dump the backlog into context.

## Routing law (owner-ratified 2026-08-10) — no prompt files, no file trackers

Work routes through Linear, period. Open loops live as issues; session
handoffs go through the NOW pointer and issue comments; per-item context
lives ON the issue it belongs to. NEVER write a `~/PROMPT-*.md`, handoff
file, or any local file that tracks work state — that includes "on stop,
rewrite this charter file" patterns from retired charters (archived in the
session-system repo, `prompts/archive/`). A fresh session orients with the
start digest, then `/now` — not by reading a file. Exception by standing
law: media-discovery's TASKS.md is that product's engineering roadmap
(separate world, separate ruling) — do not extend the exception.
