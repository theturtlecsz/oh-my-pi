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
propose closes, never close — owner verdict closes. Keep Linear reads bounded;
never dump the backlog into context.
