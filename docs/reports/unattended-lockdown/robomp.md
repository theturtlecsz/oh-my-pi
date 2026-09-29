# Robomp Lockdown Report

Evaluation of lockdown restrictions, execution boundaries, and credential exposure for autonomous pull request reviews and unattended agent execution in robomp.

## Overview

Robomp executes autonomous pull request reviews, automated issue fixes, and benchmark evaluations inside containerized sandbox slots. Untrusted code from external pull request heads and multi-tenant slot isolation require explicit lockdown controls to prevent credential leaks, unapproved code execution, and unauthorized extension execution.

See [README.md](./README.md) for report scope, column definitions, and upstream divergence taxonomy.

## Proposed Robomp Locks

| ID | Lock | Location | Affected runs | Breaks | Upstream divergence |
| --- | --- | --- | --- | --- | --- |
| L-RB-01 | `--no-extensions --no-skills --no-rules` on PR heads | [worker.py](../../../python/robomp/src/worker.py): `extra_args`, [git_ops.py](../../../python/robomp/src/git_ops.py): `fetch_pr_head`, [client.py](../../../python/omp-rpc/src/omp_rpc/client.py): `_build_command` | PR heads (`refs/pull/<n>/head`) run `omp --mode rpc` with only `--continue` in `extra_args`: PR `.omp` extensions, skills, rules load | Repo tooling | new |
| L-RB-02 | `--approval-mode write` in `extra_args` | [worker.py](../../../python/robomp/src/worker.py): `extra_args`, [approval.ts](../../../packages/coding-agent/src/tools/approval.ts): `resolveToolTier` | RPC uses default yolo | Bash, robomp host tools (no tier = exec) incl. submit_pr_review denied; install_headless_ui() cancels approval selects. File edits still run. | new |
| L-RB-03 | No provider keys in host `~/.omp/agent/models.container.yml` (gateway holds them); chmod no fix: slots (HOME=/srv/agent-home) read models.yml | external: | Agent home readable by all slots | Direct providers | none |

## Can a slot user read agent-home credentials?

Code answer: yes: `_stage_agent_home` ([worker.py](../../../python/robomp/src/worker.py)) makes files 0644, dirs 0755, except `.omp/run`; slots are uid 2001..2000+N ([entrypoint.sh](../../../python/robomp/entrypoint.sh)); staged: models.yml, AGENTS.md, rules ([docker-compose.yml](../../../python/robomp/docker-compose.yml)).

### Live probe (OMP-399-s05)

Verbatim output of `/home/thetu/master-report/evidence/omp-399-slot-probe.txt`:

```
robomp not deployed
```

Live answer: not deployed
