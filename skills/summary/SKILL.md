---
name: summary
description: End-of-session closing ritual — actually invokes the questionyourself and whatsmissing skills over the whole session, writes a SESSION REVIEW for the owner, runs the Linear close ritual (project health updates, capture triage, propose-closes, NOW handoff), and writes a standing loop-session prompt that keeps the current long-running goal iterating across sessions. Use when the user runs /summary or asks to close out a session.
---

# /summary — session closing ritual

Run this when the owner closes a session. It exists to answer four questions
honestly: *what actually happened, what is shaky about it, what is nobody
looking at, and what should the next session do.* It fuses the
`questionyourself` and `whatsmissing` audits with the law-27/28 session-close
contract and adds the one thing that contract lacks: forward-looking handoff.

## Identity and limits (state these to yourself before starting)

- You are the author grading your own work. This skill is a mirror, not a
  witness: it is law-13a bookkeeping, never law-13 independent review. Its
  "BUILT+PROVEN" lines must reflect verified evidence, and it must never be
  treated as approval of product-affecting work.
- The empty answer is a first-class outcome. If nothing is shaky, say so and
  show why. If nothing is missing, say so and show why. Never manufacture
  doubt or drama to justify the ritual.
- Scope is the WHOLE session, not just the last task. Earlier work in a long
  session is exactly where unexamined risk hides.

## Portability (2026-08-07, obs #63)

This skill was authored in one project but runs in many. Resolve every
project-specific step from the CURRENT repo — its CLAUDE.md/AGENTS.md, its
artifact and handoff directories, its ledgers, its review contracts. Where a
named convention (a numbered law, a manifest, a wiki page) has no local
equivalent, substitute the current repo's own practice silently — never
narrate the mismatch. Owner-facing output speaks only the current project's
language; the authoring project's names must never appear in it.

## Owner questions — plain language or don't ask (owner ruling 2026-08-06)

ANY question put to the owner — in this ritual or anywhere — must be
answerable by a non-engineer on the first read:

- **Context before content.** Say what the thing IS and why it matters to the
  household before asking anything about it. One plain sentence of "what
  happened / what this does" first.
- **No bare jargon.** Service names, table names, file paths, migration
  numbers, and commit hashes never carry the question. They may appear as a
  citation AFTER the plain-language explanation, never instead of one.
  "person-facts-refresh FAILED — restart?" is unanswerable; "the nightly job
  that fetches actor photos crashed this morning — want me to restart it?"
  is not.
- **Consequences in household terms.** Every option states what the household
  notices, not what the codebase does.
- If a question cannot be de-jargonned, the asker doesn't understand it well
  enough yet — go back and find the plain version.

## Phase 0 — Ground in artifacts (mandatory before any prose)

Never summarize from conversation memory alone. Re-query the record. As cheap
as these are, just run them:

- `git log --oneline` since session start on every repo touched (check
  `git worktree list` for lanes you created or left behind), plus
  `git status` on the prod tree.
- Deploy manifests: tail `deploy-manifests/*.jsonl` for lines written today.
- Live state of anything deployed or changed: `systemctl` is-active on
  touched services/timers, relevant journal tails, DB or API spot-checks of
  the session's claims.
- Linear (owner page, team HOME): one bounded read — in-flight projects with
  health, the `now` holder, and the `waiting-on-chris` queue. Where the
  `linear` tool is available use it (`tree`, `my_now`, `waiting`); otherwise
  query the GraphQL API directly (key at `~/.config/linear.env`). This is the
  record the close ritual updates.
- TASKS.md / decision-queue state for anything parked.

Anything you cannot re-verify goes in an explicit **UNVERIFIED** list — never
silently promoted to fact. If a claim from earlier in the session fails
re-verification, that is a headline finding, not a footnote.

## Phase 1 — questionyourself, over the whole session

**Invoke the `questionyourself` skill with the Skill tool now.** Actually
loading it is mandatory — a paraphrase from memory of what it says is a
skipped phase, and skipping it silently is the known failure mode this line
exists to block. Run its full protocol scoped to the WHOLE session:
**what are you least confident about in what this session did?** Name the
specific claim resting on the weakest evidence; distinguish verified
(ran/read/measured — cite it) from assumed or taken-on-trust; rank shakiest
first; for each, state what would increase confidence and do it immediately
if cheap. Honor the "nothing material" escape hatch.

## Phase 2 — whatsmissing, over the whole session

