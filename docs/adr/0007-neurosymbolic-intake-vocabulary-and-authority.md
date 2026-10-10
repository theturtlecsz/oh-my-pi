# ADR 0007: Neurosymbolic intake vocabulary and authority

**Status:** Accepted. Schemas target `python/omp-work/src/omp_work/contracts/nsi/v1/`
**Date:** 2026-10-10

## Context

Mandate section 15 (preserve advanced capabilities) keeps neurosymbolic intake
as a platform capability behind the mission interface. Owner decision D23
requires intake to extract and confirm intent for every mission. The bounded
P12 path (`BoundedIntakeDraft`) proves a minimal intake loop on main, but its
taxonomy, vocabulary, status model, and authority rules live only in code.
NSI-1 extracts those rules beyond the bounded path into versioned schemas and
ADRs, so the intake model can evolve under contract versioning instead of code
edits. The existing `BoundedIntakeDraft` stays functional. NSI-1 supplies the
versioned schema and the compatibility mapping to it.

Relational persistence (NSI-2), divergent exploration (NSI-E1–E4), external
solver workers, automated prompt-rewriting, and the interactive `/intake` UI
rewrite are deferred.

## Decision

### 1. Intake categories

Every intake record classifies its content under exactly the eleven
categories below. The taxonomy is canonical. New categories require a new schema version.

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
- **Claims** — statements about entities or edges, each with evidence
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
`stale` → a material source, rule, schema, or contract change invalidates
the current assessment and receipts.
`invalid` → the rule engine derives an `INVALID` failure assessment.
`superseded` → a newer ratified revision replaces this record.

The normative state machine is in
`03_NEURO_SYMBOLIC_INTAKE_TECHNICAL_CONTRACT.md`, section 7.
`02_NEURO_SYMBOLIC_INTAKE_COMPILER_INTAKE.md`, steps 10 to 13,
defines revalidation, publication, projection, and invalidation.

Allowed transitions:

| From | To | Actor |
| --- | --- | --- |
| collecting | reasoning | rule engine |
| reasoning | needs_spec | rule engine |
| reasoning | conflicted | rule engine |
| reasoning | invalid | rule engine |
| reasoning | ready_for_ratification | rule engine |
| needs_spec | collecting | owner |
| conflicted | collecting | owner |
| ready_for_ratification | collecting | owner |
| ready_for_ratification | ratified | owner |
| ratified | needs_spec | rule engine |
| ratified | conflicted | rule engine |
| ratified | invalid | rule engine |
| ratified | published | owner |
| collecting, reasoning, needs_spec, conflicted, ready_for_ratification, ratified, published, invalid | stale | rule engine |
| stale | collecting (new revision) | owner |
| ratified, published, stale | superseded | owner (via a new ratified revision) |

The owner can reject `ready_for_ratification` and return to `collecting`.
After ratification, the rule engine revalidates the exact semantic closure
and expected revision. A failed revalidation returns the run to `needs_spec`,
`conflicted`, `invalid`, or `stale`, with the applicable typed failure assessment.

A material change invalidates affected derivations and receipts in any current
state. The `stale` transition to `collecting` creates a new revision.
It never resumes reasoning on the stale record. A new ratified revision
supersedes the old revision. The engine never repairs or reuses an old receipt.

Readiness and ratification are strictly separated:

- `READY_FOR_RATIFICATION` is a pre-ratification readiness assessment. The
  rule engine derives it; it is not owner approval.
- Owner ratification is the only transition into `ratified`, and only the
  owner performs it.
- `ELIGIBLE_FOR_PUBLICATION` is a post-ratification positive readiness
  receipt. The rule engine issues it only for a ratified record.
- The publication receipt records the owner as `publishedBy`. The authenticated,
  idempotent `publishIntake` transition requires matching ratification and
  `ELIGIBLE_FOR_PUBLICATION` on the same current revision.

No path reaches `published` without owner ratification in between.

### 4. Authority matrix

| Actor | May | May not |
| --- | --- | --- |
| Owner | Supply intent, make decisions, grant waivers, reject, ratify, publish, supersede | — |
| Repository/tool | Contribute observations and evidence spans with source provenance | Admit claims, decide readiness, ratify |
| Model | Propose entities, edges, claims, and category assignments | Admit claims, waive gates, become canonical state |
| Rule engine | Own schemas, admit evidence-backed claims, derive deterministically, assess readiness | Waive gates, ratify, publish as owner, override owner decisions |
| Markdown projection | Render canonical state | Be reparsed into canonical state |

The model proposes. It never admits. A proposal becomes a claim only through
the rule engine with evidence, or through the owner. Rendered Markdown is a
projection: the canonical state is the versioned schema record, and no tool
may parse the Markdown back into state.

### 5. Unknown semantics

Intake reasoning is open-world and three-valued: every claim is true, false,
or unknown. Missing evidence yields unknown, not false. Unknown is not false:
the rule engine never treats an absent claim as a refuted one. Readiness
derivation counts required unknowns as gaps, which drive `needs_spec`.

### 6. Waiver policy

Only the owner grants a waiver. Each waiver identifies the owner and exact
premise, states an explicit reason, and records the decision revision.
The waiver applies only to the specified record revision.

Critical gaps cannot be waived. This rule includes critical security,
migration, destructive, compatibility, and product-semantic gaps.
A category name or lifecycle state does not determine whether a gap is critical.
For other gaps, the versioned ontology specifies `never` or `owner_with_reason`.
A completeness score cannot hide a critical gap.

### 7. Client contract conformance

Intake capabilities reach callers through the client contract as
`mission.intake`, under mandate section 3. No new CLI commands are added for
NSI-1; `/intake` remains an internal/debug operation.
