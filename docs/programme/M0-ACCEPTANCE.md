# Autoresearch M0 Acceptance Record: Native Foundation

- **Milestone**: Autoresearch M0 — Native Foundation (Section 23 of `docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md`)
- **Required Packages**: R00–R04 plus applicable ECC/stabilization prerequisites
- **Task**: `OMP-314` (Slice: `OMP-314-s01`)
- **Promised Outcome**: Reproducible source/runtime identity and durable research contracts/jobs
- **Evaluation Commit**: `8e219f7bc1e97ae3e00a40c5081d2733e8459ca1` (`main`)
- **Status**: Accepted — all required packages landed on `main` and verified by test suites

---

## 1. Package Deliveries and Evidence

### R00 — Reconcile Baseline and Native Ownership
- **Ledger Key**: `OMP-320`
- **Merge Commits on `main`**:
  - `f6e8384f5f`: `Merge pull request #88 from theturtlecsz/flood/batch-48`
  - `b8746dcd16`: `OMP-320: Autoresearch R00 baseline reconciliation and inventory`
- **What It Delivered**:
  - `docs/programme/R00-BASELINE-2026-09-25.md`: complete baseline reconciliation record distinguishing source, installed, configured, discovered, enabled, and invoked identities.
  - Runtime and installed identities audit (installed `omp` binary, WorkService daemon, and active candidate development trees).
  - Status audit of ECC work packages (WP1 to WP8) evaluated against `docs/programme/ECC-IMPLEMENTATION-SPEC.md` section 6 with evidence paths.
  - Interface inventory covering shared execution, trusted runners, deployment authorities, and WebUI.
  - Model, tool, and memory capability inventory across local services and configured provider routes.
  - Research donor source and license inventory.
  - Missing prerequisite and gap ownership crosswalk mapping all downstream milestones (`OMP-321` through `OMP-325`).
- **Proving Test / Verification**:
  - Upstream fork-behavior guardrail (`bun scripts/upstream-inventory.ts`) verifying baseline document integration.
  - Read-only live runtime state audit in `docs/programme/R00-BASELINE-2026-09-25.md` Section 1.

### R01 — Complete the ECC Engineering and Research Packs
- **Ledger Key**: `OMP-321`
- **Merge Commits on `main`**:
  - `f338b9d809`: `Merge pull request #93 from theturtlecsz/flood/batch-53`
  - `c12fe6fb9b`: `OMP-321: pin ECC mirror with deterministic engineering and research packs`
  - `ad9e01a01a`: `OMP-321: reconcile derived rows after rebase (flood merge_check_repair)`
- **What It Delivered**:
  - Full pinned mirror of upstream ECC assets under `session-system/ecc/mirror/`.
  - Deterministic OMP adaptation engine (`session-system/ecc/adapter/`).
  - Pack manifests defining engineering and research packs (`session-system/ecc/manifest.json`).
  - Pinned lock file with source and transformed SHA-256 hashes (`session-system/ecc/adapted.lock.json`).
  - Native-preserving overlays and link-rewriting (`session-system/ecc/overlays/`).
  - Pack install, update, remove, and native discovery mechanisms (`session-system/ecc/adapter/apply.ts`, `catalog.ts`).
  - Safe database-reviewer adaptation with dropped execution authority and candidate-execution refusal preservation.
  - Satisfies ECC WP1 to WP3 prerequisites.
- **Proving Test Files**:
  - `session-system/tests/ecc-install.test.ts`: proves install, update, remove, non-overwrite of unowned files, modified-file protection, and discovery removal.
  - `session-system/tests/ecc-adaptation.test.ts`: proves source hash pinning, lockfile verification, byte-identical builds, manifest invariants, overlay application, and native discovery.

