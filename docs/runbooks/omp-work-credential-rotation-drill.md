# Work Ledger runner credential rotation drill (`omp_work_app`)

## Purpose

The Work Ledger's runner credential is the `omp_work_app` database role password.
Both the native jobs store (`python/omp-work/src/omp_work/jobs/store.py`) and the
WorkService (`python/omp-work/src/omp_work/v1/server.py`) connect as that role.
This drill rotates it with the existing
`omp-work ops credentials rotate omp_work_app` command and proves that a queued
mission survives the rotation, that the operator knows which systemd units must
restart to pick up the new password, and that the rotation is observable and
recoverable.

Use it:

- On a schedule, as a drill on a healthy ledger, to keep the rotation path and
  the restart set rehearsed.
- Immediately when the credential is suspected leaked. Treat that run as an
  incident: rotate first, then run the same steps and record the evidence.

## Preconditions

- A fresh session on `arch-dev`, in `/home/thetu/flood-repos/oh-my-pi-deploy`.
- The Work Ledger units are running.
- At least one mission is queued: approved, not yet running. That window is a
  mission parked at the confirm stage after approval and before the worker runs
  its next stage step. See it with the existing commands:

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
uv run --project python/omp-work omp-work ops health --json
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

If no mission is queued, enqueue one and record the returned mission id:

```sh
uv run --project python/omp-work omp-work orchestrator submit --request <request.json>
```

## Drill steps

Record the state before the rotation, so the "before" is comparable to the
"after":

```sh
date -u +%Y-%m-%dT%H:%M:%SZ
git -C /home/thetu/flood-repos/oh-my-pi-deploy rev-parse HEAD
uv run --project python/omp-work omp-work ops health --json
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

Rotate the runner role. The command never puts a credential on argv; it writes
the replacement to a mode-0600 file and applies it with `ALTER ROLE`:

```sh
uv run --project python/omp-work omp-work ops credentials rotate omp_work_app
```

Determine the restart set before touching the units. `OperationsConfig.connection_kwargs`
in `python/omp-work/src/omp_work/operations/config.py` reads the role's credential
file on every call, so a process started after the rotation reads the new
password while any process already holding the old one must restart. The unit
list in `docs/work-ledger-operations.md` names the ledger units; those that open
the runner role and hold it are:

- `omp-work-service.service` — the WorkService HTTP process.
- `omp-work-jobs-worker.service` — the native jobs worker that claims the queued
  mission.

`omp-work-postgres.service` is the database. It already accepted the `ALTER ROLE`
and must **not** be restarted.

Restart the two units that cache the credential:

```sh
systemctl --user restart omp-work-service.service
systemctl --user restart omp-work-jobs-worker.service
```

Confirm the ledger is healthy again:

```sh
uv run --project python/omp-work omp-work ops health --mode ready
```

Confirm the queued mission is picked up. The worker advances the mission — new
stage steps appear and/or the status moves past `approved`. A mission still
parked, or a worker that cannot reconnect, is a FAIL:

```sh
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

## Failure path

`credentials rotate` writes the replacement to `<role>.next` in the credentials
directory before it runs `ALTER ROLE`, and only renames it over `<role>` after
the `ALTER` commits. When the `ALTER` raises, the command fails with
`credential rotation failed; recovery credential retained` and leaves
`<role>.next` in place. Two cases:

- The database never took the new password (the `ALTER` itself failed). The live
  `<role>` file still authenticates. Delete the retained file and re-run the
  rotation.
- The database committed the `ALTER` but the process died before the rename. The
  database now wants the replacement; promote the retained file, then re-run the
  health check and restart the two units as above.

```sh
mv <config>/<role>.next <config>/<role>
chmod 600 <config>/<role>
uv run --project python/omp-work omp-work ops health --mode ready
```

`<config>` is the credentials directory, `~/.config/omp/work-ledger/credentials`;
`<role>` is `omp_work_app`. Never edit the live credential file by hand.

## Evidence record

```text evidence
Date (UTC):
Host:
Deploy sha:
Role:
Mission id:
Steps run:
Observed:
Result (PASS/FAIL):
Follow-up items:
```
