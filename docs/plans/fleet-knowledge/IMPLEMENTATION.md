# FK-0 Architecture & Implementation Record: Fleet Knowledge Subsystem

**Document Version:** 1.2 (2026-09-12) — Native Intake Recorded & Foundation Slices Synchronized  
**Status:** ORDINARY INTAKE RECORDED (OMP-278 Umbrella & OMP-279 Shared Foundations in `BACKLOG`); FK-1..7 Verification & Candidate Slices Ongoing (No Execution Grant, No Live Activation)  
**Branches & Worktree Topology:**  
- **Primary / Engine Worktree:** `/home/thetu/.local/state/omp-fleet-knowledge/worktree` on branch `fleet-knowledge/v1-20260912` (Base `aacabebf7c9d894ff8dca4b9fff188303b2491f4`) — contains Slice C engine (`python/omp-knowledge`), shared contracts (`python/omp-work/src/omp_work/knowledge_contracts.py`), documentation, and intake records.  
- **Isolated Native Reads Worktree:** `/home/thetu/.local/state/omp-fleet-knowledge/native-a-worktree` on branch `fleet-knowledge/native-reads-20260912` (Base `aacabebf7c9d894ff8dca4b9fff188303b2491f4`, awaiting integration) — contains Slice A native exact reads in `python/omp-work`.  
- **Authoring Tier:** Both source tracks authored under High-effort mode; accepted native policy source is future, not already implemented. Native_a initial structural corrections pending; no PASS claim made.  
**Ownership:** `/root/fleet_knowledge_coordinator` (`docs/plans/fleet-knowledge/` and `fk0` discovery evidence)  
**Advisory Qualification:** Fable 5.1 completed candidate plan (`fk1-fk3-resolution.json`, session `e6acdd74-2d2f-43f6-83a0-98ada8d29e83`) with zero authoring blockers; advisory evidence only, not a native grant.

---

## 1. Status & Operational Boundaries

FK-0 architecture and mapping is now recorded under ordinary native intake. WorkService batch admission successfully established native work ownership for **OMP-278** (full-release umbrella) and **OMP-279** (shared foundations child) in `BACKLOG`. Source, documentation, and milestones FK-1 through FK-7 verification remain ongoing.

