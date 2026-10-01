# WebUI oversight panel

OMP-425-s02. The `omp-webui` daemon exposes the Work Ledger's oversight snapshot
over HTTP, and its web panel renders the pending decisions, the mission list, and
a Stop control. The snapshot is built entirely from the `@oh-my-pi/pi-work-client`
client contract reads — the daemon keeps no decision or mission state of its own.

`s05` (Chris) applies this to `omp-webui`; nothing in this repository does.

## Client configuration

The daemon talks to the loopback Work Ledger as a **client principal**, never as
the owner. Three environment variables:

| Variable | Meaning |
| --- | --- |
| `OMP_WORK_BASE_URL` | Work Ledger base URL. Default `http://127.0.0.1:54322`. |
| `OMP_WORK_WORKSPACE_ID` | The workspace UUID every read is scoped to. Required. |
| `OMP_WORK_CAPABILITY_FILE` | Path to a client capability JSON. Required. |

Mint the capability and print its path:

```sh
omp-work ops capabilities client --workspace-id <workspace-uuid>
```

The file is mode `0600`; the bearer is its `token` field, and its `actor_kind` is
`client` with the `work.read`, `work.client`, and `work.stop` scopes. The daemon
must not use `owner.json` — oversight is read plus stop, never a ledger mutation.

`bun link` the package from `omp-webui` so the daemon imports the built client:

```sh
cd packages/work-client && bun link
cd ../../omp-webui/packages/daemon && bun link @oh-my-pi/pi-work-client
```

Construct one `WorkClient(baseUrl, workspaceId, () => token)` from the capability
JSON's `token`, and call the oversight module against it.

## Daemon routes

Two routes, both backed by `src/oversight.ts`:

| Method | Path | Handler |
| --- | --- | --- |
| `GET` | `/api/oversight` | `readOversight(client)` |
| `POST` | `/api/oversight/stop` | `engageOversightStop(client, body.reason)` |

`readOversight` returns `{ stop, projects }`:

- `stop` is the `stop.status` view with `changed_at` renamed to `changedAt`.
- Each project is `{ projectId, key, name, missions, pendingDecisions }`.
  - `missions` are the project's `project.status` `mission_progress` rows
    (`mission_id`, `objective`, `status`, `revision`, `updated_at`), each enriched
    from `mission.status`: `lastTransitionAt` (the last recorded transition's
    `at`), `linkedWork` (its `links` count), and `drawn` (`{usd, wall_clock_seconds}`)
    exactly as the mission sent it. The mission's `detail` is never read.
  - `pendingDecisions` are the project's `project.decisions` rows, deduplicated by
    `decision_id`, kept only while `status === "pending"`, and grouped by each
    decision's own `project_id`.

`POST /api/oversight/stop` calls `clientEngageStop` only and returns `{ stopped }`.

**No daemon decision state.** The daemon caches nothing: each request reads the
ledger, so a restarted daemon and a decision answered elsewhere are both visible
on the next call. There is no local queue of decisions and no local mission cache.

## Panel

The oversight panel shows, per project:

- the project name and key;
- each mission's objective, status, revision, linked-work count, and drawn budget;
- each pending decision's question, why it matters, options, default, and evidence
  references, with an answer control.

A **Stop** button engages the workspace stop. It confirms first — a stop halts
every agent in the workspace — then posts the reason and re-reads the snapshot;
the stop banner shows `reason` and `changedAt`.

## Test

`packages/daemon/test/oversight.test.ts` drives both routes against a `Bun.serve`
fake of the client contract and pins the two load-bearing behaviors:

- a decision answered in the fixture drops out of the snapshot a **restarted**
  daemon serves (there is no cached decision state), and
- Stop issues exactly **one** `POST …/client/stop` and makes **no** `/missions/`
  call — the snapshot's missions are unchanged by the stop.
