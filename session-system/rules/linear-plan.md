# Linear weave — planning and work tracking

The owner's work lives in Linear (team HOME): initiatives = WORLDS, projects =
SURFACE nouns with health, milestones = PROMISE sentences (owner verdict
closes), issues = work. A `linear` tool is available: reads are free, writes
always render an on-screen confirmation.

## Uniform memory law (owner-ratified 2026-08-13, HOME-112)

state lives in Linear; knowledge lives in files; files change when reality
changes — never on a session clock. The Linear close ritual is the only
mandatory per-session write set; no other store may carry a mandatory
per-session write. Knowledge files (wikis, runbooks, references) update in
the same pass as the work that changes them.

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
closes are explicit-command only — propose or execute a close ONLY inside an
owner-entered /summary or /done, or on Chris's explicit ask (close_issue is
his on-screen verdict path). Keep Linear reads bounded;
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

## Checkpoint contract (2026-08-11, HOME-44 session — extends the routing law)

The issue is the durable cross-session log; `local://` plans, todos, and chat
do not survive the session. A plan file may be authoritative for the current
execution, but anything that must outlive the session goes on the issue.
Two mandatory comment moments when executing against an issue:
- **Plan approved** → post a digest comment on the issue: decisions that refine
  the blueprint, acceptance list, artifact paths + file hash. Not the full plan
  — the resume kit.
- **Every stop** — restart needed, blocked on the owner, session ending — →
  post a handoff comment: done / remaining / exact resume steps. (This is the
  routing law's existing handoff clause, restated at the point of failure: the
  2026-08-11 HOME-44 run posted its handoff only after the owner asked.)
- **Session closing** (owner ruling 2026-08-11; explicit-command boundary
  2026-08-13, HOME-114) → closeout is explicit-command only: the `/summary`
  close ritual (questionyourself + whatsmissing + Linear close ritual) runs
  ONLY when Chris literally enters `/summary`, and `/done` only when he
  enters `/done`. Never start either — or any close proposal, health
  update, or NOW handoff — because work or the session looks finished; a
  keep-open verdict blocks every closeout action. The handoff comment is
  still owed at every stop — handoff preserves state, /summary audits it.

Enforcement: the linear-now session-start bookend carries a CHECKPOINT CONTRACT
line, and the `linear` device doc carries the same rule — both reach every
session regardless of whether this rule file is injected. The closeout boundary
is also host code, not just text: the extension refuses update_health,
propose_close, and archive_issue unless it observed Chris literally entering
/summary or /done this session (close_issue stays his on-screen verdict path).
