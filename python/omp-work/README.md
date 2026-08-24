# omp-work

Executable, versioned `work.omp.dev/v1` Work Ledger contract plus its local PostgreSQL operator tooling, including the encrypted Linear exporter and the idempotent Linear importer (stage → reconcile → promote). It contains no WorkService server runtime cutover or backend switch.

```sh
PYTHONPATH=src python3 -m omp_work schema --check
PYTHONPATH=src python3 -m omp_work hash
PYTHONPATH=src python3 -m omp_work validate --require-approval
```

Operational bootstrap, health, and backup commands are documented in [the Work Ledger operations runbook](../../docs/work-ledger-operations.md).

Owner approval creates `src/omp_work/contracts/v1/approval.json` via `uv run --project python/omp-work omp-work approve --issue <work-key>`. It must be run by the owner in an interactive terminal after reviewing the printed digest and exact prospective JSON. Redirected or non-interactive input is refused, and agents must never mint `approval.json` from chat scope.