### R02 — Define Campaign, Trial, Evidence, and Policy Contracts
- **Ledger Key**: `OMP-322`
- **Merge Commits on `main`**:
  - `3327c0af83`: `Merge pull request #112 from theturtlecsz/flood/batch-72`
  - `2676817890`: `Merge pull request #111 from theturtlecsz/flood/batch-71`
  - `8815d99d14`: `Merge pull request #110 from theturtlecsz/flood/batch-70`
  - `0dbf273f61`: `Merge pull request #109 from theturtlecsz/flood/batch-69`
  - `f3f7e55afe`: `Merge pull request #105 from theturtlecsz/flood/batch-65`
  - Landed slices: `1c502e0c02` (s07 client bindings), `50fd6d95bb` (s06 rules & identity-trust tests), `a5989a7d8a` / `6affbad543` (s04 persistence & manifests), `110400fc15` / `fb704a921a` (s02-s02 campaign lifecycle), `726d8c8e6c` / `50ef955015` (s02-s01-s01 record-only contracts), `ab3108d583` / `833f4d226c` / `a6f756854d` (s02-s03 types & work-client reads).
- **What It Delivered**:
  - Canonical R02 JSON schemas under `python/omp-work/src/omp_work/contracts/r02/` (`campaign.schema.json`, `trial.schema.json`, `evidence.schema.json`, `policy.schema.json`) and validator `validate.py`.
  - WorkService contract operations, state transitions, and validation rules for research campaigns, trials, observations, and deliverable bindings.
  - Enforcement of "identity is not trust": research IDs cannot masquerade as native candidates, and execution status cannot substitute scientific qualification.
  - Client bindings generation (`packages/work-client/scripts/generate-research-bindings.ts` and `packages/work-client/src/research.generated.ts`).
  - WorkClient typed API integration (`packages/work-client/src/index.ts`).
- **Proving Test Files**:
  - `packages/work-client/test/research-bindings.test.ts`: proves generated client bindings match canonical schemas and detect enum drift.
  - `packages/work-client/test/research-client.test.ts`: proves client dispatch, response decoding, and research view resolution.
  - `python/omp-work/tests/test_research_contract.py`: proves canonical contract schema adherence and serialization rules.
  - `python/omp-work/tests/test_research_rules.py`: proves campaign lifecycle state machines and trial validation guards.
  - `python/omp-work/tests/test_research_identity_trust.py`: proves identity-is-not-trust barriers and receipt integrity.

### R03 — Extend the Shared Execution Foundation
- **Ledger Key**: `OMP-324`
- **Merge Commits on `main`**:
  - `ec99ece619`: `Merge pull request #122 from theturtlecsz/flood/batch-82`
  - `718db33053`: `Merge pull request #121 from theturtlecsz/flood/batch-81`
  - `c564fd0d6d`: `Merge pull request #120 from theturtlecsz/flood/batch-80`
  - `18ad3a8158`: `Merge pull request #119 from theturtlecsz/flood/batch-79`
  - `5be077b247`: `Merge pull request #118 from theturtlecsz/flood/batch-78`
  - `a24828d3c7`: `Merge pull request #117 from theturtlecsz/flood/batch-77`
  - Landed slices: `3a12ab70a0` (s08 real-process proof on omp_jobs), `f963a39e3e` / `9736775467` (s07), `62e8adcb5c` (s06 usage outbox), `44a2f9446f` (s05 usage identities), `d9f1f17323` (s04 cascade cancellation), `40c9ffd49e` (s03 fencing & lease renewal), `36c8188460` (s02 claim & capacity routing), `c843eb20c2` / `20900d3be9` (s01 workspace isolation & API).
- **What It Delivered**:
  - Dedicated native jobs database migrations on `omp_jobs` (`0001_omp_jobs_schema.sql`, `0003_native_research_jobs.sql`).
  - Native research jobs subsystem under `python/omp-work/src/omp_work/jobs/`:
    - Enqueue, lease claiming, capacity routing, and workspace isolation (`admission.py`, `lease.py`).
    - Fenced lease renewal, settlement, and stale-worker prevention (`lease.py`).
    - Cascade cancellation of jobs and non-terminal descendants (`cancel.py`).
    - Stable usage accounting on `omp_jobs.usage_events` (`usage.py`).
    - Outbox delivery and reconciliation with the file ledger (`outbox.py`).
  - Crash and restart recovery for unacknowledged transitions without duplicate external effects.
