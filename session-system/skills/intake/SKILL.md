---
name: intake
description: Socratic intake — turn a vague idea, an existing plan/draft, or a finished blueprint into a published Linear issue. Use for /intake, "intake this", stress-testing or grilling a plan before building, deep interviews, or spec re-baselines. Supersedes grilling, deep-grill, and deep-interview for owner-side intake; ends at the published Linear issue — execution lanes pull from Linear.
---

# /intake — Socratic intake → Linear blueprint

Internal skill (owner-specific doctrine and estate wiring; not for distribution).

One lane, one skill: taxonomy scan → budgeted Socratic interview with entity-graph
tracking → blueprint artifact → pre-publish lint → two-phase Linear publication.
Execution is OUT of scope: intake ends after owner-confirmed Linear publication.
The extension selects the first published issue (or batch parent) as NOW inside
that confirmed operation; no second prompt or manual state change follows.

Provenance: blueprint HOME-43 (deep-grill session 2026-08-11). This skill replaced
`grilling`, `deep-grill`, and the OMC `deep-interview` engine. The self-graded
ambiguity score is dead — the stop condition below is structural, not a grade.

Enforcement map (every rule here has a mechanical checkpoint, not a reminder):
questions → G1–G6 pre-send gate, checked immediately before EVERY AskUserQuestion;
visible scan → BINDING, rendered before the session's first question,
lint-checked below;
blueprint → lint gate, checked before ANY Linear write; writes → two-phase preview,
owner yes required. If a checkpoint fails, fix and re-check — never proceed.

## Entry seams (three; same engine, different entry state)

An intake session starts from exactly one of:

1. **Blank idea** — owner states a goal in a sentence or two. Full pipeline.
2. **Existing plan/draft** — a plan, spec, or prose draft exists. Parse it into a
   pre-filled blueprint + entity graph first; the taxonomy scan then runs against
   that pre-filled state, so questions target only what the draft leaves open.
   (This is the plan-stress-testing entry — what "grill this plan" used to be.)
3. **`--publish <blueprint>`** — a finished blueprint file. Skip the interview
   entirely; go straight to lint → publication.

Not an entry seam: `/plan` or "review" against work already owned by a published
issue is execution-lane planning under the checkpoint contract — plan in the
executing session, do not re-enter intake.

## Engine

Three mechanisms answer what the old engine graded itself on:

- **What to ask** = taxonomy scan. Scan the current state (idea, draft, or graph)
  against the fixed 11-category coverage taxonomy. Every category scores
  Resolved / Clear (N/A with reason) / Partial / Open. Open and Partial categories
  generate candidate questions. The taxonomy is input vocabulary — never rebuilt,
  renamed, or extended per session:

  1. Functional scope & behavior
  2. Domain & data model
  3. Interaction & UX flow
  4. Non-functional qualities
  5. Integrations & external dependencies
  6. Edge cases & failure handling
  7. Constraints & tradeoffs
  8. Terminology
  9. Completion signals
  10. Placeholders & ambiguities in existing text
  11. Scope boundaries (explicitly out)

- **What order** = dependency order. A question is never asked before the answer
  it depends on exists. Within the unlocked set, rank by impact × uncertainty and
  ask the question whose answer unblocks the most dependent decisions, naming that
  dependency in the "why now" line. One decision per dialog, always.

- **When to stop** = budget + dangling edges. Hard cap of **7 questions** per
  session (owner may set another number at session start). Stop when the budget
  is spent AND the entity graph has no dangling edges AND every asking-you item
  from the visible scan is asked or owner-skipped, OR the owner exits early.
  Gaps that survive the budget land in **Deferred & assumptions** — never
  silently dropped.

### Visible scan (owner-facing, BINDING; HOME-108, 2026-08-13)

After the taxonomy scan and BEFORE the first question, show the owner the gap
checklist in plain words — three short lists:

- **Figured out myself** — settled by reading code or records; each line cites
  what settled it. ONLY facts may sit here — a judgment call (taste, scope,
  tradeoff, anything the owner must live with) in this pile is a violation.
- **Asking you** — every judgment call the sitting surfaced; these ARE the
  session's questions. BINDING: each item is asked unless the owner explicitly
  says skip (his skip recorded verbatim in Decisions).
- **Leaving for later** — gaps deliberately parked; they land in Deferred &
  assumptions.

Jargon-free, readable by a non-engineer on first sight: each line names the
thing itself ("what happens when a recording fails"), never the taxonomy
category ("edge cases & failure handling"), never file paths or symbols —
category names stay internal. The scan renders on every interviewing entry
seam, even when there is nothing left to ask: a quiet session must be visibly
deliberate, not silently self-directed. On the existing-plan seam, the draft's
key decisions the owner never personally ratified are judgment calls and enter
the asking-you pile as confirm-or-overturn items. `--publish` skips the
interview and the scan by definition. There is NO question quota — the rule is
"facts get looked up, judgment calls get asked" (owner ruling at HOME-108
execution, 2026-08-13, superseding the floor-count design).

