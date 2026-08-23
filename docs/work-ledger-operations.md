# Work Ledger operations

Initialize mode-0600 credentials, render the local unit, then start the isolated loopback-only PostgreSQL instance:

```sh
uv run --project python/omp-work omp-work ops credentials init
infra/work-ledger/install.sh
systemctl --user daemon-reload
systemctl --user enable --now omp-work-postgres.service
uv run --project python/omp-work omp-work ops bootstrap
uv run --project python/omp-work omp-work ops check
uv run --project python/omp-work omp-work ops health --json
```

## Contract changes

After owner approval is present, register compatibility before service restart:

```sh
uv run --project python/omp-work omp-work ops migrate
systemctl --user restart omp-work-service.service
uv run --project python/omp-work omp-work ops health --json
```

The final health result must show `live:true`, `ready:true`, and no alerts.

Provision and prove the dedicated backup target before enabling backup automation:

```sh
uv run --project python/omp-work omp-work ops backup provision-target
uv run --project python/omp-work omp-work ops backup verify-target
uv run --project python/omp-work omp-work ops backup create
uv run --project python/omp-work omp-work ops backup wal
```
Run the isolated restore drill after the first clean backup and monthly thereafter. It restores the latest completed logical backup into a disposable native PostgreSQL 18 instance, verifies every applied migration, and records the drill outcome in the source ledger:

```sh
uv run --project python/omp-work omp-work ops restore drill --reason clean-instance
uv run --project python/omp-work omp-work ops restore drill --reason monthly
```

Rotate a runtime role without putting credentials on argv:

```sh
uv run --project python/omp-work omp-work ops credentials rotate omp_work_app
```

Loss of primary is manual fencing: stop the service, preserve its PostgreSQL and WAL directories read-only, restore a verified complete backup into a fresh data directory, run `ops check` and `ops health --mode ready`, then repoint the service. The monthly drill proves the backup content and schema compatibility; it does not alter the primary.

## Authority and sealed epoch

The Work Ledger is the sole workflow authority (`work.omp.dev/v1`), operating locally on PostgreSQL. The cutover epoch is sealed; live operations run directly against the loopback WorkService. Linear history is preserved offline as static immutable exports, encrypted reports, and provenance mappings. Linear is never a fallback or recovery authority.
