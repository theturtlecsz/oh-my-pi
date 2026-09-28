# Automation user: restricted Linux identity for flood, robomp and agents

OMP-402 moves the automated work (flood, robomp, the agent sessions they launch)
off the owner's account onto a dedicated, restricted Linux user so that an
automated mistake cannot reach the owner's credentials. The owner (Chris) still
does everything interactively as the owner's account.

Nothing here is applied by an agent: every apply step is an owner command, listed
under [Waiting for Chris](#waiting-for-chris).

## Identities and credentials

| Identity | What it is | Work Ledger | GitHub | Cloud | sudo |
| --- | --- | --- | --- | --- | --- |
| owner (`thetu`) | Chris's interactive account; owns the checkout, the Work Ledger service and its database | owner capability `~/.config/omp/work-ledger/capabilities/owner.json`; full scopes and intake publication | admin | AWS/Azure/Kubernetes credentials in its home | full |
| automation (`ompbot`) | runs flood, robomp and the agent sessions those launch | its own capability `~/.config/omp-work/automation.json`, read through `~/.config/omp-work/client.json` (`actor_kind: automation`) | fine-grained token, `theturtlecsz/oh-my-pi` only: Contents RW, Pull requests RW, Metadata R | none | only the admin list below |
| Work Ledger service | `omp-work-*` user units of the owner, PostgreSQL and backups under the owner | owner capability only | — | — | — |

The automation capability keeps the owner's five scopes (flood writes items and
appends evidence), so the only Ledger difference is the identity: the automation
never holds the owner bearer, and it cannot publish intake (the store refuses
`actor_kind: automation` for that command). Finer per-command limits are OMP-403.

The GitHub token has no Workflows, Administration or organisation permission:
it can push branches and open and merge pull requests, but cannot change branch
protection, repository settings or organisation settings (that is the owner's).
The `github-probe.py` check proves the refusals.

The automation's own `systemd --user` units need no sudo (`loginctl
enable-linger`), so its sudo list is only the robomp control wrapper:

```
/usr/local/libexec/ompbot/robomp-ctl up
/usr/local/libexec/ompbot/robomp-ctl down
/usr/local/libexec/ompbot/robomp-ctl restart
/usr/local/libexec/ompbot/robomp-ctl ps
```

`/etc/ompbot/admin-commands` is the root-owned copy of that list; the sudoers
rule is generated from it by `provision.sh`.

## Probes

Run as the automation user (its own login), from its clone:

- `python3 infra/automation-user/verify-restrictions.py --owner thetu
  --admin-commands /etc/ompbot/admin-commands` — self-check: not the owner and
  not root; no `wheel`/`sudo`/`docker`/`adm`/`root`/`lxd`/`libvirt`/`disk`
  group; none of the owner's credential paths readable or listable; the named
  sudo list matches exactly with no `ALL`; the docker socket is not writable;
  the automation home and environment hold no cloud credentials; the Ledger
  bearer is outside the owner's home with `actor_kind` not `owner`. Prints JSON
  `{check: {"ok", "detail"}}` and exits 0 only when every check is ok.
- `GH_TOKEN=$(gh auth token) python3 infra/automation-user/github-probe.py
  --repo theturtlecsz/oh-my-pi --branch main` — the token can push but every
  admin read (branch protection) and every settings write (repo, org) is
  refused; it sends no write at all once the token shows administration rights.

## Parallel test

`infra/automation-user/parallel-test.sh --owner thetu --admin-commands
/etc/ompbot/admin-commands --repo theturtlecsz/oh-my-pi --flood-dir ~/flood
--flood-config ~/flood/flood.fixture.json --evidence-dir ~/ev` proves the
automation user can run the whole stack on its own without touching the owner
or `/home/thetu/oh-my-pi`. It refuses (exit 2, nothing written) when run as the
owner's uid or as root, or from a checkout inside the owner's home, then runs
the probes, `robomp-ctl up`/`ps` (with `down` on exit even after a failure), one
`omp -p` session, one `claude -p` session and a flood fixture run, keeping going
after a failure. It writes `<evidence-dir>/summary.json` `{step: rc}` and exits
0 only if every step is 0.

## Cutover

`infra/automation-user/cutover.sh` moves flood's units from the owner to the
automation user and has a written rollback (`cutover-units.tsv` is the list).

Preconditions — do not start until all hold:

1. **Flood idle**: `python3 ~/flood/flood.py status` shows no running job.
2. **Parallel test passed**: `summary.json` has every step `rc` 0.
3. **Flood port done**: the flood operator applied the `OMP-402 flood port`
   section of `NOTES-EXTERNAL.md` (its paths, its Ledger bearer, its unit
   install state).

Commands (run by the owner from the clone):

```
S=~/flood/owner-evidence/OMP-402/cutover.json
bash infra/automation-user/cutover.sh plan     --user ompbot --state-file $S
bash infra/automation-user/cutover.sh apply    --user ompbot --state-file $S
bash infra/automation-user/cutover.sh rollback --user ompbot --state-file $S
```

- `plan` prints the owner units (with their current enabled/active state) and
  the automation units, and changes nothing.
- `apply` refuses, mutating nothing, when the state file already exists, when an
  automation unit is not installed for `ompbot` (`sudo systemctl --user -M
  ompbot@ cat <unit>` fails), or when an owner `robomp` container is running.
  Otherwise it saves each owner unit's `is-enabled`/`is-active` to the state
  file, `systemctl --user disable --now` stops and disables the owner units,
  then `sudo systemctl --user -M ompbot@ enable --now` enables and starts the
  automation units. Any automation unit that is not active afterwards fails the
  cutover with exit 1 and prints
  `run: cutover.sh rollback --state-file <F>`.
- `rollback` disables and stops the automation units, then restores every owner
  unit to its saved `is-enabled`/`is-active` exactly (including units that were
  enabled but inactive, or disabled but active) and records `rolled_back_at` in
  the state file. It is idempotent, so it can be rerun.

Rehearse `apply` then `rollback` before the real cutover; `systemctl --user
list-units 'flood*' --all` must match the pre-rehearsal listing afterwards.

## Waiting for Chris

Owner steps only (agents prepare, test and document them):

1. **OMP-402-s07** — create `ompbot` and its credentials: `sudo bash
   infra/automation-user/provision.sh --user ompbot --owner thetu --repo-url
   https://github.com/theturtlecsz/oh-my-pi.git`; mint and install the
   automation capability and its `client.json`; create and install the GitHub
   token and the robomp `.env`; log the user into `claude` and `omp`; install
   the flood fixture.
2. **OMP-402-s08** — run the parallel test and a real push/pull-request probe;
   rehearse `rollback`; then, once the flood port below is in place, apply the
   real cutover and confirm the automation's units and `robomp.service` are
   active while the owner's are inactive and disabled.

Owner decisions that stay with Chris: whether to cut over at all, the GitHub
token's permissions, and the contents of the admin list.
