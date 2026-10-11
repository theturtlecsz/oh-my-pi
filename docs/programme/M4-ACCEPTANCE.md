# Autoresearch M4 Acceptance Record: Continuous Research Service

- **Milestone**: Autoresearch M4. Continuous research service.
- **Required Packages**: R15, R16, R18
- **Task**: `OMP-318` (Slice: `OMP-318-s06`)
- **Promised Outcome**: Persistent distributed campaigns with CLI/WebUI control and complete accounting.
- **Status**: source delivered, not installed-qualified

Waiting for Chris: OMP-318-s07 (child filing and blocking edges)

## 1. Package Deliveries and Evidence

### R15 — Continuous programs and distributed capacity

`research/program.py` owns question queues, dependencies, standing-policy renewal, evidence refresh, and snapshots. `research/capacity.py` routes CPU/GPU jobs through the native jobs pool with engineering headroom and fenced settlement. `research/continuous.py` composes these APIs with one service campaign. Cancellation leaves attached CPU research cancelled, rejects its old fence, preserves sealed GPU evidence, and leaves ordinary engineering admitted on the CPU worker.

Proving test files:

- `python/omp-work/tests/test_research_m4_continuous.py`
- `python/omp-work/tests/test_research_program.py`
- `python/omp-work/tests/test_research_capacity.py`

### R16 — Research views and review

The shared `cpk5/v1` facts feed campaign, graph, evidence, usage, worker, and operation projections. This slice appends a campaign fact through `append_program_fact`; it does not import TypeScript or replace the existing client projections.

Proving test files:

- `packages/work-client/test/research-views.test.ts`
- `packages/work-client/test/research-review.test.ts`

### R18 — Accounting and qualification gates

`research/accounting.py` writes usage into the native ledger, including quota-crossing spend, and supplies the campaign admission gate. The continuous proof exercises protected negative acceptance, matched-resource preregistered comparison, drift detection, fault denominators, and policy intervention without changing the other policy or queue.

Proving test files:

- `python/omp-work/tests/test_research_accounting.py`
- `python/omp-work/tests/test_research_m4_continuous.py`

## 2. Outcome Clause to Evidence Mapping

| Outcome clause | Repository evidence | Proving test |
| --- | --- | --- |
| Persistent campaigns | `program.json` restores supported evidence and narrowed authority; resume adds no jobs and duplicate campaign admission is refused. | `test_research_m4_continuous.py` |
| Distributed capacity | GPU research seals; CPU research cancellation invalidates its fence; engineering retains its CPU lease. | `test_research_m4_continuous.py`, `test_research_capacity.py` |
| Control and projections | Existing service command cancels the same campaign; canonical campaign fact remains readable after restart. Client projections and review consume shared research facts. | `test_research_m4_continuous.py`, `research-views.test.ts`, `research-review.test.ts` |
| Complete accounting | First usage event persists 10 input tokens and $1; crossing event persists $10; admission refuses `quota_exceeded`. | `test_research_m4_continuous.py`, `test_research_accounting.py` |
| Qualification | Accepted negative control is refused; canary detects drift; preregistered comparison uses matched resources; crash remains in denominator 2. | `test_research_m4_continuous.py`, `test_research_accounting.py` |

## 3. Test Verification

Command, from repository root:

```bash
uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_m4_continuous.py
```

Result: **2 passed**.

This slice verifies the continuous source proof only. The other proving files identify package evidence; they are not a claim that this slice ran them. Installed qualification, native deployment, and complete-system performance remain unverified.

## Assumptions

The native jobs fixture supplies an admitted service campaign, proposed trial, registered worker/evaluator components, and UUID workspace/actor identities. Logical program time is 2026-10-02 UTC. Native jobs retain their existing lease clock and authority checks. Resume reads `program.json` and `facts.jsonl`; it does not enqueue work. Durable receipt bytes live in `receipts/<trial_id>.json`.

## Deferred

The s01 program coordinator does not itself call jobs, spawn processes, or open sockets/HTTP clients. Its deferred operational work remains explicit:

- Live continuous controller and automatic question-to-campaign scheduling.
- Fleet/native scheduling ownership reconciliation and production fairness.
- Installed heterogeneous workers, physical instrument adapters, and actual backend receipts.
- Production data locality and distributed coordinator failover qualification.
- Installed CLI/WebUI control and complete-system stabilization/performance qualification.
- Child filing and blocking edges: OMP-318-s07.
