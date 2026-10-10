# ADR 0007: Neurosymbolic intake vocabulary and authority

**Status:** Accepted; schemas target `python/omp-work/src/omp_work/contracts/nsi/v1/`
**Date:** 2026-10-10

## Context

Mandate section 15 (preserve advanced capabilities) keeps neurosymbolic intake
as a platform capability behind the mission interface. Owner decision D23
requires intake to extract and confirm intent for every mission. The bounded
P12 path (`BoundedIntakeDraft`) proves a minimal intake loop on main, but its
taxonomy, vocabulary, status model, and authority rules live only in code.
NSI-1 extracts those rules beyond the bounded path into versioned schemas and
ADRs, so the intake model can evolve under contract versioning instead of code
edits. The existing `BoundedIntakeDraft` stays functional; NSI-1 supplies the
versioned schema and the compatibility mapping to it.

Relational persistence (NSI-2), divergent exploration (NSI-E1–E4), external
solver workers, automated prompt-rewriting, and the interactive `/intake` UI
rewrite are deferred.

## Decision

### 1. Intake categories

Every intake record classifies its content under exactly the eleven
categories below. The taxonomy is canonical; new categories require a new
schema version.

1. `functional_scope_and_behavior`
2. `domain_and_data_model`
3. `interaction_and_ux_flow`
4. `non_functional_qualities`
5. `integrations_and_external_dependencies`
6. `edge_cases_and_failure_handling`
7. `constraints_and_tradeoffs`
8. `terminology`
9. `completion_signals`
10. `placeholders_and_ambiguities`
11. `scope_boundaries`

### 2. Vocabulary

The intake graph has six term types:

- **Entities** — named things in the problem space (actors, data types,
  screens, external systems).
- **Edges** — typed relations between two entities.
- **Claims** — statements about entities or edges, each carrying evidence
  spans and a truth value under section 5.
- **Constraints** — requirements that candidate solutions must satisfy,
  classified under one of the eleven categories.
- **Evidence spans** — byte ranges into a source artifact that ground a claim.
- **Source provenance** — the artifact, revision, and actor each evidence
  span came from.

### 3. Status model

An intake record moves through ten lifecycle states:

`collecting` → evidence and claims accumulate.
`reasoning` → the rule engine derives coverage and conflicts.
`needs_spec` → required categories are open; owner input is required.
`conflicted` → two admitted claims contradict each other.
`ready_for_ratification` → deterministic readiness derivation passes.
`ratified` → the owner ratified the record.
`published` → the record is visible to downstream planning.
`stale` → underlying evidence or the mission revision changed after
publication.
`invalid` → the record has a fundamental defect and is withdrawn.
`superseded` → a newer ratified revision replaces this record.

Allowed transitions:

| From | To | Actor |
| --- | --- | --- |
| collecting | reasoning | rule engine |
| reasoning | needs_spec | rule engine |
| reasoning | conflicted | rule engine |
| reasoning | ready_for_ratification | rule engine |
| needs_spec | collecting | owner |
| conflicted | collecting | owner |
| ready_for_ratification | ratified | owner |
| ratified | published | rule engine |
| published | stale | rule engine |
| stale | reasoning | rule engine |
| collecting, reasoning, needs_spec, conflicted, ready_for_ratification | invalid | owner |
| ratified, published | superseded | owner (via a new ratified revision) |

Readiness and ratification are strictly separated:

- `READY_FOR_RATIFICATION` is a pre-ratification readiness assessment. The
  rule engine derives it; it is not owner approval.
- Owner ratification is the only transition into `ratified`, and only the
  owner performs it.
- `ELIGIBLE_FOR_PUBLICATION` is a post-ratification positive readiness
  receipt. The rule engine issues it only for a ratified record.
- The publication receipt records the actual move into `published`. It
  requires `ELIGIBLE_FOR_PUBLICATION` on the same revision.

No path reaches `published` without owner ratification in between.

### 4. Authority matrix

| Actor | May | May not |
| --- | --- | --- |
| Owner | Supply intent, make decisions, grant waivers, ratify, invalidate, supersede | — |
| Repository/tool | Contribute observations and evidence spans with source provenance | Admit claims, decide readiness, ratify |
| Model | Propose entities, edges, claims, and category assignments | Admit claims, waive gates, become canonical state |
| Rule engine | Own schemas, derive deterministically, assess readiness, publish eligible records | Waive gates, override owner decisions |
| Markdown projection | Render canonical state for reading | Be reparsed into canonical state |

The model proposes; it never admits. A proposal becomes a claim only through
the rule engine with evidence, or through the owner. Rendered Markdown is a
projection: the canonical state is the versioned schema record, and no tool
may parse the Markdown back into state.

### 5. Unknown semantics

Intake reasoning is open-world and three-valued: every claim is true, false,
or unknown. Missing evidence yields unknown, not false. Unknown is not false:
the rule engine never treats an absent claim as a refuted one. Readiness
derivation counts required unknowns as gaps, which drive `needs_spec`.

### 6. Waiver policy

Only the owner grants a waiver. Each waiver names the gap, states an explicit
reason, and pins the record revision it applies to; a waiver dies with its
revision. Critical gaps cannot be waived: completion signals, scope
boundaries, and unresolved contradictions (`conflicted`) always block
ratification regardless of waivers.

### 7. Client contract conformance

Intake capabilities reach callers through the client contract as
`mission.intake`, under mandate section 3. No new CLI commands are added for
NSI-1; `/intake` remains an internal/debug operation.