### Strict Negative Contracts & Boundary Guarantees
- **Intake vs Execution**: `intakeRecorded: true`, but `executionAdmitted: false` (no execution grant) and `nativeAcceptance: false` (no native acceptance).
- **No Live Mutation**: No live schema mutation, contract activation, service restart, or bearer capability alterations are authorized or granted by this documentation.
- **Dual Worktree Integration**: Original `/worktree` (`fleet-knowledge/v1-20260912` with C engine and shared contracts) and isolated `native-a-worktree` (`fleet-knowledge/native-reads-20260912` with Slice A native reads) share base `aacabebf7c9d894ff8dca4b9fff188303b2491f4` and await integration. Native_a initial structural corrections are pending; no PASS claim is made.
- **Mandatory Two-Repository Release Scope**: Full first release qualification mandatory across both `oh-my-pi` AND `media-discovery` actual repositories, inspecting actual topologies and fixtures, without single-repo shortcuts.
- **Model & GPU Qualification**: GPU private CUDA 64MiB probe passed (pointer [`gpu-runtime-20260912/cuda-readiness-receipt.json`](file:///home/thetu/.local/state/omp-fleet-knowledge/gpu-runtime-20260912/cuda-readiness-receipt.json)); no model inference or residency qualification yet. Optional comparative benchmarks (Harbor OMP-250 / OMP-252) remain DEFERRED.
- **Stabilization Boundary**: Stabilization track retains sole ownership of OMP-277 and native criteria/execution mutation reconciliation. No overlap or mutation occurs here.

---

## 2. Preserved Source Documents

The three authoritative documents from `/home/thetu/.local/state/omp-fleet-knowledge/intake-20260912` are preserved verbatim under `docs/plans/fleet-knowledge/`. Byte counts and SHA-256 hashes match `/home/thetu/.local/state/omp-fleet-knowledge/intake-20260912/extraction.json` independently verified including final newlines:

| Preserved Document Path | UTF-8 Bytes | SHA-256 Digest |
|---|---:|---|
| `docs/plans/fleet-knowledge/OMP_Fleet_Knowledge_Orchestrator_Handoff.md` | 105,160 | `36bd3672970bcab83d10784bb1fead1a2492bea86dcde67b811858274ff6fe3e` |
| `docs/plans/fleet-knowledge/OMP_Immediate_Roadmap_Amendment.md` | 26,194 | `bb4933d5eff9a623537a347364c551d11b0c928f9fcb65713a2651335866fae3` |
| `docs/plans/fleet-knowledge/OMP_Memory_Candidate_Decision_Report.md` | 43,276 | `50d6e266ba0219d8fe0c8560e28a5f4dd32501b866105abcb7a840e97bc9369c` |

Historical `sandbox:` links are retained verbatim as dated source references.

---

## 3. Current Native Work Authority & Authoritative Readback Records

Native GET reads were performed against the active WorkService loopback endpoint (`127.0.0.1:54322`) via `WorkClient` and capability-based authentication (`loadWorkConfig` / `loadBearer`). No credentials or tokens were logged.

### Live Service Identity
- **Endpoint**: `http://127.0.0.1:54322`
- **Health State**: `live: true`, `ready: true`, alerts empty.
- **Service Fingerprint**: `40ee9700adbaf44c2454443700c58ee47c92e7b8e70f915a977888decb8ddcac`
- **Authority**: `authority: "work"`, epoch active.

### Programmatically Verified Native Records
Every row in the table below is derived directly from raw `fk0/workflows/{KEY}.json` records (persisted at `/home/thetu/.local/state/omp-fleet-knowledge/fk0/workflows/`) reading `.workflow.item.alias.key`, `.workflow.item.work_id`, `.workflow.item.revision.revision_id`, `.workflow.item.revision.revision_number`, `.workflow.item.state`, and `.workflow.item.revision.title`. Programmatic verification evidence is preserved in [`fk0/table-verification.json`](file:///home/thetu/.local/state/omp-fleet-knowledge/fk0/table-verification.json).

| Alias | Work ID | Revision ID | Rev | State | Title |
|---|---|---|---:|---|---|
| `OMP-202` | `f7cf53f0-c2b9-4985-977e-686360bfb3e8` | `a71f757f-2716-543d-a0f2-37a184e63f38` | r4 | `BACKLOG` | OMP v9.1 — Constitutional Plugin Kernel & Deterministic Workflow Hardening |
| `OMP-204` | `2c4ca917-1526-4f07-81a1-67e58c099f5b` | `1ca644a3-0abc-4979-a932-9f536b9b6e12` | r1 | `BACKLOG` | H1: Freeze, Observe & CPK-0 Discovery |
| `OMP-205` | `6a98e808-7ff4-4ccc-b2bf-ad110d0824b3` | `22ccae60-0c3e-474f-8460-b1ff39e88ff3` | r1 | `BACKLOG` | H2: Deterministic Proof & CPK-1/2 Manifest/Graph Shadow |
| `OMP-206` | `0abf72de-8ae9-4deb-a0f6-c69651b0c27d` | `99c62f99-f035-47fe-8c29-eb0c65099e23` | r1 | `BACKLOG` | H3: Single Transition Owner & CPK-3 Typed Seams |
| `OMP-207` | `f89eba71-ffe4-42b6-93d7-bf2ee48e5f6d` | `8d8d8002-b3c0-47a2-9c7e-3c37673cd92e` | r1 | `BACKLOG` | H4: Thin Consumers, Replayable Projections & CPK-4/5 Profiles |
| `OMP-208` | `4a3bf75d-fe11-4154-9791-9851873a022d` | `3dbb4d38-1529-4c36-8828-3a23317e628e` | r1 | `BACKLOG` | H5: Supervision Tuning, CPK-6 Retirement & Canary Rollout |
| `OMP-266` | `95d52a7f-77a4-4434-8bcc-ab90ccbbfc8b` | `65bbf08c-7a76-461b-992c-941b401110c0` | r1 | `BACKLOG` | Bounded typed intake: one small code-change path with native ratification and readiness |
| `OMP-267` | `ce215e63-9072-4622-8051-9df9a2e6350c` | `5ed47036-ffbe-5aa4-9c0c-f605e3ee6fc0` | r2 | `BACKLOG` | 03B E1: exploration contracts, generation policy and reviewed frame registry |
| `OMP-268` | `4daad6ca-0213-4aff-b026-878c5793fb02` | `af997771-8a69-5afe-bf82-cb56a79d2128` | r2 | `BACKLOG` | 03B E2: durable exploration execution, screening, budget and recovery |
| `OMP-269` | `a99ac8f0-eb7b-4607-9652-04f709ab52a2` | `c56247d2-4aa5-5600-81c5-7a2c9b9f3436` | r2 | `BACKLOG` | 03B E3: neutral CLI and Web exploration selection with disposable projections |
| `OMP-270` | `b00e8c27-107e-479d-a92b-51c71ac13e3b` | `a6014b0d-6886-57a5-82ab-405a5e04bd5c` | r2 | `BACKLOG` | 03B NSI-E4: focused posture, frame and breadth qualification |
| `OMP-16` | `d76f38e8-749d-4b33-9b6c-05ccab6f3538` | `c1596e22-53d2-5e93-a43d-88b6b5caff8c` | r3 | `TRIAGE` | Build the autonomous Work Ledger fleet controller [owner ruled 2026-08-21: parked until OMP-47 settles] |
| `OMP-217` | `4df3f00e-da44-49a6-9851-7db07714023e` | `127a6ab4-5250-530c-8f85-71c854dd6e1e` | r2 | `DONE` | oh-my-pi fork main CI red baseline: biome errors, migration-pin pytest failures, env-sensitive MCP test |
| `OMP-233` | `40571eb8-fd7b-4161-aa41-0c965b158f47` | `35966504-9045-5ac8-8399-725624127411` | r2 | `BACKLOG` | Execution grant lifecycle hardening: interjection scope, pause consistency, stop/resume/admission integrity |
| `OMP-264` | `bfa56e52-047b-4680-ac73-c80247813775` | `2a456763-ccf4-50dd-8705-be5986a80333` | r2 | `DONE` | Host extension execution audit blocker: relative repository path in beginCloseAttempt |
| `OMP-273` | `bae667b3-2543-4fe6-a63f-d3a61a97bb7c` | `e44cd77d-1198-4feb-88cd-0fe0b9d6c525` | r1 | `DONE` | Require Antidote before new repository work |
| `OMP-274` | `83172611-94ae-44a9-891b-9da1469fc74d` | `b21f097c-2550-47ab-89e4-b214b9672464` | r1 | `DONE` | P4a: reconcile committed criteria response loss |
| `OMP-275` | `798fc633-9c6a-47c7-904d-ff6c7b8a3151` | `19c0d4c0-9e1e-41a9-8eb5-742737d8bc99` | r1 | `DONE` | Preserve native audit model attribution in parent-session diagnostics |
| `OMP-276` | `3d583fb1-38d4-4449-af9e-201bfa4d308a` | `918c0e0b-3294-4dfa-912e-4d0cbfb4f66c` | r1 | `DONE` | Expose bundled bunx in isolated runtime packages |
| `OMP-277` | `db7bac30-d5e2-43e8-909e-eabd32605904` | `88674605-e953-4087-8eb2-14003f470ad0` | r1 | `BACKLOG` | Reconcile committed execution mutations after response loss beyond criteria seals |
| `OMP-278` | `ac0c0f93-3cde-4400-8ae3-e07503e0a707` | `70bbaa8e-a485-4bb6-8c53-92755bdfe9f8` | r1 | `BACKLOG` | Fleet Knowledge v1: evidence-bound learning, code snapshots and context |
| `OMP-279` | `3a51a1a4-ea43-468d-aabe-3ea6bcab6a34` | `45baa9b3-dadb-48e7-9d31-ded0696da770` | r1 | `BACKLOG` | Fleet Knowledge foundation slice: native exact historical reads, shared contracts, and pinned Cognee adapter |

### 3.2 Native Admission Record & Readback Verification
Programmatically extracted from [`verified-result.json`](file:///home/thetu/.local/state/omp-fleet-knowledge/admission-preparation/admission-run/verified-result.json) (`2026-09-12T12:47:00.552933+00:00`):

- **OMP-278 (Full-Release Umbrella)**:
  - **Work ID**: `ac0c0f93-3cde-4400-8ae3-e07503e0a707`
  - **Workspace ID**: `d95ce294-57f9-42fe-9485-e2eec6eb0272`
  - **Project ID**: `01a011eb-d41b-7f64-9404-751f87207d42` ("The Bookends")
  - **Parent**: OMP-202 (`f7cf53f0-c2b9-4985-977e-686360bfb3e8`, rev `a71f757f-2716-543d-a0f2-37a184e63f38`)
  - **State**: `BACKLOG`
  - **Revision**: `70bbaa8e-a485-4bb6-8c53-92755bdfe9f8` (rev 1)
  - **Content SHA-256**: `878c828315680a2d677bd6892a79c4b1561f554f9051b6e7f0c28809bf30dc5a`
  - **Scope**: Full-release Fleet Knowledge subsystem across milestones FK-0 through FK-7 under OMP-202.

- **OMP-279 (Shared Foundations Child)**:
  - **Work ID**: `3a51a1a4-ea43-468d-aabe-3ea6bcab6a34`
  - **Workspace ID**: `d95ce294-57f9-42fe-9485-e2eec6eb0272`
  - **Project ID**: `01a011eb-d41b-7f64-9404-751f87207d42` ("The Bookends")
  - **Parent**: OMP-278 (`ac0c0f93-3cde-4400-8ae3-e07503e0a707`)
  - **State**: `BACKLOG`
  - **Revision**: `45baa9b3-dadb-48e7-9d31-ded0696da770` (rev 1)
  - **Content SHA-256**: `286d90c48bc085ef7ff80a9729542662b715d5f6d1a2c307e9a849a7a2aece55`
  - **Candidate**: ID `b19aaeab-3aec-558e-ab16-38fd8664ac78`, SHA-256 `0b67c360bcca832b3a0ea697466c1792a667e489535952b67cf8275dcfc83c66`, kind `planned`, allocated at `2026-09-12T12:45:22.273000+00:00`
  - **Scope**: Bounded implementation of FK1, FK2, and FK3 first source slice.

- **Plan Evidence Binding**:
  - **Plan Receipt ID**: `8e253cba-d6fa-5410-bd55-a6649c5d4b29`
  - **Plan SHA-256**: `5a7eba6e76f19fe2bd02c25d1cfb654db0342ff8ab6082258048438e192a5e1d`

- **21 Independent Readback Checks (All Succeeded)**:
  `focus_unchanged`, `execution_unchanged`, `OMP-202_item_unchanged`, `OMP-277_item_unchanged`, `OMP-246_item_unchanged`, `OMP-249_item_unchanged`, `OMP-278_backlog`, `OMP-278_scope_description_criteria_match`, `OMP-279_backlog`, `OMP-279_scope_description_criteria_match`, `related_OMP-266`, `related_OMP-206`, `related_OMP-207`, `related_OMP-208`, `related_OMP-16`, `related_OMP-241`, `related_OMP-250`, `related_OMP-252`, `child_parent`, `umbrella_parent`, `plan_receipt_exact_hash`.

- **Script False Failure Root Cause**:
  The admission coordinator script exited with code 1 (`script_exit: 1`) because its verification assertion wrongly demanded that the umbrella work item be the `source` endpoint for every related edge. In PostgreSQL WorkService, `related` edges are undirected and endpoint UUID order is canonicalized on insert. Independent canonical comparison confirmed all 8 related relations and properties match exactly. Zero retried writes (`retry_writes: 0`).

- **Negative Contract Guarantees**:
  - `intakeRecorded: true`
  - `executionAdmitted: false` (no execution grant admitted)
  - `nativeAcceptance: false` (no native acceptance)
  - Live contract activation / service restart: none.

### Completeness Limitations & Source Proof
Inspection of `/home/thetu/omp-233-lifecycle/python/omp-work/src/omp_work/v1/store.py` proves:
- **No Cursor / Pagination / Export API**: Neither `WorkClient` nor `WorkService` provides a pagination cursor or bulk export endpoint.
- **`tree()` Hard Caps**:
  - `work_items`: `LIMIT 1000` ordered by `a.key` (`store.py:5171`)
  - `work_relations`: `LIMIT 5000` ordered by `created_at` (`store.py:5179`)
  - `projects`: `LIMIT 500` ordered by `p.name` (`store.py:5184`)
- **`workflow(key)` Hard Caps**:
  - `auditor_launches`: `LIMIT 100` ordered by `reserved_at, launch_id` (`store.py:5119`)
  - `close_attempt_events`: unresolved delivery debt merged with recent events `LIMIT 200` (`store.py:5134`)
- **Completeness Gap**: Known-key reads retrieve current workflow snapshots, but cannot prove absence beyond 1,000 items or reconstruct complete historical source revision progressions.

---

## 4. Host Environment & Model Serving Profile

Parent host inventory is documented in [`/home/thetu/.local/state/omp-fleet-knowledge/host-inventory-20260912/REPORT.md`](file:///home/thetu/.local/state/omp-fleet-knowledge/host-inventory-20260912/REPORT.md).

### Host & Hardware Observations
- **Environment**: CT120 (arch-dev), LXC on Proxmox VE (hostname `pve` at `192.168.0.159`), Linux 6.17.4-2-pve.
- **CPU & Memory**: Intel Core i9-12900KF (16 effective CPUs online), 104 GiB RAM (84 GiB available at observation).
- **GPU Hardware**: NVIDIA GeForce RTX 5090 passed through (`/proc/driver/nvidia/gpus/0000:01:00.0/information`).
- **Concrete Driver Mismatch Blocker**:
  - Host kernel module / GPU firmware is version **580.105.08**.
  - Container userspace package is `nvidia-utils 610.57.04-1`.
  - Result: `nvidia-smi` in container fails with `Failed to initialize NVML: Driver/library version mismatch`.
- **Mitigation / Staging Path**:
  - Host `pve` contains matching userspace libraries `/usr/lib/x86_64-linux-gnu/libcuda.so.580.105.08` and `/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.580.105.08`.
  - A private matched-userspace staging path in CT120 (`gpu-runtime-20260912/nvlib`) provided 580 userspace libraries to an isolated process without altering container-wide packages.
  - **CUDA Probe Status**: Private CUDA 64MiB memory allocation probe **PASSED**. Evidence pointer: [`gpu-runtime-20260912/cuda-readiness-receipt.json`](file:///home/thetu/.local/state/omp-fleet-knowledge/gpu-runtime-20260912/cuda-readiness-receipt.json) (receipt schema `omp-fleet-knowledge-cuda-readiness/v1`, 67,108,864 bytes allocated on NVIDIA GeForce RTX 5090 sm_120 via `cuMemAlloc_v2`, context cleanup verified, `cuda_ready: true`).
- **Model Inference Qualification**:
  - No model inference or residency qualification has been executed yet. CPU execution is not an approved substitute.
  - Harbor comparative benchmarks (OMP-250 / OMP-252) remain DEFERRED.

### Generator & Specialist Candidate Direction
- **Lead Local Generator Candidate**: Qwen3.8-27B (unqualified candidate), chosen for request-level thinking controls and structured output adherence.
- **Specialist Baselines**: Qwen3-Embedding-0.6B and Qwen3-Reranker-0.6B as initial efficiency baselines.
- **Active Scope**: Model installation, isolated runtime staging, and GPU qualification remain active implementation scope. Benchmarks (Harbor) are deferred.

---

## 5. Architectural Contracts & Reusable Seams

Existing contract documents (`03_NEURO_SYMBOLIC_INTAKE_TECHNICAL_CONTRACT.md`, `04_AUTONOMOUS_FLEET_KERNEL_INTAKE.md`, `03E_CONSTITUTIONAL_PLUGIN_KERNEL_TECHNICAL_CONTRACT.md`) represent **planned specifications**, not already implemented code, compiler, or outbox infrastructure.

### 5.1 Quoting Authoritative Work Evidence Contract
In `/home/thetu/omp-233-lifecycle/packages/work-client/src/index.ts`:

```typescript
export type EvidenceKind =
	| "plan"
	| "verification"
	| "audit"
	| "push"
	| "closeout"
	| "handoff"
	| "same_session_found_fixed";

export type EvidenceReceipt = {
	receipt_id: UUID;
	work_id: UUID;
	revision_id: UUID;
	candidate_id: UUID;
	kind: EvidenceKind;
	payload: Record<string, unknown>;
	payload_sha256: string;
	artifact_sha256?: string | null;
	issuer: string;
	issued_at: string;
	candidate_sha256?: string | null;
	candidate_commit?: string | null;
	verdict?: Verdict | null;
	independent?: boolean;
	remote_ref?: string | null;
	remote_commit?: string | null;
};
```

Existing utilities `payloadHash(value: unknown): string`, `canonicalJson(value: unknown): string`, and `candidateSha256(commitSha, paths, pathBasis)` in `work-client` define the canonical hashing contract. Duplicate hashing or canonical JSON utilities must not be authored.

### 5.2 Transactional Event Log & Cursor Reuse (No Second Outbox)
- The existing PostgreSQL table `omp_audit.domain_events` has an immutable trigger, RLS, and `omp_work_readonly` SELECT; it is already the authoritative committed event log.
- **No Second Outbox**: Fleet Knowledge reuses `domain_events` directly for the FK-2+ event cursor rather than minting a second outbox table or secondary event ledger.
- A cursor watermark query guarantees transaction-bound commit visibility across concurrent serializable transactions, preventing event loss from out-of-order sequence commits.

### 5.3 Engine Separation & Native Validity Authority
- **Cognee Role**: Derived index owner (knowledge graphs, vector embeddings, session lesson proposals).
- **Native Truth**: PostgreSQL Work Ledger (`omp_work`, `omp_evidence`) is the sole authority for work status, acceptance, and validity.
- **Acceptance Invariant**: Acceptance is NOT a status value returned by the memory engine. Native Work Ledger policy decides acceptance based on verified evidence receipts (`verdict=PASS`, issuer `work-service/auditor-settle`, plus applied completion event). Model outputs are proposals (`proposal` or `historical_observation`). Accepted native policy source future is not already implemented.
- **Explicit Extraction States**: An empty useful extraction from a session may be a valid `no_lesson` result; it must not be conflated with failure, nor may failures be reported as zero-item successes. Processing states must be explicit: `no_lesson`, `success`, `partial`, `failed`, `cancelled`.
- **Rebuild Path**: When derived vector or graph data is lost, corrupted, or re-indexed, the entire derived store can be rebuilt deterministically from retained native receipts, task traces, and git commits.

### 5.4 Candidate Code Snapshot Isolation & Full Fact ID Preservation (Enola)
- Enola operates as an independent code AST and symbol extractor producing standalone manifests without requiring Cognee runtime services.
- **Full Fact ID Preservation**: Child snapshot IDs namespace complete Enola `fact.id` including file (`sha256(repo\0kind\0name\0file)[:32]`) under `workspace_id:repository_id@snapshot_id`. Identically named symbols across distinct files never collide into duplicate node IDs.
- **No Stock Repo-Sweep Importer**: Stock Cognee's `_sweep_stale_code_graph` mutates repository views and breaks multi-candidate isolation; it must never be called. Retirement and cleanup delete only node IDs belonging to the specific retired snapshot.
- **A/B Isolation**: Ingesting Candidate B leaves Candidate A structural facts and evidence completely unchanged; partial or aborted attempts remain unpublished and invisible.

### 5.5 Real Cognee / Ladybug Integration Qualification Status
- Real embedded Cognee (`cognee==1.5.4`) and Ladybug (`ladybug==0.19.0`) integration tests are authored in `python/omp-knowledge/tests/test_cognee_isolation.py`.
- **Unexecuted ≠ Pass**: Integration qualification remains **unexecuted** until actual installation commands and daemon startup are performed (`cognee-c-runtime/install_and_qualify.sh`). No false PASS claim is made.

---

## 6. Implementation Slices & Multi-Worktree Coordination

### 6.1 Dual Worktree Topology
Authoring for the foundation slices is partitioned across two worktrees sharing base commit `aacabebf7c9d894ff8dca4b9fff188303b2491f4`:
1. **Primary Worktree (`/worktree`)**: Branch `fleet-knowledge/v1-20260912`. Houses Slice C engine (`python/omp-knowledge`), shared canonical contracts (`python/omp-work/src/omp_work/knowledge_contracts.py`), documentation, and intake artifacts.
2. **Isolated Worktree (`/native-a-worktree`)**: Branch `fleet-knowledge/native-reads-20260912`. Houses Slice A native exact reads in `python/omp-work` (`store.py`, `service.py`, `server.py`, `api_models.py`, `client.py`, `contract.json`, TS `work-client`).
3. **Integration Status**: Both source tracks authored under High effort. Native_a initial structural corrections are pending; no PASS claim is made; branches await integration into `main` / primary worktree.

### 6.2 Completed Fable 5.1 Advisory Candidate Plan
The architectural qualification gate completed with no authoring blockers at [`/home/thetu/.local/state/omp-fleet-knowledge/fk1-fk3-resolution.json`](file:///home/thetu/.local/state/omp-fleet-knowledge/fk1-fk3-resolution.json) (resolution receipt SHA-256 `6d512b4879e978c1bb86750dd5108a04556a472f84b816ebc39728144621521f`).
- Bounded first slice details are agreed across Slice A (native exact reads) and Slice C (`omp-knowledge` real Cognee adapter).
- Subsequent live actions (owner interactive approval via `omp-work approve`, service restart) are strictly decoupled from candidate authoring and remain gated by the stabilization window.

### 6.3 Child-Preview Scope (OMP-278 Slices S1–S6)
Under isolated child-preview scope for OMP-278:
- **Title**: Fleet Knowledge first vertical slice: native event capture, publication proof, proposals/applicability, context seam, reuse/outcome, correction (FK2/FK5/FK6 subslice).
- **Parent**: OMP-278 (`ac0c0f93-3cde-4400-8ae3-e07503e0a707`).
- **Scope**: FK2 native event capture, FK4 publication & rebuild proofs, FK5 proposals and single stage-context compiler seam, and FK6 reuse/outcome and correction/withdrawal loops.
- **Foundation Status**: OMP-279 remains scoped to FK1–FK3 foundation slices.
- **Authority & Admission**: No native admission or execution grant has occurred (`nativeWritesNow: false`, `nativeIdAllocated: false`). WorkService / Native-A contracts remain the sole authority.
- **Missing Owner Routing**: The stage-context compiler implementation is rooted in the single seam `omp_knowledge.context.compiler.ContextCompiler`. Requests utilizing the `tokens` budget method route explicitly to the missing FLEET-5 owner (returning 422 `budget_method_unavailable`). No second compiler or token estimator is permitted.

