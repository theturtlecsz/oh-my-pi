# Mission Typed Intake

This document describes how mission orchestration integrates with typed intake authority.

## Head Revision and Persisted PASS

A mission requires a persisted PASS assessment receipt at the exact head revision of its typed intake. Admission at the planning stage verifies that the current intake head revision has an approved PASS assessment. Without a genuine, persisted PASS receipt matching the exact head revision and content hash, the mission is refused and cannot enter the planning pipeline.

## Append-Only Mission Bindings

Mission intake bindings are stored in the database (`omp_work.mission_intake_bindings`) as append-only records:

- When a mission is admitted, a binding row links the mission to the admitted intake revision and assessment receipt id in the `admitted` state.
- When an intake revision advances (such as when revision $N+1$ is appended), open bindings for that mission are transitioned to `held` with the hold code `intake_revision_advanced`.
- Older binding rows stay in the table to preserve immutable provenance history; rows are never deleted or overwritten in place.

## Stage Verification at Start and Success Recording

Every orchestrator stage verifies intake authority at two points:

1. **At stage start**: Before stage operations begin, the orchestrator inspects the current intake head against the admitted binding. If the head has advanced beyond the bound revision and lacks an approved PASS for the new head, the stage is refused immediately and held without executing any stage effects.
2. **Before success is recorded**: When an operation completes, the orchestrator re-reads the intake head to confirm it did not change during the operation. If an intake revision was appended concurrently while the operation ran, the success is refused and held rather than recorded.

## Re-Binding on Head PASS

When an intake revision has advanced to head $N+1$, the mission remains held until a persisted PASS assessment for that exact head revision is recorded. Once the current-head PASS exists and the hold is resumed, the next stage re-binds the mission to the new revision and receipt under the same mission id. Historical bindings for earlier revisions remain preserved in the binding history.
