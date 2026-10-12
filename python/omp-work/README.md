# omp-work

Executable, versioned `work.omp.dev/v1` Work Ledger contract plus its local PostgreSQL operator tooling, including the encrypted Linear exporter and the idempotent Linear importer (stage → reconcile → promote). It contains no WorkService server runtime cutover or backend switch.

```sh
PYTHONPATH=src python3 -m omp_work schema --check
PYTHONPATH=src python3 -m omp_work hash
PYTHONPATH=src python3 -m omp_work validate --require-approval
```

Operational bootstrap, health, and backup commands are documented in [the Work Ledger operations runbook](../../docs/work-ledger-operations.md).

Owner approval creates `src/omp_work/contracts/v1/approval.json` via `uv run --project python/omp-work omp-work approve --issue <work-key>`. It must be run by the owner in an interactive terminal after reviewing the printed digest and exact prospective JSON. Redirected or non-interactive input is refused, and agents must never mint `approval.json` from chat scope.

## Jobs worker

Worker configuration JSON specifies execution parameters and handler dispatch:

- `capacity`: Resource-weight budget for job admission, not a concurrency setting. A job is claimable when its weight (`cpu` + `memory_mib` + `gpu` + `model_calls`) fits within capacity minus the weight of the worker's unreleased reservations. Workers claim and execute jobs serially one at a time whatever the value (probe jobs have weight 0, so capacity never limits probes).
- `handlers`: Mapping of job kinds or capability names to handler specifications (e.g. `omp.probe` -> `omp_work.jobs.probe:factory`).

## Research campaigns

Run a Work Ledger research campaign on an item through from draft to concluded:

```sh
uv run --project python/omp-work omp-work research run --key <KEY> --definition <FILE> --client-config <client.json>
```

- **Definition (`<FILE>`)**: specifies `objective`, `domain` (e.g. `machine_learning`), optional `timeout_seconds`, `criteria` array (`metric`, `op` in `gte`/`gt`/`lte`/`lt`/`eq`, `threshold`), and `trials` array referencing harness scripts (`harness: "path/to/script.sh"`).
- **Client config (`<client.json>`)**: JSON document containing `base_url`, `workspace_id`, and `bearer_file`. Defaults to `$XDG_CONFIG_HOME/omp-work/client.json`.
- **Scopes**: the bearer file must authorize `work.read`, `work.execute`, and `work.approve`.
- **METRIC scoring**: trial harnesses run under `bash` and emit lines formatted as `METRIC <name>=<value>`. A harness must exit 0; non-zero exit refutes the campaign even if printed metrics satisfy criteria.
- **States & conclusion**: campaigns transition draft → admitted → running → evaluating → concluded. Terminal outcome is `supported` when all criteria pass across all trials, or `refuted`.
- **DB flags**: `--config-dir`, `--state-dir`, `--data-dir`, and `--db-port` must all be set or all omitted. Role credentials `omp_work_app` and `omp_work_migrator` from `<config_dir>/credentials/` are used; no host credentials are required. An optional `--harness-dir` sets the unpack destination.
- **Output**: prints a single JSON object with `campaign_id`, `state: "concluded"`, `outcome`, `reason`, and `trials` (each with `trial_id`, `execution_status`, and `metrics`), exiting 0 on conclusion.
- **No resume**: each invocation creates and runs a new campaign from draft to concluded; it does not resume prior campaigns.
