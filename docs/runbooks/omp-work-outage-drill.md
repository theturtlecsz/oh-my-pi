# Work Ledger outage drill (WorkService down mid-mission)

## Purpose

This drill stops `omp-work-service.service` while one mission is `running`, waits, and brings the service back. It shows what the orchestrator, the jobs worker, and the alarm units do while the HTTP listener is down, and it checks that the mission continues with no step recorded twice.

Use it:

- On a schedule, as a drill, on a healthy ledger, with one mission you can interrupt.
- When WorkService is already down. Treat that run as an incident: skip the stop step and follow [Incident](#incident).

A deploy or a host shutdown is a different procedure. `docs/work-ledger-operations.md` says to run `drain check` and wait until it exits 0 before stopping the service. This drill stops the service while a mission is still `running`.

## Preconditions

- A fresh session on `arch-dev`, in `/home/thetu/flood-repos/oh-my-pi-deploy`.
- `omp-work-postgres.service` is `active`. This drill does not stop or start PostgreSQL. `drain check`, `orchestrator status`, and `ops health` read PostgreSQL. They still answer while the HTTP listener is down.
- Exactly one mission you can interrupt is `running`. Prefer a mission that is already running. Otherwise submit the throwaway below and stop only after `drain check` lists it.

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
systemctl --user is-active omp-work-postgres.service
uv run --project python/omp-work omp-work drain check
```

`drain check` prints one JSON line. Exit 0 means `drained` is true and nothing is in flight: do not stop the service until a mission is running. Exit 1 means `drained` is false and `in_flight` lists each running mission (`mission_id`, `project_id`, `objective`, `status`, `created_at`). Copy the `mission_id` you will follow. Exit 255 means the check failed (`drain:` on stderr). Fix that before this drill.

Leave every other running mission alone. Pause or wait for those, or pick another time.

### Throwaway mission

`omp-work orchestrator submit` reads a request file through `load_request` in `python/omp-work/src/omp_work/orchestrator/service.py`. The file is the `MissionRequest` document: `mission_id`, `project_id`, `unattended`, `intake`, `scope`, `instruction`, and `work`. `intake.source.sha256` is the SHA-256 of the UTF-8 source text (`text_sha256` in `python/omp-work/src/omp_work/v1/canonical.py`).

`<workspace-id>` and `<actor-id>` are the files `~/.config/omp/work-ledger/credentials/workspace-id` and `~/.config/omp/work-ledger/credentials/operator-actor-id`. `<project-key>` is the active project key (`.work-project`, or `projects check`). Copy `project_id` from `projects show`. Use a new UUID for `<mission-id>`.

```sh
uv run --project python/omp-work omp-work projects show --workspace <workspace-id> --actor <actor-id> --key <project-key>
cat > /tmp/omp-outage-request.json <<'EOF'
{
  "mission_id": "<mission-id>",
  "project_id": "<project-id>",
  "unattended": false,
  "intake": {
    "archetype": "small_code_change",
    "source": {
      "text": "Throwaway outage drill.",
      "sha256": "257b4499f01730369ebb42ff3359b7da2bdbb1cb4abaeb7382a1d37a3eb4488b",
      "spans": []
    },
    "goal": {
      "id": "goal",
      "statement": "Throwaway outage drill."
    }
  },
  "scope": {
    "project_id": "<project-id>",
    "risk_policy": "standard",
    "approval_policy": "standard",
    "effort_policy": "standard",
    "kind": "engineering.execute"
  },
  "instruction": {
    "text": "Run the throwaway outage drill mission.",
    "provenance": {
      "channel": "cli",
      "message_ref": "outage-drill",
      "received_at": "2026-10-02T00:00:00+00:00"
    }
  },
  "work": {
    "client_ref": "outage-drill",
    "title": "Throwaway outage drill",
    "project_id": "<project-id>"
  }
}
EOF
uv run --project python/omp-work omp-work orchestrator submit --request /tmp/omp-outage-request.json
uv run --project python/omp-work omp-work drain check
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

Submit prints JSON. Continue only when `status` is `enqueued` and the following `drain check` lists that `mission_id` with `"status": "running"`. When `owner.json` sits beside the automation capability, submit sets the mission to `running`. A `refused` result means the qualification record did not accept the request. Do not stop the service. Remove `/tmp/omp-outage-request.json` when the drill is over.

## Drill

Record the before state. Keep the files until the evidence block is filled.

```sh
date -u +%Y-%m-%dT%H:%M:%SZ
git -C /home/thetu/flood-repos/oh-my-pi-deploy rev-parse HEAD
OUTAGE_START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "$OUTAGE_START" | tee /tmp/omp-outage-start.txt
uv run --project python/omp-work omp-work drain check | tee /tmp/omp-outage-drain-before.json
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id> | tee /tmp/omp-outage-status-before.json
uv run --project python/omp-work omp-work ops health --json | tee /tmp/omp-outage-health-before.json
```

Stop only the WorkService unit. `omp-work-jobs-worker.service` has `Requires=omp-work-service.service` (`infra/work-ledger/install.sh`). An explicit stop of the service also stops the worker. `Restart=always` on the worker does not start it again: systemd does not restart a process it stopped itself.

```sh
systemctl --user stop omp-work-service.service
systemctl --user is-active omp-work-service.service omp-work-jobs-worker.service omp-work-postgres.service
```

Expect `inactive` for the service and the worker, and `active` for PostgreSQL. The orchestrator is that jobs worker: the worker whose capabilities include `omp.orchestrator` (`docs/orchestrator-control-plane.md`). It has no unit of its own. On SIGTERM the worker finishes the current `tick` and exits 0. It does not drain the mission (`python/omp-work/src/omp_work/jobs/process.py`). The mission row stays `running`.

Wait 10 minutes. `omp-alarms.timer` and `omp-events-push.timer` use `OnCalendar=*:0/5`, so this window includes at least one fire of each.

```sh
sleep 600
OUTAGE_END=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "$OUTAGE_END" | tee /tmp/omp-outage-end.txt
uv run --project python/omp-work omp-work drain check | tee /tmp/omp-outage-drain-during.json
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id> | tee /tmp/omp-outage-status-during.json
journalctl --user -u omp-work-service.service --since "$OUTAGE_START" --no-pager
journalctl --user -u omp-work-jobs-worker.service --since "$OUTAGE_START" --no-pager
journalctl --user -u omp-alarms.service --since "$OUTAGE_START" --no-pager
journalctl --user -u omp-events-push.service --since "$OUTAGE_START" --no-pager
```

Record what those four journals show.

- **Orchestrator.** `orchestrator status` during the wait matches the before file: same `status`, same `steps`. No new `step_index`. `drain check` still exits 1 and still lists the mission. Both commands use PostgreSQL, so a successful status read is not proof that the HTTP listener is up.
- **Jobs worker.** The journal shows the unit stopped because the service stopped. The process exits after the current tick. It stays inactive for the rest of the wait.
- **Alarms.** `omp-alarms.service` runs `alarms watch-credentials`. It calls WorkService only when a watched credential is new or changed (`python/omp-work/src/omp_work/credential_watch.py`). With no change, the oneshot exits 0 and prints `{"signalled": []}`. With a change, the HTTP call fails, that key is left unsaved, and the next run retries it. `omp-events-push.service` runs `events push`, which reads subscriptions over HTTP and fails while `127.0.0.1:54322` is down. A failed send does not advance the push cursor (`python/omp-work/src/omp_work/event_push.py`).
- **`ops health`.** It connects to PostgreSQL as `omp_work_migrator`. A green result during the wait does not mean the HTTP service is up. Use `systemctl --user is-active` for that.

Start the service, then the worker. Starting the service does not start the worker. `Requires=` points the other way.

```sh
systemctl --user start omp-work-service.service
systemctl --user is-active omp-work-service.service
uv run --project python/omp-work omp-work ops health --mode ready
systemctl --user start omp-work-jobs-worker.service
systemctl --user is-active omp-work-jobs-worker.service
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id> | tee /tmp/omp-outage-status-after.json
uv run --project python/omp-work omp-work drain check | tee /tmp/omp-outage-drain-after.json
```

`ops health --mode ready` exits 0 when PostgreSQL readiness passes. Confirm the service unit is `active` as well.

Confirm the mission continued and no step ran twice. `python/omp-work/src/omp_work/orchestrator/step_log.py` stores each step once: the same `(mission_id, step_index, kind, idempotency_key)` either repeats that row or is refused. In `/tmp/omp-outage-status-after.json`:

- `status` is `running`, or it moved forward in the stop tick that was already in flight. It is not back at `draft`, and there is not a second `submitted` step.
- Every `step_index` appears once.
- The `steps` array from `/tmp/omp-outage-status-before.json` is a prefix of the after array. Same `kind` and `rule_id` for each shared index. New steps, if any, sit at the end with a higher `step_index`.
- The jobs worker is `active`.

A repeated `step_index` is a failed drill. Leave the service and the worker up. Put the two status files in the evidence record.

## Incident

WorkService is already down or failing. Do not run the drill stop. Do not wait 10 minutes before the restart.

An explicit `systemctl stop` stops the jobs worker. A service process that exits on its own does not: `Requires=` does not propagate that kind of stop (`BindsTo=` would). Read the worker status before you assume it is down. The worker talks to PostgreSQL, so it can keep appending steps while the HTTP listener is down.

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
date -u +%Y-%m-%dT%H:%M:%SZ
git -C /home/thetu/flood-repos/oh-my-pi-deploy rev-parse HEAD
systemctl --user status omp-work-service.service omp-work-jobs-worker.service omp-alarms.service omp-events-push.service omp-work-postgres.service --no-pager
journalctl --user -u omp-work-service.service -n 80 --no-pager
journalctl --user -u omp-work-jobs-worker.service -n 80 --no-pager
journalctl --user -u omp-alarms.service -n 40 --no-pager
journalctl --user -u omp-events-push.service -n 40 --no-pager
uv run --project python/omp-work omp-work drain check | tee /tmp/omp-outage-drain-before.json
```

For each `mission_id` in `in_flight`, record status before the restart:

```sh
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id> | tee /tmp/omp-outage-status-before.json
```

Restart the listener, then the worker if it is not `active`.

```sh
systemctl --user restart omp-work-service.service
systemctl --user start omp-work-jobs-worker.service
systemctl --user is-active omp-work-service.service omp-work-jobs-worker.service
uv run --project python/omp-work omp-work ops health --mode ready
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id> | tee /tmp/omp-outage-status-after.json
```

Apply the same step check as the drill. When `ops health --mode ready` exits 0 and both units are `active`, leave the drop-ins in place.

### Drop-in rollback

`/home/thetu/flood/deploy-omp.sh` re-points these units at the deploy worktree by writing `flood-clone.conf` under `~/.config/systemd/user/<unit>.service.d/`:

- `omp-work-service`
- `omp-work-backup`
- `omp-work-wal`
- `omp-work-restore-drill`
- `omp-jobs-shadow-tailer`

Before it replaces a drop-in, it copies the current file to `flood-clone.conf.prev`. When a re-pointed restart does not become ready, the script moves each `*.prev` back, or deletes the drop-in when no `*.prev` exists, then reloads and restarts the service.

Run this only when `ops health --mode ready` exits non-zero after the restart above. A drill that stopped and started a healthy unit does not use it.

```sh
DROP=/home/thetu/.config/systemd/user
for u in omp-work-service omp-work-backup omp-work-wal omp-work-restore-drill omp-jobs-shadow-tailer; do
  if [ -f "$DROP/$u.service.d/flood-clone.conf.prev" ]; then
    mv "$DROP/$u.service.d/flood-clone.conf.prev" "$DROP/$u.service.d/flood-clone.conf"
  else
    rm -f "$DROP/$u.service.d/flood-clone.conf"
  fi
done
systemctl --user daemon-reload
systemctl --user restart omp-work-service.service
systemctl --user start omp-work-jobs-worker.service
uv run --project python/omp-work omp-work ops health --mode ready
uv run --project python/omp-work omp-work orchestrator status --mission <mission-id>
```

Record whether a `*.prev` file was restored. When health still fails, stop. Do not edit the drop-ins by hand beyond this rollback.

## Evidence record

`Role:` is who ran the drill. `Outage start:` and `Outage end:` are the UTC timestamps around the stop and the end of the wait (or, for an incident, the time the outage was seen and the time the service was `active` again).

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
Outage start:
Outage end:
```
