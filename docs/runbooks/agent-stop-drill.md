# Agent stop drill (OMP-405-s08)

## Purpose

This drill engages the workspace stop from the WebUI Stop control and checks the host reaction. It is the rerun of the 2026-10-02 drill (OMP-447). Run it in a quiet moment: stopping `flood.service` ends every flood job.

Callers in this drill:

- The owner credential, from `ops capabilities init`. It holds `work.stop`. Release is owner-only.
- The WebUI client credential, from `ops capabilities client --name client`. Its scopes are `work.client`, `work.read`, and `work.stop`. The oversight daemon reads `OMP_WORK_CAPABILITY_FILE` as this file (`docs/webui-oversight.md`).

Grok Bot is outside this drill (owner ruling 2026-10-10). OMP-490, OMP-503, and OMP-514 stay cancelled. No step runs `ops capabilities grokbot`, writes a capability named grokbot, or passes a grokbot bearer to Stop or to `stop release`.

## Preconditions

- A fresh owner session on the host, in `/home/thetu/flood-repos/oh-my-pi-deploy`.
- `deploy-omp.sh` has deployed main with OMP-405 s01–s07 and OMP-536.
- `flood.service` is `active` before the drill.
- `${XDG_CONFIG_HOME:-$HOME/.config}/omp-work/client.json` exists and has `workspace_id`, `owner_id`, and `bearer_file`.
- Chris is asked only when a step needs his own login in a web page. Tell Chris afterwards (D47).

## Steps

### 1. Owner credential gains `work.stop`

`ops capabilities init` rotates the owner token. Copy the current owner bearer aside before the command returns.

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
C="${XDG_CONFIG_HOME:-$HOME/.config}/omp-work/client.json"
W=$(jq -r .workspace_id "$C")
OWNER=$(jq -r .owner_id "$C")
OWNER_FILE=$(jq -r .bearer_file "$C")
cp -a "$OWNER_FILE" "${OWNER_FILE}.bak-$(date -u +%Y%m%dT%H%MZ)"
uv run --project python/omp-work omp-work ops capabilities init --workspace-id "$W" --owner-id "$OWNER"
```

After this command, `client.json`'s `bearer_file` is the new owner capability. That file is the release credential. The WebUI daemon does not read it.

### 2. WebUI client capability

Mint the client principal when `${XDG_CONFIG_HOME:-$HOME/.config}/omp/work-ledger/capabilities/client.json` is missing, or when its `actor_kind` is anything other than `client`, or when its `scopes` are anything other than `work.client`, `work.read`, and `work.stop`. Minting replaces the token. Restart the WebUI daemon after a mint so it loads the new file.

The client command has no `--stop-only` flag. That flag stores `work.stop` alone, and the panel's reads need `work.read` and `work.client` as well.

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
C="${XDG_CONFIG_HOME:-$HOME/.config}/omp-work/client.json"
W=$(jq -r .workspace_id "$C")
uv run --project python/omp-work omp-work ops capabilities client --name client --workspace-id "$W"
CAP="${XDG_CONFIG_HOME:-$HOME/.config}/omp/work-ledger/capabilities/client.json"
stat -c '%a' "$CAP"
jq -c '{actor_kind, scopes}' "$CAP"
```

`stat` prints `600`. `jq` prints `actor_kind` `client` and scopes `["work.client","work.read","work.stop"]`.

Point the daemon at that file:

| Variable | Value |
| --- | --- |
| `OMP_WORK_BASE_URL` | `http://127.0.0.1:54322` |
| `OMP_WORK_WORKSPACE_ID` | `$W` |
| `OMP_WORK_CAPABILITY_FILE` | `$CAP` |

### 3. Install guards

