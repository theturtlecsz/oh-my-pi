# Autoresearch M1 Acceptance Record: Managed Investigation

- **Milestone**: Autoresearch M1. OMP runs one engineering investigation and can reproduce it.
- **Required Packages**: R05, R06, R07, and initial R17
- **Task**: `OMP-315` (Slice: `OMP-315-s07`)
- **Promised Outcome**: OMP can run, evaluate, recover, and reproduce an engineering investigation
- **Status**: source delivered, not installed-qualified

Waiting for Chris: OMP-315-s08 (child filing and blocking edges)

---

## 1. Package Deliveries and Evidence

### R05 — Local jail and cancel

- **Commits**:
  - `632e8a7935`: `OMP-315-s01: add the local-jail research runner`
  - `6613f4f6e8`: `OMP-315-s01: resolve and hash actual jail executable in runner manifest`
  - `605406c014`: `OMP-315-s02: flood auto-commit (attempt 1)`
- **What it delivered**:
  - The runner runs one candidate inside the worker jail.
  - The runner hashes each jail executable.
  - Cancel stops the candidate process.
- **Proving test files**:
  - `python/omp-work/tests/test_research_runner.py`
  - `python/omp-work/tests/test_native_jobs_cancel_signal.py`

### R06 — Receipt, worker, and trust

- **Commits**:
  - `ee630f8bbf`: `OMP-315-s03: typed research receipt, evaluation protocol, and verification`
  - `4298ac2811`: `OMP-315-s04: add the managed-trial evaluation worker`
  - `8b9f41c268`: `OMP-315-s04: harden managed-trial evaluation against path escape, cancel, bad score`
  - `f37caa8460`: `OMP-315-s05-s01: implement is_managed_campaign and verify_managed_trial steps 1-6`
  - `82edcd2640`: `OMP-315-s05-s02: revert version check in is_managed_campaign, clean test imports, and verify trial_id tie-break`
  - `a8ecde93c2`: `OMP-315-s05-s03: bind managed research trials only with a trusted qualified receipt`
- **What it delivered**:
  - The protocol and the receipt bind one trial.
  - The worker runs the candidate and records the receipt.
  - Trust follows the job settlement and the worker capability.
- **Proving test files**:
  - `python/omp-work/tests/test_research_receipt.py`
  - `python/omp-work/tests/test_jobs_managed_trial.py`
  - `python/omp-work/tests/test_research_managed_gate.py`

### R07 — Frozen candidates and import

- **Commits**:
  - `605406c014`: `OMP-315-s02: flood auto-commit (attempt 1)`
  - `d6ee2a1b6a`: `OMP-315-s06: import managed autoresearch results as unverified runs`
- **What it delivered**:
  - The adapter stores one git commit as a candidate archive.
  - The import reader marks each row imported-unverified.
- **Proving test files**:
  - `python/omp-work/tests/test_research_engineering_adapter.py`
  - `packages/coding-agent/test/autoresearch-managed-import.test.ts`

### R17 — Bundle and M1 proof

- **What it delivered**:
  - `python/omp-work/src/omp_work/research/bundle.py` stores the bundle for one investigation.
  - The bundle holds the candidate, the evaluator, the protocol, and the receipt.
  - The bundle does not hold confirmation bytes.
  - `verify_bundle` checks artifact bytes and the result files.
  - `reproduce` runs each qualified trial on this host.
  - A separate directory must hold the confirmation file.
  - An edit of `results.md` yields `report_altered`.
- **Proving test file**:
  - `python/omp-work/tests/test_research_m1_investigation.py`

---

## 2. Outcome Clause to Evidence Mapping

The promised outcome is: OMP can run, evaluate, recover, and reproduce an engineering investigation.

| Outcome clause | Repository evidence | Proving test |
| --- | --- | --- |
| Run | The client posts `mission.submit` with kind `research.run`. A worker process then runs the trials. | `python/omp-work/tests/test_research_m1_investigation.py` |
| Evaluate | The worker qualifies two trials. The broken trial returns `candidate_failed`. | `python/omp-work/tests/test_research_m1_investigation.py` |
| Recover | A killed worker stops the heartbeat in under 2 seconds. A new worker records one observation. Cancel records no observation and no receipt. | `python/omp-work/tests/test_research_m1_investigation.py` |
| Reproduce | `verify_bundle` returns an empty list. The reproduce command returns `reproduced` for the winner when the private file is present. The command returns `missing_private_dependency` without that file. An edited `results.md` yields `report_altered`. | `python/omp-work/tests/test_research_m1_investigation.py` |