- **Proving Test Files**:
  - `python/omp-work/tests/test_native_jobs_process_recovery.py`: proves real-process crash/restart recovery, lease fencing, and unacknowledged transition reconciliation.
  - `python/omp-work/tests/test_native_jobs_cancellation.py`: proves cascade cancellation of jobs and descendant tasks with post-cancellation admission refusal.
  - `python/omp-work/tests/test_native_jobs_usage.py`: proves usage accounting, idempotent outbox settlement, and stable event identities.

### R04 — Implement Reproducible Artifact and Source Storage
- **Ledger Key**: `OMP-323`
- **Merge Commits on `main`**:
  - `856be856d8`: `Merge pull request #115 from theturtlecsz/flood/batch-75`
  - Landed slices: `ba8b7c57a8` (verified custody core), `8a35aa01b3` (contract approval), `a9b2cf0ada` (derived rows), `a95da1156f` (migration pins & client tests).
- **What It Delivered**:
  - Database migration `0028_research_artifact_custody.sql` for research sources, datasets, and artifacts.
  - Contract v1 extension in `python/omp-work/src/omp_work/contracts/v1/` (`research-artifact-hash.json`, `0011-research-artifact-custody.md`).
  - Content-addressed custody engine in `python/omp-work/src/omp_work/research/custody.py` and `store.py`.
  - Deterministic hash verification, path traversal escape protection, workspace ACL enforcement, retention, and cache replication boundaries.
  - Typed client methods (`researchSource`, `researchDataset`, `researchArtifactContent`) in `packages/work-client/src/research.generated.ts`.
- **Proving Test Files**:
  - `python/omp-work/tests/test_research_artifact_custody.py`: proves registration, snapshot hashing, ACL-governed retrieval, and path escape prevention.
  - `python/omp-work/tests/test_research_custody_rules.py`: proves custody invariants, cache replicate restrictions, and tamper rejection.

---

## 2. Outcome Clause to Evidence Mapping

Section 23 of `docs/programme/AUTORESEARCH-IMPLEMENTATION-PLAN.md` defines the M0 milestone outcome as:
> **Reproducible source/runtime identity and durable research contracts/jobs**

The table below maps each clause of this outcome statement to the concrete repository artifacts and test evidence establishing it:

| Outcome Clause | Concrete Repository Artifacts | Governing Tests & Evidence |
|---|---|---|
| **Reproducible Source Identity** | • Pinned mirror: `session-system/ecc/mirror/`<br>• Source & transformed hashes: `session-system/ecc/adapted.lock.json`<br>• Pack catalog: `session-system/ecc/manifest.json`<br>• Research source & dataset manifests: `python/omp-work/src/omp_work/contracts/v1/research-artifact-hash.json`<br>• Custody storage: `python/omp-work/src/omp_work/research/custody.py` | • `session-system/tests/ecc-adaptation.test.ts` (verifies mirror SHA-256 hashes against manifest pins and byte-identical builds)<br>• `session-system/tests/ecc-install.test.ts` (verifies installed file hashes against lock file)<br>• `python/omp-work/tests/test_research_artifact_custody.py` (verifies content-addressed snapshot hashes and source registration) |
| **Runtime Identity** | • Installed binary & daemon identities: `docs/programme/R00-BASELINE-2026-09-25.md` Section 1<br>• Discovery catalog: `session-system/ecc/adapter/catalog.ts`<br>• Drop-in service configuration: `/home/thetu/.config/systemd/user/omp-work-service.service.d/flood-clone.conf` | • `docs/programme/R00-BASELINE-2026-09-25.md` (committed via `b8746dcd16` on main, audited against live processes and git trees)<br>• `session-system/tests/ecc-adaptation.test.ts` (verifies native discovery under namespaced identity) |
| **Durable Research Contracts** | • Canonical schemas: `python/omp-work/src/omp_work/contracts/r02/`<br>• WorkService contract definitions: `python/omp-work/src/omp_work/contracts/v1/schema.json` & `api-schema.json`<br>• Generated client bindings: `packages/work-client/src/research.generated.ts`<br>• WorkClient API: `packages/work-client/src/index.ts` | • `packages/work-client/test/research-bindings.test.ts` (proves client bindings match canonical schemas; drift detection)<br>• `packages/work-client/test/research-client.test.ts` (proves typed client dispatch and response serialization)<br>• `python/omp-work/tests/test_research_contract.py` (30 contract tests)<br>• `python/omp-work/tests/test_research_rules.py` (10 rule tests)<br>• `python/omp-work/tests/test_research_identity_trust.py` (5 identity-is-not-trust tests) |
| **Durable Research Jobs** | • Native jobs engine: `python/omp-work/src/omp_work/jobs/`<br>• Migrations on `omp_jobs`: `0001_omp_jobs_schema.sql`, `0003_native_research_jobs.sql`<br>• Fenced leasing: `python/omp-work/src/omp_work/jobs/lease.py`<br>• Cancellation cascading: `python/omp-work/src/omp_work/jobs/cancel.py`<br>• Stable usage accounting: `python/omp-work/src/omp_work/jobs/usage.py` & `outbox.py` | • `python/omp-work/tests/test_native_jobs_process_recovery.py` (6 tests proving real-process recovery across unacknowledged transitions and process restarts)<br>• `python/omp-work/tests/test_native_jobs_cancellation.py` (2 tests proving cascade cancellation and admission refusal)<br>• `python/omp-work/tests/test_native_jobs_usage.py` (7 tests proving idempotent outbox delivery and stable usage accounting) |