### Entity graph

Per-feature working state, kept alongside the draft blueprint. Every owner answer
adds exactly one of:

- **node** — a defined noun (entity, state, actor, artifact),
- **edge** — a typed relation between nodes (has / uses / blocks / becomes),
- **constraint** — one of: exactly-one · exclusive · allowed-values · transitive.

A **dangling edge** — a noun referenced in any answer, draft, or criterion that no
node defines — IS an open question and blocks the stop condition.

Constraint-style question templates derive mechanically from the graph:
- exactly-one: "Can X ever have two Ys, or exactly one?"
- exclusive: "Is A ever also B, or are they mutually exclusive?"
- allowed-values: "Name the complete set of states X can be in — is this list closed?"
- transitive: "If A blocks B and B blocks C, does A block C for your purposes?"

### Question mechanics

Every question goes through AskUserQuestion, one at a time, waiting for the answer
before continuing — asking multiple questions at once is bewildering. Every option
list leads with the recommended answer, labeled "(Recommended)", with a one-line
reason. If a question can be answered by exploring the codebase or probing live
state, explore instead of asking — FACTS ONLY: a judgment call is never
self-answered by exploration; it enters the visible scan's asking-you pile.
Every emitted question must first pass the G1–G6 pre-send gate below.

## Pre-send gate (mandatory, every question — added 2026-07-18; extended 2026-08-07)

These laws were violated repeatedly IN grilling sessions (8 documented corrections
2026-07-03 → 2026-07-17) despite being written down. So they are enforced as a
gate, not a reminder: immediately before emitting ANY AskUserQuestion, check all
six — a failure on any one means fix the question before sending, no exceptions:

* **G1 Data, with a ceiling:** the deciding data (verbatim wording, numbers,
  evidence) is INSIDE the dialog — and ONLY the deciding data. Include a fact only
  if it could change the answer; everything else stays in prose above. If the
  question needs more than ~3 facts, split the decision (obs #41: completeness
  without a ceiling inverts into confusion).
* **G2 Jargon:** a non-technical reader understands every term, or it's defined
  in-sentence.
* **G3 Don't-understand debt:** no prior answer in this interview is flagged "I
  don't understand" and still unresolved — if one is, re-explain and re-ask it
  FIRST; forward progress is blocked until it clears (and re-confirm that round's
  other answers after).
* **G4 Ack:** the previous answer got a one-line acknowledgment before this next
  question (silent advancement reads as "did my answer register?").
