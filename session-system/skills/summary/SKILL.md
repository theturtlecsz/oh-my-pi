---
name: summary
description: End-of-session closing ritual — actually invokes the questionyourself and whatsmissing skills over the whole session, writes a SESSION REVIEW for the owner, runs the Work Ledger close ritual (project health updates, capture triage, close requests, NOW handoff), and writes a standing loop-session prompt that keeps the current long-running goal iterating across sessions. Use ONLY when the owner has literally entered /summary — never because a session seems to be ending, work finished, or wrap-up wording appeared (HOME-114); /done runs its own separate flow.
---

# /summary — session closing ritual

Run this when the owner enters /summary. It exists to answer four questions
honestly: *what actually happened, what is shaky about it, what is nobody
looking at, and what should the next session do.* It fuses the
`questionyourself` and `whatsmissing` audits with the amended law-28 Work Ledger
close contract (law 27 is the uniform memory law, HOME-112 — no per-session
vault ingest) and adds the one thing that contract lacks: forward-looking
handoff.

## Gate — literal /summary only (HOME-114)

Run this ritual ONLY if Chris literally entered /summary this session. A
finished plan, a completed todo list, an inferred session ending, wrap-up
wording, or a checkpoint reminder is NOT an invocation — if that is what
brought you here, stop now and do nothing. A keep-open verdict on an issue
blocks every closeout action (close proposals, health updates, capture
triage, NOW handoff) until Chris enters /summary or /done.

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
- Work Ledger (owner page): one bounded read — in-flight projects with
  health, the `now` holder, and the triage queue. Use the
  workflow tool (`work`) with `tree`, `my_now`, `waiting`; never query a backend
  API directly. This is the record the close ritual updates.
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

The review OPENS with the completion tree (HOME-109): call the workflow tool
with `action:"my_now"` and paste its output verbatim, then explain in plain
words what this session changed in that tree. Only then the sections below.

Format (the law-28 fixed format, extended):

All seven law-28 sections (DECIDED … PRODUCT MOVED) remain, but chat carries
ONLY plain words: commit hashes, file paths, sha256 seals, and test counts
move to the issue handoff/board comment, cited from chat as "evidence saved
on the issue." (HOME-109 box 4: review renders as the tree + explanation;
technical evidence goes to the board comment only. Law 28(a)'s fixed
sections survive in plain words — the two reconcile, neither is repealed.)

- **DECIDED** — rulings made, with where each is now recorded (or flagged as
  recorded nowhere).
- **BUILT+PROVEN** — each item with its dated evidence: commit hashes, test
  counts, live proofs re-queried in Phase 0. Evidence citations live on the
  issue; chat states what the household can rely on.
- **SHAKY** — the Phase 1 ranking, compressed.
- **BLIND SPOTS** — the Phase 2 ranking, compressed.
- **UNVERIFIED** — claims that could not be re-grounded, explicitly labeled.
- **PARKED ON YOU** — every item waiting on the owner (decision, approval,
  in-person action), each with a one-line "how to unblock." Each item here
  must ALSO exist in the ledger carrying the `waiting-on-chris` label (file it in
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
  queue (the ledger surface/promise tree and its open items first, then the
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

- **Deliver the charter through the session review.** In ledger-routed estates
  (routing law, owner-ratified 2026-08-10), include the next-session charter in
  Phase 5's single typed closeout receipt on the current item. NEVER post a
  second handoff comment or write PROMPT-*.md/handoff files. Elsewhere, save to
  `.omc/handoffs/PROMPT-<topic>-<date>.md` AND show it in chat. Never end by
  telling the owner to save it.
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
invoked their skills via the Skill tool this session; (2) the charter is inside
the single Phase 5 review body in ledger-routed estates, else its file exists;
(3) the prompt is a loop charter tied to a named standing goal, or justifies the
single-task exception; (4) every embedded command was executed or probed this
session; (5) every touched project got health plus one line, or is listed as not
updated with a reason; (6) every finding is filed, listed UNVERIFIED, or dropped
out loud; (7) NOW was re-verified and remains the reviewed issue.

## Phase 5 — The close ritual

The Work Ledger is the owner page. Perform these writes through the workflow
tool (`work`) in order:

1. **Project updates** — every touched project gets honest health plus one
   plain-language line via `record_health`. Include another project when this
   session moved its dependency or promise.
2. **Capture triage** — every stray finding, watch-item, or parked idea becomes
   a work item via `create_work`, or is explicitly dropped. Chat and local
   ledgers are not destinations.
3. **Request closes** — finished items get `request_closeout`; the owner verdict
   still closes them.
4. **Verification + session review** — on the current NOW/executing item, post
   `action:"append_evidence"`, `kind:"verification"` with the concrete check
   evidence (what ran, what passed, what remains unverified). Then call the
   workflow tool exactly once with `action:"append_evidence"`, `kind:"closeout"`,
   and the review `body`.
   The body carries all technical evidence from Section 1 plus the complete
   next-session state and loop charter from Section 2. The host adds the
   `Session review` prefix and current plan hash; require `success:true`.

Leave NOW unchanged. NEVER invoke `/done`, clear NOW, post a second handoff, or
close an issue from `/summary`; Chris enters `/done` separately when he wants
the reviewed issue closed.