Guard `flood.service`, `flood-operator.service`, and `continuous-admit.service`. Leave `media-watch-agent.service` out (OMP-527, Media Discovery). Add a robomp unit only when that unit is deployed.

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
uv run --project python/omp-work omp-work stop install-guards --unit flood.service --unit flood-operator.service --unit continuous-admit.service
systemctl --user daemon-reload
systemctl --user enable --now omp-agent-stop.service
```

Each guarded unit has `50-omp-agent-stop.conf`. Where the unit file or an earlier drop-in sets `KillMode=process` or `KillMode=none`, `zz-omp-agent-stop-killmode.conf` is present and sorts after that drop-in. `systemctl --user show <unit> -p KillMode --value` then prints `control-group`.

### 4. Update the agent tree

```sh
git -C /home/thetu/oh-my-pi pull --ff-only
```

### 5. WebUI Stop control

`docs/webui-oversight.md` is the control. The daemon calls `clientEngageStop` with the client bearer from step 2, and the panel shows stop status. When the control is already deployed, record its git SHA. When it is not, apply that document in `omp-webui`, commit, deploy, and record the SHA. Chris is asked only when that step needs his own login in a web page.

### 6. Run the drill

Save the transcript to `~/.local/state/omp-work/omp-447-drill-<UTC stamp>.txt` (`date -u +%Y%m%dT%H%MZ`).

Record each guarded unit's cgroup processes before Stop. `ControlGroup` is the path under `/sys/fs/cgroup`. A missing cgroup directory means no processes. Any other read error fails the check.

```sh
for u in flood.service flood-operator.service continuous-admit.service; do
  cg=$(systemctl --user show -p ControlGroup --value "$u")
  echo "$u $cg"
  if [ -n "$cg" ] && [ -r "/sys/fs/cgroup${cg}/cgroup.procs" ]; then
    echo "$u procs:"
    cat "/sys/fs/cgroup${cg}/cgroup.procs"
  fi
done
```

Press Stop in the WebUI. Then:

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
date -u +%Y-%m-%dT%H:%M:%SZ
uv run --project python/omp-work omp-work stop status
```

`stopped` is true. Within 30 seconds, `systemctl --user is-active flood.service continuous-admit.service` prints `inactive` for both. Read the cgroup process lists again. No process of a guarded unit remains. Run 1's `is-active` line alone missed the worker that `KillMode=process` left behind.

Start while stopped. `ExecCondition` runs `stop check`, which exits 1, and systemd skips the start:

```sh
systemctl --user start flood.service
systemctl --user is-active flood.service
```

`flood.service` stays `inactive`.

An `omp` prompt is refused before any provider call. The message names the stop (`Agent stop engaged`):

```sh
omp -p 'say ready'
```

When a terminal is available, repeat with an interactive prompt and record the same refusal.

Release as the owner. This command takes no `--bearer-file`, so it uses the owner capability `client.json` names:

```sh
cd /home/thetu/flood-repos/oh-my-pi-deploy
uv run --project python/omp-work omp-work stop release --reason drill-over
systemctl --user start flood.service continuous-admit.service
systemctl --user is-active flood.service continuous-admit.service
```

Both units are `active` only after this start.

Read `NRestarts` for `continuous-admit.service` immediately, wait 120 seconds, and read it again. An increase is the OMP-537 restart loop. Compare the two readings from after the start.

`flood-operator.service` is timer-driven. Record the timer's next elapse after release. The evidence for that unit is `Result=success` and `ExecMainStatus=0` on its first run after release, read on a later pass. This drill does not start an operator pass.

## Mark done

After the transcript file exists and the checks above passed:

```sh
python3 ~/flood/flood.py mark-done OMP-405-s08 --ref <omp-webui SHA>:<drill output file>
```

## Evidence record

```text evidence
Date (UTC):
Host:
Deploy sha:
Role:
WebUI sha:
Client capability path:
Client scopes:
Steps run:
Observed:
Result (PASS/FAIL):
Follow-up items:
Drill file:
```

`Client scopes:` is `work.client work.read work.stop`. `Drill file:` is the transcript path. `Result (PASS/FAIL):` is PASS when the transcript shows: `stopped` true and `flood.service` and `continuous-admit.service` inactive within 30 seconds of Stop from the WebUI, with no process left in those units; a start skipped while stopped; the omp prompt refused while stopped, with the message naming the stop; the units active only after the owner release and the following start; `NRestarts` unchanged across the 120 second wait.