---

## 3. Test Verification

Command, from the repository root:

```bash
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_research_m1_investigation.py
```

- **Result**: `1 passed, 10 warnings in 56.75s`
- The test skips when `OMP_WORK_POSTGRES_INTEGRATION` is unset.
- The test skips when the unshare probe fails.
- `test:py:work-ledger:integration` in `package.json` now runs four Postgres tests from OMP-315.
- This record states source delivery only. The status is source delivered, not installed-qualified.

## Assumptions

- Package map: R05 = s01 (runner), s02 (cancel, drain); R06 = s03 (receipt, protocol), s04 (evaluator worker), s05 (evidence translation); R07 = s02 (frozen engineering candidates), s04 (managed mode), s05 (trusted ingestion), s06 (dashboard/history, labels); initial R17 = s07 (bundle, end-to-end proof). Filing them as Work Ledger children with blocking edges (including the R01-R04 prerequisites OMP-321, OMP-322, OMP-324, OMP-323 named in docs/programme/M0-ACCEPTANCE.md) is owner slice s08, run by Chris.
- Waiting for Chris: s08 only (child filing and edges). No slice depends on it. No contract change, so no approval slice.
- WP4 (plan: "typed audit") is OMP-520, the item the owner ruling ties to R06's receipt; s08 adds "OMP-520 blocks R06".
- research.run: the client contract already carries it as MissionKind "research.run" with requested_capabilities on mission.submit (contracts/v1/decisions/0019-client-contract.md item 5; routing in tests/test_routing_mission.py). No new client operation. The path to managed execution is: dispatch_managed_trial (s04) refuses unless the trial's work is linked to an approved or running research.run mission whose requested_capabilities hold "research.managed_trial"; s07 submits that mission through the client operation mission.submit (POST /v1/workspaces/{ws}/client/missions, which maps to submit_mission), then approves and links it, and runs the investigation from it. Creating a campaign from a mission stays manual (Deferred).
- Containment reuses the existing egress worker jail (run_sandboxed root="worker", egress_research_jail.py): tmpfs root with only /usr, /bin, /lib, /lib64, /sbin read-only, generated /etc, four /dev nodes, fresh /proc, /tmp; /work read-write; named read-only file binds; own network namespace with no egress; env only PATH, LANG, TERM, TZ. Writable: /work and the jail's own discarded /tmp tmpfs.
- Model/tool broker isolation = no broker, socket or network reachable from the jail. A brokered model channel for trials is Deferred.
- Trust anchor: a managed receipt counts only when the lease holder of job managed-trial:<trial> settles it, on a worker registered with "research.managed_trial", with issuer = the trial's admitted evaluator. Custody bytes alone prove nothing; candidates in the jail hold no token or socket.
- The typed receipt mirrors OMP-520's EvidenceReference names (kind, ref, result, sha256); OMP-520 is not on main. The protocol + receipt module is the evaluation base shared with OMP-252.
- Native TS autoresearch cannot authenticate a results file, so imported rows are labeled "imported-unverified", flagged and kept out of best/baseline. WorkService (s05 gate, s07 bundle) is the trusted ingestion.
- Worker admission and drain exist (R03/OMP-400); s02 qualifies drain and adds cancel propagation. R03 audit/release receipts for a bind are supplied by tests, as tests/test_native_jobs_trial_gate.py does.
- No changelog: owner slice s08 has no dependents, so no slice depends on all others.

## Deferred

- Remote, GPU and instrument backends; installed/compiled worker qualification of the managed runner.
- Brokered model/tool access for trials.
- Authenticated verification of imported results in TS (needs a coding-agent to WorkService read path).
- Automatic campaign creation from a research.run mission; TS autoresearch submitting trials itself.
- Native close-audit receipts for trials and importing OMP-520 types (OMP-520); Harbor grading on the receipt (OMP-252).
- R17 plots, notebooks, manuscript/PDF. Changelog entries.
