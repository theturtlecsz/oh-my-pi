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

## Worker launch contract

Production orchestrator workers run headless in an isolated sandbox jail via exact argv `["/opt/omp/bin/omp", "orchestrator-worker"]`. See [docs/orchestrator-worker.md](orchestrator-worker.md) for the complete launch contract, token/manifest injection, scoped tools, evaluate stage, and loopback model proxy configuration.

## Verifier launch contract

Production audit launches the verifier through `run_worker` with exact argv `["/usr/bin/python3","-P","-m","omp_work.orchestrator.verify_main"]`, not the test launcher. Its cwd is `/work`; the candidate checkout is read-only, while `/work/.omp` is the writable audit directory. Linked git metadata and trusted `PYTHONPATH` runtime roots are mounted read-only, with `PYTHONSAFEPATH=1` so the candidate cannot shadow the verifier runtime.

The verifier reads `.omp/verify-task.json`, publishes `.omp/audit-report.txt`, then writes `.omp/verdict.json`. The outer verifier receives a read-only signing-key bind; inner `run_tests` runs against a frozen, read-only snapshot with no key bind. The launch timeout is the remaining stage lease, which also bounds the task's test timeout. A signed PASS settles only for the frozen candidate digest and attempt.

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
