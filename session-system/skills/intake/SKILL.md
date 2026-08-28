---
name: intake
description: Socratic intake — turn a vague idea, an existing plan/draft, or a finished blueprint into a published Work Ledger item. Use for /intake, "intake this", stress-testing or grilling a plan before building, deep interviews, or spec re-baselines. Supersedes grilling, deep-grill, and deep-interview for owner-side intake; ends at the published work item — execution lanes pull from the ledger.
---

# /intake — Socratic intake → Work Ledger blueprint

Internal owner doctrine (HOME-43). Pipeline: taxonomy scan → budgeted interview + entity graph → blueprint → lint → two-phase publication. Execution OUT of scope: intake ends at owner-confirmed publication — first published item (or batch parent) becomes NOW inside it, no second prompt. Every rule is host-checkpointed; failure → fix, re-check, never proceed.

## Entry seams

1. **Blank idea** — full pipeline.
2. **Existing plan/draft** — parse into pre-filled blueprint + entity graph; questions target only what the draft leaves open.
3. **`--publish <blueprint>`** — skip interview and scan; lint → publication.

Not a seam: published-issue work plans in its executing session, never intake.

## Engine

**What to ask** — scan state (idea/draft/graph) against the fixed taxonomy; each scores Resolved/Clear(N/A+reason)/Partial/Open; Open+Partial → candidates. Vocabulary fixed — never rebuilt/renamed/extended:

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

**What order** — never ask before its dependency's answer exists; rank unlocked by impact × uncertainty; name the dependency in the "why now" line.

**When to stop** — hard cap **7 questions**/session (owner may reset). Stop: budget spent AND no dangling edges AND every asking-you item asked/owner-skipped — or owner exits. Survivors → Deferred & assumptions, never dropped.

### Visible scan (BINDING)