* **G5 Neutrality (obs #40 — two owner corrections in one grill):** scenarios
  stated symmetrically; no option's case argued in the question text beyond the
  recommended option's one factual reason line; no safety/compliance flavor in
  personal-project contexts. Embedded advocacy both biases the answer and reads as
  distrust.
* **G6 Claims probed (obs #29/#66 — recurred once):** any factual claim embedded
  in the question about what a live system CAN currently do — or any safety fact
  that makes a destructive option sound safe ("recoverable from history") — was
  probe-verified before send, with the probe's result quoted in the dialog. For
  deletion targets: diff against the alleged surviving copy first, always. If a
  claim can't be probed cheaply, hedge it explicitly ("may exist — unverified")
  inside the dialog.

## Data-in-the-question law (owner corrections, 2026-07-15 — twice in one session)

The owner decides from what is INSIDE the AskUserQuestion dialog, not from prose
printed above it. Therefore:

3. The question text itself must carry the data needed to answer it — verbatim
   current wording, measured state, repo evidence — compressed but complete. Prose
   context above the dialog is supplementary, never load-bearing.
4. Use option `preview` fields for anything that needs more room than the question
   text (current-vs-proposed wording, tables, evidence excerpts).
5. If an answer arrives and you realize the deciding data was not in the dialog,
   re-ask with the data embedded rather than banking the answer.
6. ONE decision per dialog (owner correction #3, 2026-07-15): never bundle
   multi-part confirmations into a single question — even a structural
   ratification is confirmed component-by-component, one dialog each, with that
   component's full context (verbatim current wording, dates, evidence, what
   confirming it commits to) detailed in the question text.

## Probe doctrine (2026-08-07 review; obs #26/#36/#44/#64/#65)

Live-state claims are testable assertions — probe them when they are WRITTEN, not
when they are acted on:

7. Acceptance criteria that assert something about live state get their cheap
   probe BEFORE the owner approves the done-test — an approved-but-false criterion
   converts a later probe failure into a scope renegotiation (obs #36).
8. The day an acceptance checklist is sealed, audit every capability it assumes
   against the live tree (grep for the launch points, handlers, flags it
   presumes); each miss becomes a chartered build item ahead of the proof event,
   not a proof-day surprise (obs #44).
9. Session-record blocker lists are claims about live state: re-run the cheap
   read-only probe for each "remaining/blocked" item at record-write time —
   blockers clear silently (obs #26).
10. Landscape/provider research closes with a date pass: extract every
    deprecation, shutdown, or retirement date mentioned; diff each against today;
    live-probe every lane whose critical date has passed or falls inside the
    decision horizon. Probe output goes in the memo as evidence, replacing doc
    claims — documentation states launch-time truth, only a live probe states
    current truth (obs #64).
11. A faithful lane probe replicates the caller's WHOLE invocation contract — user
    identity, environment (including cleared variables), working directory, stdin
    shape, exact arguments. Each omitted dimension fabricates its own misleading
    failure (permission artifact, trust-refusal, silent hang). A lane is only
    "dead" if it fails under the full contract (obs #65).

## Walk mechanics (obs #34/#37/#61)

12. Before any estate-wide review/reorg session, probe for concurrent workers on
    the target trees (running lanes, fresh uncommitted mtimes, lock/handoff
    artifacts) and sequence with the owner if found — a reorg is a long
    read-then-mutate transaction; check for writers before opening it.
13. Sequential rulings are not independent. After any landscape-changing answer —
    a repeal, an adoption, a new principle the owner states mid-stream — sweep the
    session's earlier rulings for premise collisions and surface hits immediately
    for explicit re-decision. Record reversals as REVISED in the decision log,
    never by editing history invisibly.

## Charter rules (fix-charters; obs #45/#47/#52)

14. A charter that cites a sealed architecture/contract artifact verifies the
    artifact's frozen file hashes against the live tree FIRST; on mismatch, the
    charter reconciles explicitly which parts remain binding (usually invariants)
    and which are superseded by merged reality (usually shapes and naming). Frozen
    hashes are the artifact's built-in staleness detector.
15. A charter mandating tests that need isolation (own DB/fixtures/partition)
    either names the existing partition they belong to or explicitly authorizes
    extending the harness additively ("existing partitions' semantics untouched;
    the final gate may only get stricter") — never force the implementer to choose
    between scope violation and unsafe test placement.
16. Every defect section in a fix charter carries (a) the raw oracle evidence
    inline, (b) the mechanism hypothesis explicitly labeled falsifiable ("verify
    against source, cite what you find"), and (c) named oracles. Implementers
    verify instead of re-diagnosing, and wrong hypotheses get corrected instead of
    baked in.

## Observation-derived rules (folded from OPEN observations at skill creation, 2026-08-11)

17. Never author a custom "Other"-style option inside an option list; the tool's
    built-in Other captures typed text, a hand-rolled one returns only its label —
    a dead-end selection that captures no data (obs #77).
18. Environmental/ops findings recorded in a blueprint (a broken feed, a missing
    service, a quota state) are stamped with their observation date and marked
    "re-probe at execution" — a spec is authoritative about decisions, not about
    the environment (obs #78).
19. When a question proposes a STRUCTURE (taxonomy, carving, hierarchy), the
    evidence base is three-layered in priority order: (1) what the product IS
    (archetype / jobs-to-be-done), (2) verified inventory (code probe) as a
    completeness check, (3) status artifacts only as freshness input, never as the
    skeleton (obs #95).
20. Naming convention by structural layer: containers/areas get NOUNS,
    deliverables/promises get SENTENCES; flag any proposal violating it. Dialog
    ratification of visual/structural artifacts is provisional — schedule the
    first-live-look as an explicit revision checkpoint (obs #96).

## Blueprint

One per complaint — a session normally carries one; when a session surfaces ≥2
INDEPENDENT complaints (no blocking relation between the deliverables), each
gets its own blueprint and its own single-issue two-phase publication; never a
manufactured parent (owner-codified 2026-08-13 from the HOME-108/109
deviation). Each blueprint doubles verbatim as its Linear issue description.
Drafted in the session-local scratch (`local://intake-{slug}.md`) for the lint pass ONLY —
after publication returns success:true, the Linear issue is the sole record
(routing law: local artifacts don't survive and must not shadow the issue;
never write blueprints to durable paths, and never to `.omc/*` — that is an
OMC-era home, obs #107). Sections, in order:

1. **Problem** — what's wrong today, in the owner's domain language.
2. **Solution** — the settled shape, one paragraph.
3. **Entities & Rules** — CONDITIONAL: include only when the session minted ≥1
   new noun. Every node, edge, and constraint from the entity graph, one bullet
   each. Constraints live ONLY here; Decisions reference them.
4. **Decisions** — one line per decision + the question that settled it. Owner
   overrules recorded as such.
5. **Acceptance criteria** — checkboxes; wherever a criterion asserts live state
   it carries its probe command + expected result (probe doctrine rule 7 applies
   at write time).
6. **Out of scope** — explicit exclusions.
7. **Deferred & assumptions** — every budget-surviving gap, categorized:
   [scheduled review] · [assumption] · [deferred design]. Environmental claims
   date-stamped per rule 18.
8. **Coverage table** — the 11 taxonomy categories with final status each.

EXCLUDED from blueprints, always: user stories, testing decisions, file paths,
code snippets (single exception: a schema or state machine that itself encodes a
decision), Q&A transcript, task breakdown, scores of any kind.

## Lint (deterministic pre-publish gate)

No side effects before lint passes — the ontology lives at the ledger. Check:

- [ ] Every acceptance criterion asserting live state is probed (result recorded)
      or explicitly hedged ("unverified — probe at build").
- [ ] Every Deferred item carries a category tag.
- [ ] Every noun used in acceptance criteria is defined in Entities & Rules (or
      the section is legitimately absent because no new noun was minted).
- [ ] Target Linear project exists — probe xd://linear, don't assume.
- [ ] Blocking edges among proposed issues are acyclic.
- [ ] The visible scan was shown before the session's first question (a
      no-question session still shows it).
- [ ] Every asking-you item was asked, or the owner's skip is recorded
      verbatim in Decisions.
- [ ] Nothing in figured-out-myself is a judgment call — each line cites the
      code or record that settled it.
- [ ] Payload names concrete records in an external system (projects,
      milestones, labels) → one live exemplar of each record type probed and
      the term→type mapping confirmed BEFORE the preview (obs #108).
- [ ] Any criterion quantified over N written records ("every issue…") gets an
      aggregate readback probe (fetch all N, audit, report the miss count)
      before being checked (obs #109).

On lint failure: report the failing checks, fix the blueprint (asking at most one
follow-up question if a fix needs owner input, budget permitting), re-lint. Never
publish a failing blueprint.

## Publication (two-phase, via xd://linear)

- **Single issue** (default): `create_issue` with the blueprint as description.
  Two-phase: first call returns the payload preview — show it to the owner
  verbatim, including that the issue becomes NOW; repeat with `confirm:true`
  only after his yes.
- **Conditional split**: only when ONE complaint's blueprint decomposes into ≥2
  independently verifiable slices that share blocking relations — the split is
  a decomposition of a single deliverable, never a bundling of independent
  complaints (those each get their own single-issue publication; Blueprint
  section). Parent issue holds the blueprint; children hold the slices
  with NATIVE Linear blocking links (never text-described edges); ONE batch
  preview = one owner yes for the whole set. GATED: before attempting any split
  publish, read the xd://linear device doc and confirm `create_issue` exposes
  parent/blocks/batch params (charter: ~/.omp/agent/charters/linear-batch-publish.md).
  If the params are absent, publish the single-issue path and record the split as
  deferred on the issue.
- Never assume a write landed without `success:true`.

Intake ends here. Native publication selects the first issue or batch parent as
NOW; do not ask Chris to select it again. Do not start building inside intake —
the next owner action is `/plan`.

## Optional feasibility stage (owner-defined process, 2026-07-01)

When the blueprint contains technical-feasibility claims nobody has verified
(external APIs, architecture assumptions), offer a validation pass over the risky
claims BETWEEN blueprint approval and publication. Interview output alone never
green-lights unverified architecture.

## Unchanged laws

Plain language throughout (no jargon; "I don't understand" = full stop, re-explain
and re-ask). Explore the codebase instead of asking anything the code can answer
— facts only; judgment calls are always asked (see Visible scan). No Linear
writes before the lint passes AND the owner explicitly confirms the two-phase
preview.

## Scheduled review (from HOME-43 blueprint; survives its close)

Review #1 done 2026-08-13 (sessions 1–5: HOME-44, HOME-55, backlog backfill
HOME-57..103, HOME-106, HOME-108+109): (a) Entities & Rules KEPT — probed
HOME-44/55/106/109; every section carries real nodes/edges/constraints, none
degraded to prose; (b) budget default 7 KEPT — observed sessions asked 1–5
questions, the cap never tripped; (c) owner-codified independent
multi-complaint publication (Blueprint section); (d) added the BINDING visible
scan (HOME-108; owner revised the numeric-floor design at execution); (e)
folded obs #103 (already present), #108, #109 into the lint gate. Next review
after ~5 more real intake sessions (count: 7 as of 2026-08-15 — HOME-111; HOME-112; HOME-123; HOME-130; HOME-131; HOME-136; HOME-137). Increment the
count line here at the end of every real intake session.