---

## 3. Test Verification and Pass Counts

Executed on checked-out commit `8e219f7bc1e97ae3e00a40c5081d2733e8459ca1`:

### TypeScript / Bun Test Suites
1. **ECC Installation Suite**:
   ```bash
   bun test session-system/tests/ecc-install.test.ts
   ```
   - **Result**: `17 pass, 0 fail` (80 expect calls across 1 file)

2. **ECC Adaptation Suite**:
   ```bash
   bun test session-system/tests/ecc-adaptation.test.ts
   ```
   - **Result**: `25 pass, 0 fail` (172 expect calls across 1 file)

3. **Work-Client Research Bindings and Client Suites**:
   ```bash
   bun test packages/work-client/test/research-bindings.test.ts packages/work-client/test/research-client.test.ts
   ```
   - **Result**: `3 pass, 0 fail` (37 expect calls across 2 files: `research-client.test.ts` 1 pass, `research-bindings.test.ts` 2 pass)

### Python / pytest Test Suites
Command (run from `python/omp-work`):
```bash
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest \
  tests/test_research_contract.py \
  tests/test_research_rules.py \
  tests/test_research_identity_trust.py \
  tests/test_research_artifact_custody.py \
  tests/test_research_custody_rules.py \
  tests/test_native_jobs_process_recovery.py \
  tests/test_native_jobs_cancellation.py \
  tests/test_native_jobs_usage.py
```
- **Result**: `74 passed, 0 failed, 1 warning` (in 17.40s)
  - `tests/test_research_contract.py`: 30 passed
  - `tests/test_research_rules.py`: 10 passed
  - `tests/test_research_identity_trust.py`: 5 passed
  - `tests/test_research_artifact_custody.py`: 8 passed
  - `tests/test_research_custody_rules.py`: 6 passed
  - `tests/test_native_jobs_process_recovery.py`: 6 passed
  - `tests/test_native_jobs_cancellation.py`: 2 passed
  - `tests/test_native_jobs_usage.py`: 7 passed

*(Note: When run without `OMP_WORK_POSTGRES_INTEGRATION=1`, tests requiring live PostgreSQL integration skip gracefully: 7 passed, 67 skipped. With the integration flag active, all 74 tests execute and pass.)*

### Verification Summary
- **Total Test Files Evaluated**: 12 files (4 TypeScript, 8 Python)
- **Total Tests Passed**: 119 passed, 0 failed (45 TypeScript tests, 74 Python tests)
- **Milestone Criteria Status**: Satisfied
