# SECTION 2 — NEXT SESSION (the standing goal and its loop prompt)

Sessions serve **long-running goals**, not one-off errands. The owner's
standing ruling (2026-08-08): stop producing single-task charters session
after session; plan a goal once, then hand every subsequent session a
**standing loop prompt** that drains that goal's roadmap item by item until a
stop condition fires. This phase's deliverable is that prompt.

**Step 1 — name the standing goal.** State in one line which long-running
goal the session's work served, and where that goal's plan lives (spec,
roadmap, ledger — exact path). Two special cases:

- **No goal or no plan exists** → the next session is a PLANNING session,
  not an execution session. Its prompt charters the repo's planning
  convention to produce the goal, its roadmap, and the queue sources a loop
  can later drain. Planning first is the ruling, not an option.
- **The goal's roadmap is drained** (everything shipped or parked on the
  owner) → say so with proof, list what's parked, and offer candidate next
  goals instead of manufacturing filler slices.

**Step 2 — offer 2–4 charters** the owner can pick from. Sources: PARKED
items once unblocked, SHAKY items that need evidence, BLIND SPOTS that need
action, and the standing goal's highest-priority ready roadmap item. Each
charter says which long-running goal it serves and is self-contained — the
next session has zero context:

- **Goal** — the owner's underlying goal, stated before any method. The
  Objective below is the chosen method's end state and is explicitly
  revisable (obs #70). Any charter whose method burns real resources embeds
  a pre-execution re-confirmation of the METHOD, not just plan details.
- **Objective** — one sentence, verifiable end state.
- **Why now** — what this session changed or discovered that makes it live.
- **State pointers** — exact commits, branches, services, file paths, host
  names. Any rerunnable driver/tool pointer records the exact last-used
  command line, all arguments and env quirks included (obs #84).
- **First action** — the concrete first command or step.
- **Gates** — owner approvals, device-safety limits, or laws in force.
- **Data path walked** — when the charter names an end-to-end proof target,
  walk the read path backwards NOW (surface → endpoint → store → writer) and
  charter any missing producer/mapping as explicit prep work (obs #90).

Mark the recommended charter and say why in one line.

**Step 3 — write the loop prompt.** Once the owner picks (or for the
recommended charter if he doesn't), write the next-session prompt as a
**standing loop charter**, not a single-task errand. Required skeleton,
adapted to the repo:

- **Goal** — the standing goal, method revisable.
- **Starting state** — repo, branch, expected head commit, liveness probes
  for anything that might still be running.
- **Queue sources, in priority order** — where the session builds its work
  queue (the ledger surface/promise tree and its open items first, then the
  goal's roadmap/spec, open recommendations in named artifacts, labeled
  stubs). The session builds the queue itself and executes top to bottom; it
  never stops because "one charter's worth" is done.
- **The loop, per item** — read fully, implement the smallest version, prove
  with the repo's existing test patterns, run the gates (exact commands,
  output redirected to files), commit one slice per commit, pull the next
  item. No interim reports between slices.
- **MUST NOTs** — paid calls, protected paths, label-don't-delete,
  never-red-at-commit — resolved from the current repo's laws.
- **Stop conditions, exhaustively** — queue empty (with proof all sources
  were checked), all remaining items parked on the owner, suite red after
  two honest fix attempts, context nearly exhausted. Waiting on a background
  step is NEVER a stop condition (obs #91).
- **On stop** — write the refreshed loop charter for the next session in the
  same format, then the plain-English owner summary.

Single-task charters are the exception, reserved for work that genuinely
cannot loop — say why the loop form doesn't fit when you use one.

**Prompt hygiene, all forms:**

- **Deliver the charter through the session review.** In ledger-routed
  estates, include the next-session charter in the single typed closeout
  receipt on the current item. NEVER post a second handoff comment or write
  PROMPT-*.md/handoff files. Elsewhere, save to
  `.omc/handoffs/PROMPT-<topic>-<date>.md` AND show it in chat.
- If the prompt promises to quote or analyze a stored data field, grep the
  source artifact for that field NOW and either confirm it exists or
  downgrade the objective (obs #87).
- Process-lifetime claims are beliefs at write time, not runtime truth:
  phrase each as an assumption with its mandatory liveness probe (obs #54).
- A handoff charter is state, not prose: when any chartered step is executed
  outside the session that will run the charter, mark it DONE in the charter
  artifact in the same turn, and re-verify each step against current reality
  before handing off (obs #101).
- If the owner picks a different charter than recommended, regenerate the
  prompt for their pick before the session ends.

**Pre-delivery check (run, don't skim):** (1) the two audit skills were
actually invoked via the Skill tool this session; (2) the charter is inside
the single closeout review body in ledger-routed estates, else its file
exists; (3) the prompt is a loop charter tied to a named standing goal, or
justifies the single-task exception; (4) every embedded command was executed
or probed this session; (5) every touched project got health plus one line,
or is listed as not updated with a reason; (6) every finding is filed, listed
UNVERIFIED, or dropped out loud; (7) NOW was re-verified and remains the
reviewed issue.
