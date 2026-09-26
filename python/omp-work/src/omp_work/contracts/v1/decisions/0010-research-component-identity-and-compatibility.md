# 0010 — Research component identity and campaign compatibility manifest (R02-S3b)

## Problem

1. Research evaluator, environment, policy, worker, audit, and release hashes were unanchored caller strings, allowing callers to assert qualification or trust merely by submitting an arbitrary hash.
2. Campaigns lacked an owner-bound compatibility manifest enumerating accepted component fingerprints across required research axes.
3. Component declarations and compatibility manifests required canonical content-addressing and immutability to prevent qualification fabrication, configuration drift, or unauthorized post-admission mutation.
4. Legacy campaigns admitted prior to compatibility manifest enforcement required continuous readability and lifecycle drivability without gaining unearned compatibility or permitting new trial proposals.

## Decision

1. **Canonical research component descriptor and registry.** Components are declared via `ResearchComponentDescriptor` and registered immutably in workspace registry table `omp_research.components`. Registration declares identity only and confers no qualification, capability, or execution authority. A descriptor's content strictly defines its content-addressed `component_sha256 = sha256(descriptor)`.
2. **Owner-bound campaign compatibility manifest.** Campaign admission (`admit_research_campaign`, requiring `work.approve`) binds a required `ResearchCompatibilityManifest` and its recomputed `compatibility_sha256`. Admission validates that the policy component and all manifest-referenced component fingerprints exist and match their declared component kind.
3. **Immutability and legacy campaign invariants.** Campaign compatibility manifests are immutable once set. Legacy campaigns admitted prior to migration 0027 retain `NULL` compatibility and remain operable for existing lifecycle transitions (e.g. pause, resume, conclude, cancel), but are permanently refused for new trial proposals and cannot gain a manifest post-admission.
4. **Trial proposal compatibility enforcement.** `propose_research_trial` verifies that the campaign has a compatibility manifest and that trial evaluator and environment fingerprints are explicitly listed in the manifest. Unlisted identities and legacy campaigns fail closed with `stale_evidence`.
5. **ResearchView projection.** `ResearchView` collects and returns declarations referenced by visible campaigns without broadening authorization boundaries.
6. **Authority boundaries and deferred runtime execution.** Registration executes under `work.execute`. Admission binds compatibility under `work.approve`. Runtime worker routing, execution leasing, worker handshakes, and audit/release receipt settlement remain deferred to E1/R03/R06.

## Consequences

- Database schema migration `0027_research_component_identity.sql` adds `omp_research.components`, extends `omp_research.campaigns` with compatibility columns, and enforces immutability via triggers.
- Contract digest updates; client bindings in `packages/work-client` reflect the extended types, commands, and results.
