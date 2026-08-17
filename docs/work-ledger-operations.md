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
Run the isolated restore drill after the first clean backup and monthly thereafter. It restores the latest completed logical backup into a disposable pinned PostgreSQL 18.3 container, verifies every applied migration, and records the drill outcome in the source ledger:

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

The coordinator drives rehearsal, the freeze window, finalization, and rollback. Its state lives at `~/.config/omp/work-ledger/cutover-state.json`; the Linear freeze marker at `$XDG_CONFIG_HOME/omp-work/linear-frozen.json`.

```sh
uv run --project python/omp-work omp-work ops cutover preflight
uv run --project python/omp-work omp-work ops cutover rehearse --ordinal 1
uv run --project python/omp-work omp-work ops cutover rehearse --ordinal 2 --retain-candidate
uv run --project python/omp-work omp-work ops cutover execute
uv run --project python/omp-work omp-work ops cutover finalize   # after the personal key is revoked in Linear settings
```

Every gate failure exits 2 with a JSON `blocked` list; nothing is half-applied.

### Recovery branches

- **Before the first WorkService mutation** (`workspace_authority.first_work_mutation_at IS NULL`): `ops cutover rollback` is the supported path. It rolls the epoch back and deletes the authority row in one transaction while Linear stays frozen, switches the selector to `--backend linear`, and removes the freeze marker last. If the marker exists but no epoch row is locatable, rollback refuses — verify no Work authority exists before removing the marker by hand.
- **After the first WorkService mutation**: rollback is refused; repair/restore is the only path. Restore a verified backup into a fresh data directory (see loss-of-primary above), `ops check`, `ops health --mode ready`, repoint the service. The epoch stays `active` until `finalize` seals it.
- **Execute interrupted mid-window**: the coordinator persists the window (epoch id, candidate pgdata, ports) immediately after activation, so a rerun of `rollback` or `finalize` can still locate the candidate database. The first-mutation timestamp, not the window record, decides legality.
- **Retained candidate drift**: if code, contract, or migration fingerprints change between rehearsal 2 and `execute`, execute refuses; discard the candidate and rerun rehearsal 2 from empty state.