BEFORE the first question: standalone assistant message, ZERO tool calls that turn (co-emitted text doesn't count). Host blocks `ask` and publication until it lands. Three lists:

- **Figured out myself** — facts settled by code/records, each citing its source; judgment calls here = violation.
- **Asking you** — every surfaced judgment call; these ARE the questions. BINDING: each asked unless owner skips, skip recorded verbatim in Decisions.
- **Leaving for later** — deliberately parked → Deferred & assumptions.

Jargon-free: name the thing ("what happens when a recording fails"), never categories/paths/symbols. Renders on every interviewing seam, even with nothing to ask. Existing-plan seam: draft judgment calls enter as confirm-or-overturn. NO quota: facts looked up, judgment calls asked.

### Entity graph

Beside the draft blueprint. Every answer adds exactly one: **node** (defined noun — entity/state/actor/artifact) | **edge** (typed relation: has/uses/blocks/becomes) | **constraint** (exactly-one|exclusive|allowed-values|transitive; templates derive mechanically: two-Ys? · ever-both? · closed-list? · transitive?).

**Dangling edge** — a referenced noun no node defines — IS an open question; blocks the stop condition.

### Question mechanics

AskUserQuestion, ONE decision per dialog, answer awaited — never bundle; ratifications confirm component-by-component. Recommended option first: "(Recommended)" + one-line reason. Explorable facts get explored, never asked; judgment calls never self-answered — they enter asking-you.

## Pre-send gate (G1–G6)

Owner decides from INSIDE the dialog, never prose above it. Check all six before ANY AskUserQuestion; failure → fix first, no exceptions:

- **G1 Data, with a ceiling** — deciding data (verbatim wording, numbers, evidence) INSIDE the dialog, ONLY that; a fact enters only if it could change the answer; >~3 facts → split. Overflow → option `preview` fields. Deciding data missing → re-ask with it embedded, never bank.
- **G2 Jargon** — every term plain or defined in-sentence.
- **G3 Don't-understand debt** — unresolved "I don't understand" → re-explain, re-ask FIRST, progress blocked; then re-confirm the round's other answers.
- **G4 Ack** — previous answer acknowledged in one line first.
- **G5 Neutrality** — scenarios symmetric; no advocacy beyond the recommended option's one reason; no safety/compliance flavor in personal projects.
- **G6 Claims probed** — live-capability claims + safety facts easing destructive options: probed before send, result quoted in-dialog; deletion targets diffed against the claimed surviving copy; unprobeable → hedge ("may exist — unverified").

## Standing rules

Live-state claims: probe when WRITTEN, not when acted on.

7. Live-state acceptance criteria: cheap probe BEFORE owner approval; at checklist seal, audit assumed capabilities against the live tree — misses become chartered items.
13. Landscape-changing answer → sweep prior rulings for premise collisions; re-decide; reversals = REVISED, never invisible.
17. No custom "Other" option — built-in captures typed text, hand-rolled nothing.
18. Environmental findings: date-stamped "re-probe at execution"; specs settle decisions, not environment.

Research, estate-walk, fix-charter, structure-question, and record-writing sessions: first read `skill://intake/references/session-rules.md` (rules 9–12, 14–16, 19–20).

## Blueprint

One per complaint; doubles verbatim as the work-item description. ≥2 INDEPENDENT complaints (no blocking relation) → EACH its own blueprint and single-issue publication, never a manufactured parent — decomposition mechanical, never an owner question. Drafted at `local://intake-{slug}.md` for lint ONLY; after success:true the work item is sole record — never durable paths/`.omc/*`. Sections in order:

1. **Problem** — what's wrong, owner's domain language.
2. **Solution** — settled shape, one paragraph.
3. **Entities & Rules** — when ≥1 new noun minted; node/edge/constraint bullets; constraints ONLY here.
4. **Decisions** — one line + settling question each; overrules recorded as such.
5. **Acceptance criteria** — checkboxes; live-state criteria carry probe command + expected result (rule 7).
6. **Out of scope**.
7. **Deferred & assumptions** — surviving gaps tagged [scheduled review]·[assumption]·[deferred design]; environmental claims date-stamped (rule 18).
8. **Coverage table** — 11 categories, final status each.

EXCLUDED always: user stories · testing decisions · file paths · code snippets (exception: a decision-encoding schema/state-machine) · Q&A transcript · task breakdown · scores.

## Lint (pre-publish gate)

No side effects before lint passes. Check all:

- Blueprint saved to `local://intake-{slug}.md`.
- `create_work.description` byte-for-byte from the artifact; post-lint edits → re-save, re-lint.
- Deliverables lacking native blocking edges → own single-issue blueprints.
- Live-state criteria probed (result recorded) or hedged "unverified — probe at build" — hedging ONLY for genuinely expensive probes (owner/device/money/long-setup); cheap probes run now.
- Every Deferred item tagged.
- Every criterion noun defined in Entities & Rules (or section absent).
- Target surface probed via `tree`; else phase-1 `create_work` preview (writes nothing) = existence probe.
- Categorical mappings exercised on the most important live exemplar, rendered line shown; falsified at execution → surfaced conflict + parked decision, never silent change.
- Batch blocking edges acyclic; every child in ≥1 edge.
- Scan shown before the first question (even no-question sessions).
- Every asking-you item asked or skip recorded verbatim in Decisions.
- Figured-out-myself: facts only, each citing its source.
- Named external records (projects/milestones/labels): one live exemplar per type probed, term→type confirmed BEFORE preview.
- Criteria over N records → aggregate readback (fetch all N, report misses) first.

Failure → report failing checks, fix (≤1 owner follow-up, budget permitting), re-lint; never publish failing.

## Publication (two-phase)

Host refuses missing/stale artifacts, byte drift, multi-deliverable single issues, unlinked batch children.

- **Single issue** (default): `create_work`, description byte-exact from the artifact; preview shown verbatim (item becomes NOW); `confirm:true` only after owner yes.
- **Conditional split** — ONE complaint, ≥2 independently verifiable slices with blocking relations (never bundled independents): parent = blueprint byte-exact; children carry NATIVE `blocks` links (text never substitutes), every child ≥1 edge; ONE batch preview, one owner yes.
- No `success:true` → not landed.

Unverified feasibility claims → offer pre-publication validation; interview output never green-lights architecture.

Intake ends here: publication selects NOW — never re-ask, never start building; next `/plan`.

## Laws & review

Plain language; "I don't understand" = full stop → re-explain, re-ask. Review after ~5 more real intake sessions; after each, increment `~/.agents/skill-observations/intake-session-count.txt` (`count: N as of YYYY-MM-DD — <keys>`) — counter deliberately outside this staged-review file.
