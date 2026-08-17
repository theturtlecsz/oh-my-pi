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

Loss of primary is manual fencing: stop the service, preserve its PostgreSQL and WAL directories read-only, restore a verified complete backup into a fresh data directory, run `ops check` and `ops health --mode ready`, then repoint the service. Never down-migrate, auto-fail over, or re-enable Linear during recovery. The monthly drill proves the backup content and schema compatibility; it does not alter the primary.

## Cutover (HOME-148)

The coordinator drives rehearsal, the freeze window, managed-service promotion, finalization, and rollback. Its state lives at `~/.config/omp/work-ledger/cutover-state.json`; the Linear freeze marker at `$XDG_CONFIG_HOME/omp-work/linear-frozen.json`. `execute` promotes the retained candidate onto the standard PostgreSQL/HTTP ports, enables `omp-work-postgres.service` and `omp-work-service.service`, proves `/v1/health/ready`, and only then swaps the session-system selector.

```sh
map=infra/work-ledger/linear-import-map.json
plan=/absolute/path/to/approved-HOME-148-plan.md
uv run --project python/omp-work omp-work ops cutover preflight --mapping-file "$map"
uv run --project python/omp-work omp-work ops cutover rehearse --ordinal 1 --mapping-file "$map"
uv run --project python/omp-work omp-work ops cutover rehearse --ordinal 2 --retain-candidate --mapping-file "$map"
uv run --project python/omp-work omp-work ops cutover execute --mapping-file "$map" --plan-file "$plan"
uv run --project python/omp-work omp-work ops cutover finalize   # after the personal key is revoked in Linear settings
uv run --project python/omp-work omp-work ops cutover status
```

Before activation, every failed gate returns Linear to sole authority, archives the poisoned candidate under the state directory, and removes rehearsal 2 admission. After activation, failures stay frozen and report `repair_required`; they never guess that Linear is authoritative.

### Recovery branches

- **Before activation**: Linear auto-unfreezes. The failed candidate and final-delta chain are retained as evidence but cannot be reused. Run a fresh rehearsal 2 before another execute.
- **After activation, before the first WorkService mutation** (`workspace_authority.first_work_mutation_at IS NULL`): `ops cutover rollback` is the supported path. It rolls the epoch back and deletes the authority row in one transaction while Linear stays frozen, stops/disables the managed WorkService units and backup timers, switches the selector to `--backend linear`, and removes the freeze marker last. If the marker exists but no epoch row is locatable, rollback refuses — verify no Work authority exists before removing the marker by hand.
- **After the first WorkService mutation**: rollback is refused. Re-run the same `ops cutover execute` command; database authority plus the nominated request stamp selects post-write recovery. Recovery may run after T+60 but performs no new production mutation: it reconstructs an interrupted attestation receipt, uses disposable clones for mutation smoke, and only verifies production focus. If the authoritative database is damaged, restore a verified backup into a fresh data directory, run `ops check` and `ops health --mode ready`, repoint the managed unit, then resume execute. The epoch stays `active` until `finalize` seals it.
- **Unknown authority**: `ops cutover status` reports `authority: unknown` when the candidate database cannot be queried while Linear is frozen. Keep the marker and selector unchanged; restore database reachability before choosing rollback or repair.
- **Retained candidate drift**: if code, contract, migration, or imported source fingerprints change between rehearsal 2 and `execute`, execute refuses. Discard the candidate and rerun rehearsal 2 from empty state.
