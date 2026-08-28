# Intake situational rules (9–12, 14–16, 19–20)

Loaded on demand from `SKILL.md` §Standing rules; numbering continues the in-skill sequence.

Record-writing:

9. Session-record blocker/remaining lists are live-state claims: re-run the cheap read-only probe for each at record-write time — blockers clear silently.

Research sessions:

10. Landscape/provider research closes with a date pass: extract every deprecation, shutdown, or retirement date; diff each against today; live-probe every lane whose critical date has passed or falls inside the decision horizon. Probe output replaces doc claims in the memo — documentation states launch-time truth, only a live probe states current truth.
11. A faithful lane probe replicates the caller's WHOLE invocation contract — user identity, environment (including cleared variables), working directory, stdin shape, exact arguments. Each omitted dimension fabricates its own misleading failure. A lane is "dead" only if it fails under the full contract.

Estate walks:

12. Before any estate-wide review/reorg session, probe the target trees for concurrent workers (running lanes, fresh uncommitted mtimes, lock/handoff artifacts); sequence with the owner if found — a reorg is a long read-then-mutate transaction.

Fix-charters:

14. A charter citing a sealed architecture/contract artifact verifies the artifact's frozen file hashes against the live tree FIRST; on mismatch, reconcile explicitly which parts remain binding (usually invariants) and which are superseded by merged reality (usually shapes and naming).
15. A charter mandating tests that need isolation (own DB/fixtures/partition) names the existing partition they belong to or explicitly authorizes additive harness extension ("existing partitions' semantics untouched; the final gate may only get stricter") — never force a choice between scope violation and unsafe test placement.
16. Every defect section in a fix charter carries (a) the raw oracle evidence inline, (b) the mechanism hypothesis explicitly labeled falsifiable ("verify against source, cite what you find"), and (c) named oracles — implementers verify instead of re-diagnosing.

Structure questions:

19. A question proposing a STRUCTURE (taxonomy, carving, hierarchy) layers evidence in priority order: (1) what the product IS (archetype / jobs-to-be-done), (2) verified inventory (code probe) as completeness check, (3) status artifacts as freshness input only — never the skeleton.
20. Naming by structural layer: containers/areas get NOUNS, deliverables/promises get SENTENCES; flag violations. Dialog ratification of visual/structural artifacts is provisional — schedule the first-live-look as an explicit revision checkpoint.
