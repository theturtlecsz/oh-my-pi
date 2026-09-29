# ADR 0004: Hard padlock — sole mutator, effort launch-gate, reviewer_required on blast-radius

**Status:** Accepted (Horizon C padlock). Horizon A/B PASS evidence is source delivered, not installed-qualified.
**Date:** 2026-09-20
**Program:** FULL-PROGRAM v1.1

## Context
Day-30 and in-process Horizon A/B harnesses demonstrated the cockpit path and depth slices. Platform freeze requires a hard padlock so advisors cannot mutate live economy and paid work cannot bypass gates.

## Decision

### 1. Sole mutator
Run Owner is the only principal holding mutate grants and exercises them **only through WorkService** (FULL-PROGRAM v1.1 §3). Advisors (Programme Design, Status Desk, Loop Auditor, Deep Research) are read-only / blueprint-only.

### 2. Effort launch-gate
Every paid or role-gated launch must pass `bin/launch-gate.sh` / `admit-check.sh` with:
- `effort:` E0–E4 (default E1)
- `effort_reason:` when not E1 (mandatory for E4)
- Model pairing per ACTIVE-POLICY / MODELS
- Soft checkpoint ≤ expected_max; empty soft + 0 product writes = fail

### 3. reviewer_required on blast-radius
Blast-radius paths (ACTIVE-POLICY, ledger, auth, economy workflows, deploy/release) require `reviewer_required: true` (or documented `reviewer_override_reason` ≥20 chars). Horizon PASS/FAIL writers use E3 + `reviewer_required: true` (no self-declared horizon flags alone).

### 4. Job API freeze
`ACTIVE/JOB-API.md` is the frozen WorkService contract for this programme revision. Changes are a new ADR + Chris gate.

### 5. Extract default
`ACTIVE/EXTRACT-POLICY.md`: stay-in-OMP; any extract stops for Chris.

## Evidence cited (A+B) — source delivered, not installed-qualified

In-process harnesses in this repository demonstrate the A/B slices; the `ACTIVE/PIVOT-*.md` records cited below are not in this repository.
- A1 durability: [`durability_a1.py`](../../python/omp-work/src/omp_work/durability_a1.py) (in-memory, fixed zero-loss subset).
- A2/A3 cockpit verbs: [`cockpit_verbs.py`](../../python/omp-work/src/omp_work/cockpit_verbs.py) (in-memory demo, not a Grok Bot integration).
- B1 finding reuse ×2: `knowledge_b1.py` — OMP-419 removed it from the product.
- B1 keyword demo: `cognee_adapter.py` — OMP-419 removed it from the product.
- Referenced but absent from this repository: `ACTIVE/PIVOT-HORIZON-A-PASS.md`, `ACTIVE/PIVOT-HORIZON-B-PASS.md`, `ACTIVE/PIVOT-DAY30-PASS.md`, `ACTIVE/FULL-PROGRAM-LOCK.json` (`lock-full-program-v1.1-20260920`).

## Soft leftovers (explicit — not padlocked yet)
1. Full 72-hour ADR 0001 durability soak (A1 was §3 subset, not 72h)
2. Live Ollama/Fable usage lines on every research job (B2 used declared model ids on mechanical BoN costs)
3. Production WorkService PG integration tests beyond harness façades
4. Qualified ECC submodule install into session-system (packs demonstrated; full WP2 install not this freeze)
5. Status Desk human rollup UX polish beyond verb outputs
6. Horizon C does **not** claim microservice readiness

## Consequences
- Violations of sole-mutator / launch-gate / blast-radius review are programme defects, not style nits
- Post-C work requires Chris direction (`FULL_PROGRAM_AWAIT_CHRIS`)
