# Orchestrator entry points

Every way work can enter the deterministic orchestrator, the trigger that starts
it, and the disposition of the run. This is the D35 unattended-safety envelope:
a product entry point may run a control-plane operation or a deterministic
`ops` command, but no timer-started agent session exists (D36 scopes flood out).

The systemd units are the ones `infra/work-ledger/install.sh` writes; keep this
table in step with that unit map.

| Entry point | Trigger | Disposition |
| --- | --- | --- |
| robomp `run_task` (`triage_issue`) | `issues.opened` webhook → queue worker | `refused_unattended` |
| robomp `run_task` (`review_pr`) | incoming PR `opened`/`synchronize`/`reopened` → queue worker | `refused_unattended` |
| robomp `run_task` (`handle_comment`) | `issue_comment.created` on an issue → queue worker | `refused_unattended` |
| robomp `run_task` (`handle_pr_conversation`) | `issue_comment.created` on a PR → queue worker | `refused_unattended` |
| robomp `run_task` (`handle_review`) | `pull_request_review_comment.created` → queue worker | `refused_unattended` |
| robomp `run_task` (`handle_release_ci`) | release sentinel / failed release run → queue worker | `refused_unattended` |
| `omp-work-postgres.service` | systemd user unit (`infra/work-ledger/install.sh`) | `control_plane` |
| `omp-work-service.service` | systemd user unit (`infra/work-ledger/install.sh`) | `control_plane` |
| `omp-work-jobs-worker.service` | systemd user unit (`infra/work-ledger/install.sh`) | `control_plane` |
| `omp-work-backup.service` | `omp-work-backup.timer` (daily) | `control_plane_op` |
| `omp-work-wal.service` | `omp-work-wal.timer` (every 5 minutes) | `control_plane_op` |
| `omp-work-restore-drill.service` | `omp-work-restore-drill.timer` (monthly) | `control_plane_op` |
| `omp-events-push.service` | `omp-events-push.timer` (every 5 minutes) | `control_plane_op` |
| `omp-work-backup.timer` | systemd user timer (`infra/work-ledger/install.sh`) | `control_plane_op` |
| `omp-work-wal.timer` | systemd user timer (`infra/work-ledger/install.sh`) | `control_plane_op` |
| `omp-work-restore-drill.timer` | systemd user timer (`infra/work-ledger/install.sh`) | `control_plane_op` |
| `omp-events-push.timer` | systemd user timer (`infra/work-ledger/install.sh`) | `control_plane_op` |

Dispositions:

- `refused_unattended` — the deployment runs unattended (D35), so this agent
  task raises `UnattendedRefused` before any RPC session starts. No model runs.
- `control_plane` — the ledger's own service process (PostgreSQL, the HTTP
  service, the native jobs worker). It runs no model and no worker agent.
- `control_plane_op` — a deterministic `omp_work ops` or `omp_work events push` (ops.alarm/ops.digest push) command run by a systemd
  timer under the ledger's service identity. It reads no model and starts no
  worker session.
