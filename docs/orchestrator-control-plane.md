# Orchestrator control plane

One jobs worker whose capabilities include `omp.orchestrator` is the control plane for a workspace. It holds `pg_try_advisory_lock(hashtextextended('omp_jobs.controller:' || workspace_id, 0))` for its lifetime. A second such process exits 3. While that config file `orchestrator.json` is present under `OperationsConfig.defaults().config_dir`, `parallel_streams` `tick`, `enqueue`, and `claim_one` raise `RuntimeError("control_plane_owns_admission")`.

## Authorities

Each authority is decided only by the modules named in its row.

| Authority | Modules |
| --- | --- |
| fleet admission | `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/jobs/stage_admission.py`, `python/omp-work/src/omp_work/control_plane/work_gates.py` |
| scheduling | `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/jobs/worker.py`, `python/omp-work/src/omp_work/orchestrator/stages.py` |
| leases | `python/omp-work/src/omp_work/jobs/lease.py`, `python/omp-work/src/omp_work/jobs/admission.py`, `python/omp-work/src/omp_work/control_plane/mutation.py` |
| routing | `python/omp-work/src/omp_work/routing/policy.py`, `python/omp-work/src/omp_work/jobs/admission.py` |
| retry and recovery | `python/omp-work/src/omp_work/jobs/lease.py`, `python/omp-work/src/omp_work/jobs/worker.py`, `python/omp-work/src/omp_work/orchestrator/stages.py` |
| cancellation | `python/omp-work/src/omp_work/jobs/cancel.py`, `python/omp-work/src/omp_work/control_actions.py` |
| budgets | `python/omp-work/src/omp_work/jobs/budget.py`, `python/omp-work/src/omp_work/project_store.py` |
| progress events | `python/omp-work/src/omp_work/orchestrator/step_log.py`, `python/omp-work/src/omp_work/jobs/store.py` |

## Parallel streams features

Where the file-backed loop's feature lives in the control plane. `none` means no control-plane component decides it yet.

| Feature | Component |
| --- | --- |
| in_flight_max lock | none |
| heartbeat | none |
| claim | `python/omp-work/src/omp_work/jobs/admission.py` |
| path leases | none |
| budget reserve | `python/omp-work/src/omp_work/jobs/budget.py` |
| provider partitions | `python/omp-work/src/omp_work/routing/policy.py` |

## Parallel streams consumers

Callers of `parallel_streams` that still have to move. `migrated` is yes only when that caller no longer admits through the file-backed loop.

| Consumer | Path | Migrated |
| --- | --- | --- |
| parallel-admit CLI | `python/omp-work/src/omp_work/__main__.py` | no |
| parallel-admit tests | `python/omp-work/tests/test_parallel_streams.py` | no |
| parallel-admit routing tests | `python/omp-work/tests/test_parallel_streams_routing.py` | no |
| parallel streams baseline note | `docs/programme/R00-BASELINE-2026-09-25.md` | no |