**Invoke the `whatsmissing` skill with the Skill tool now.** Same rule:
loading it is mandatory, paraphrase is a skipped phase. Run its full
protocol scoped to the whole session: **what is the biggest thing the owner
is probably missing and hasn't thought to ask?** Step outside the
conversation's frame; ground each item in a specific file, commit, service,
or clock; rank by blast radius; classify each as *a question the owner should
ask*, *a fact nobody verified*, or *a decision being made by default through
inaction*; run cheap checks instead of naming them. Include second-order
effects of decisions made this session (what else did that ruling unblock,
who else can overwrite this state, what else shares the thing you changed).
Honor the "nothing material" escape hatch.

## Phase 3 — SECTION 1: THE SESSION (for the owner)

Audits BEFORE summary — never write the summary first and let the audits
anchor on it. Fold Phase 1–2 findings in; the summary must absorb them, not
contradict them.

Format (the law-28 fixed format, extended):

- **DECIDED** — rulings made, with where each is now recorded (or flagged as
  recorded nowhere).
- **BUILT+PROVEN** — each item with its dated evidence: commit hashes, test
  counts, live proofs re-queried in Phase 0.
- **SHAKY** — the Phase 1 ranking, compressed.
- **BLIND SPOTS** — the Phase 2 ranking, compressed.
- **UNVERIFIED** — claims that could not be re-grounded, explicitly labeled.
- **PARKED ON YOU** — every item waiting on the owner (decision, approval,
  in-person action), each with a one-line "how to unblock." Each item here
  must ALSO exist in Linear carrying the `waiting-on-chris` label (file it in
  Phase 5 if it doesn't) — chat prose evaporates, the label is the queue.
- **PRODUCT MOVED** — what the household can see/do now that it couldn't
  this morning. If nothing, say nothing moved.

## Phase 4 — SECTION 2: NEXT SESSION (the standing goal and its loop prompt)

Sessions serve **long-running goals**, not one-off errands. The owner's
standing ruling (2026-08-08): stop producing single-task charters session
after session; plan a goal once, then hand every subsequent session a
**standing loop prompt** that drains that goal's roadmap item by item until
a stop condition fires. This phase's deliverable is that prompt.

**Step 1 — name the standing goal.** State in one line which long-running
goal the session's work served, and where that goal's plan lives (spec,
roadmap, ledger — exact path). Two special cases:

- **No goal or no plan exists** → the next session is a PLANNING session,
  not an execution session. Its prompt charters the repo's planning
  convention (deep interview / consensus planning — whatever the current
  repo uses) to produce the goal, its roadmap, and the queue sources a
  loop can later drain. Planning first is the ruling, not an option.
- **The goal's roadmap is drained** (everything shipped or parked on the
  owner) → say so with proof, list what's parked, and offer candidate
  next goals instead of manufacturing filler slices.

**Step 2 — offer 2–4 charters** the owner can pick from. Sources: PARKED
items once unblocked, SHAKY items that need evidence, BLIND SPOTS that need
action, and the standing goal's highest-priority ready roadmap item. Each
charter says which long-running goal it serves. Each is self-contained —
the next session has zero context:

- **Goal** — the owner's underlying goal, stated before any method. The
  Objective below is the chosen method's end state and is explicitly
  revisable; a charter that records only the method loses the intent needed
  to cheaply re-route when costs or circumstances change (obs #70). Any
  charter whose method burns real resources (paid runs, subscription burn,
  long builds) embeds a pre-execution re-confirmation of the METHOD, not
  just of plan details.
- **Objective** — one sentence, verifiable end state.
- **Why now** — what this session changed or discovered that makes it live.
- **State pointers** — exact commits, branches, services, file paths, host
  names the next session needs without searching. Any rerunnable
  driver/tool pointer records the exact last-used command line, all
  arguments and env quirks included (obs #84).
- **First action** — the concrete first command or step.
- **Gates** — owner approvals, device-safety limits, or laws in force.

Mark the recommended charter and say why in one line.

**Step 3 — write the loop prompt.** Once the owner picks (or for the
recommended charter if he doesn't), write the next-session prompt as a
**standing loop charter**, not a single-task errand. Required skeleton,
adapted to the repo:

- **Goal** — the standing goal, method revisable.
- **Starting state** — repo, branch, expected head commit, liveness probes
  for anything that might still be running.
- **Queue sources, in priority order** — where the session builds its work
  queue (the Linear surface/promise tree and its open issues first, then the
  goal's roadmap/spec, open recommendations in named artifacts, labeled
  stubs). The session builds the queue itself and executes top to
  bottom; it never stops because "one charter's worth" is done.
- **The loop, per item** — read fully, implement the smallest version,
  prove with the repo's existing test patterns, run the gates
  (exact commands, output redirected to files), commit one slice per
  commit, pull the next item. No interim reports between slices.
- **MUST NOTs** — paid calls, protected paths, label-don't-delete,
  never-red-at-commit — resolved from the current repo's laws.
- **Stop conditions, exhaustively** — queue empty (with proof all sources
  were checked), all remaining items parked on the owner, suite red after
  two honest fix attempts, context nearly exhausted.
- **On stop** — write the refreshed loop charter for the next session in
  the same format, then the plain-English owner summary: slices shipped
  with commits, slices parked and why, test totals.

Single-task charters are the exception, reserved for work that genuinely
cannot loop (a one-shot migration, a paid run needing owner presence) —
say why the loop form doesn't fit when you use one.

**Prompt hygiene, all forms:**

- **Deliver the charter to the repo's handoff home yourself.** In
  Linear-weave estates (routing law, owner-ratified 2026-08-10) that home is
  a handoff comment on the NOW/executing issue — PROMPT-*.md/handoff files
  are BANNED there; sessions self-orient from the bookend, never from
  prompt files (obs #106). Elsewhere, save to
  `.omc/handoffs/PROMPT-<topic>-<date>.md` AND show it in chat. Either way,
  never end by telling the owner to save it — a drafted-but-unsaved prompt
  was lost exactly that way (obs #88).
- If the prompt promises to quote or analyze a stored data field, grep the
  source artifact for that field NOW and either confirm it exists or
  downgrade the objective (obs #87).
- Process-lifetime claims ("X died with the session", "the run is
  finished") are beliefs at write time, not runtime truth: phrase each as
  an assumption with its mandatory liveness probe ("assumed dead —
  `pgrep <pattern>` before relaunching"), and gate every resume step that
  relaunches, re-arms, or re-creates on that probe (obs #54).
- If the owner picks a different charter than recommended, regenerate the
  prompt for their pick before the session ends.

**Pre-delivery check (run, don't skim):** (1) Phases 1 and 2 actually
invoked their skills via the Skill tool this session; (2) the charter
artifact exists — comment landed (success:true) in Linear-weave estates,
else the file on disk (`ls` it); (3) the prompt is a loop charter tied to a named
standing goal, or carries a one-line justification for the single-task
exception; (4) every command embedded in the prompt was executed or probed
this session, not composed from memory; (5) every touched project got its
Linear health + one-line update, or is explicitly listed as not updated and
why; (6) zero session findings remain unfiled — each is an issue, in the
review as UNVERIFIED, or dropped out loud; (7) the NOW pointer was
re-verified true, handed off, or cleared.

## Phase 5 — The Linear close ritual (the bookend CLOSE contract)

Linear (team HOME) is the owner page; this phase updates it. Where the
`linear` tool is available (omp), drive it through the tool — every write
shows on screen for the owner's yes. Elsewhere, draft the exact payloads and
show them before writing via API. Four steps, in order:

1. **Project updates** — for every project this session touched: health
   (onTrack / atRisk / offTrack, honestly) + ONE plain-language line of what
   moved (`update_health`). Cross-surface propagation: if the work moved a
   promise in ANOTHER project (a trial blocker, a dependency), that project
   gets its line too.
2. **Capture triage** — every stray finding, watch-item, or parked idea from
   the session becomes an issue in its surface (`create_issue`), or is
   explicitly dropped out loud. The ledger file is not a destination;
   findings that only live in TASKS.md are unfiled, and unfiled = lost.
3. **Propose closes** — anything finished gets a close PROPOSAL
   (`propose_close`: comment + `waiting-on-chris`). Owner verdict closes;
   never close an issue or milestone yourself.
4. **NOW handoff** — is the NOW pointer still true? Finished → `/done` (or
   `propose_close` + clear). Continuing next session → leave it. Superseded →
   propose the next NOW and set it on the owner's yes (`set_now`).

Also draft, on owner confirmation, the **SESSION REVIEW** (Section 1) and —
where the current repo keeps a knowledge vault — the **wiki vault ingest**
for durable knowledge produced. Legacy Product-State pages are a pointer to
Linear now; do not maintain parallel decision queues there.
