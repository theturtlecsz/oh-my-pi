# Work Ledger runner credential rotation drill (`omp_work_app`)

## Purpose

The Work Ledger's runner credential is the `omp_work_app` database role password.
Both the native jobs store (`python/omp-work/src/omp_work/jobs/store.py`) and the
WorkService (`python/omp-work/src/omp_work/v1/store.py` / `python/omp-work/src/omp_work/v1/server.py`)
connect as that role. This drill rotates it with the existing
`omp-work ops credentials rotate omp_work_app` command and proves that a queued
mission survives the rotation, that the operator knows which systemd units must
restart to pick up the new password, and that the rotation is observable,
verified, and recoverable.

Use it:

- On a schedule, as a drill on a healthy ledger, to keep the rotation path and
  the recovery procedure rehearsed.
- Immediately when the credential is suspected leaked. Treat that run as an
  incident: rotate first, then run the same steps and record the evidence.

## Preconditions

- A fresh session on `arch-dev`, in `/home/thetu/flood-repos/oh-my-pi-deploy`.
- The Work Ledger units are running (`omp-work-postgres.service` and `omp-work-service.service`).
- At least one mission is queued: its status is `approved` and its next stage job
  has not yet been claimed by a worker (not yet `running`). This required window
  is `approved`, not `awaiting_confirmation` (which parks before approval and
  leaves the mission unapproved). See open and approved missions with existing
  commands:

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
uv run --project python/omp-work omp-work ops health --json
uv run --project python/omp-work omp-work projects show --workspace <workspace-id> --actor <actor-id> --key <project-key>
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

`<workspace-id>` and `<actor-id>` are read from `~/.config/omp/work-ledger/credentials/workspace-id`
and `~/.config/omp/work-ledger/credentials/operator-actor-id`.
`<project-key>` is the key of the active project (for example from `.work-project`
or verified via `projects check`).
In the output of `projects show`, look at the `open_missions` array to find a
mission with `"status": "approved"`, copy its `mission_id`, and verify with
`orchestrator status --mission <mission-id>` that the mission is `approved` and
awaiting worker pickup.

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

Determine the restart set before touching any units.
`OperationsConfig.connection_kwargs` in `python/omp-work/src/omp_work/operations/config.py`
reads the role's credential file on every call (`read_secret`), without caching.
Both the WorkService store (`python/omp-work/src/omp_work/v1/store.py`) and
`NativeJobStore` (`python/omp-work/src/omp_work/jobs/store.py`) open a new
`omp_work_app` connection per transaction, so running processes read the updated
password on their next connection automatically.
The jobs worker's only long-lived `omp_work_app` session is the advisory-lock
connection established at startup in `python/omp-work/src/omp_work/jobs/process.py`;
PostgreSQL keeps that session open and valid after `ALTER ROLE`.

Checking the unit list in `docs/work-ledger-operations.md`:
Only `omp-work-postgres.service` and `omp-work-service.service` are named.
(The jobs worker is not in that unit list).
- `omp-work-postgres.service` is the database; it committed the `ALTER ROLE`
  and must **not** be restarted.
- `omp-work-service.service` connects per transaction and does not require a restart
  to pick up the new password.

Therefore, the required restart set is empty: **no units must restart to pick up
the new password**. If the operator bounces `omp-work-service.service` as a
routine verification of clean process startup:

```sh
systemctl --user restart omp-work-service.service
```

Confirm the ledger is healthy again:

```sh
uv run --project python/omp-work omp-work ops health --mode ready
```

Confirm the queued mission is picked up. When the jobs worker claims the queued
job for the approved mission, it advances the mission: `status` transitions from
`approved` to `running`, and new stage steps appear in `steps`. A status that has
moved to `running` confirms that the worker successfully reconnected using the
new credential:

```sh
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

## Failure path

`credentials rotate` writes the replacement to `<role>.next` in the credentials
directory (`<config>/credentials/<role>.next`) before it runs `ALTER ROLE`, and
only renames it over `<role>` after the `ALTER ROLE` transaction commits.
`<config>` is `~/.config/omp/work-ledger/credentials`; `<role>` is `omp_work_app`.

Two distinct failure cases:

- **Transaction failure / rollback (`ALTER ROLE` failed)**:
  When the `ALTER ROLE` transaction raises an exception, the transaction rolls
  back and the command fails with:
  `credential rotation failed; recovery credential retained`
  The database rolled back the change and still holds the password in the live
  `<role>` file. The replacement in `<role>.next` was rejected by the database.
  Do **not** move `<role>.next` over `<role>` (that would overwrite the working
  password with a rejected one; `ops health` connects as `omp_work_migrator` so
  it would falsely stay green while WorkService fails).
  Delete the rejected file and re-run rotation:

```sh
rm <config>/<role>.next
uv run --project python/omp-work omp-work ops credentials rotate omp_work_app
```

- **Crash after commit (process died before rename)**:
  The database committed the `ALTER ROLE`, but the process terminated before
  `os.replace` renamed `<role>.next` over `<role>`. In this case, no Python
  error is raised, but PostgreSQL now expects the replacement password while the
  live `<role>` file retains the old password.
  Promote the retained file over the live file, verify health, and confirm that
  `omp_work_app` can authenticate:

```sh
mv <config>/<role>.next <config>/<role>
chmod 600 <config>/<role>
uv run --project python/omp-work omp-work ops health --mode ready
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

Never edit credential files by hand.

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
