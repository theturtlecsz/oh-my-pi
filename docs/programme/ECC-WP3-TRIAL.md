# ECC WP3 workflow trial

Offline recorder for the WP3 pilot ([ECC-IMPLEMENTATION-SPEC.md](ECC-IMPLEMENTATION-SPEC.md) §6 WP3, §7, §8):
5–10 ordinary tasks with models and assistance held fixed, recording results, interventions, usage and workflow growth.
`scripts/ecc-workflow-trial.ts` records paths and flags. It makes no model calls, edits no settings and
records no adoption decision. It does not import any pack API; every input is a recorded path.

## What is recorded

`trial.json` under the trial root:

| Field | Content |
| --- | --- |
| `repos[]` | `id`, `name`, `is_omp`, 40-hex `baseline_commit` |
| `pack` | `install_hash`, `pack_hash` of the installed pack |
| `config.before` / `config.after` | effective `models` (role → model), `effort`, `fallback`, `advisor`, `assistance`. Must be identical; any difference, or any session using a model or effort outside them, is a fixed-config violation |
| `tasks[]` (5–10) | `work_key` (native), `repo`, `domains` (`python-postgres`, `typescript`, `rust`, `other`; several allowed), `status` (`pending`, `accepted`, `failed`, `blocked`, `abandoned`), `acceptance` (criteria ref, checks with evidence refs, optional `known_bad` candidate/evidence/`rejected`), `reuse` (intake and plan refs; refs into the existing records, no second backlog), `retrieved_rules` (`rule://` URIs), `records` (outcome, audit, closeout refs), `usage.exports`, `interventions` (kind, minutes, `unplanned_workflow_repair`), `time` (elapsed seconds, stalls), `workflow_growth` (unplanned steps, mandatory additions), `false_acceptance`, `false_blocking` |
| `stop_conditions` | the six §8 conditions, each `observed` true/false/null plus `evidence_ref` (required when true) |

Required coverage: Python/PostgreSQL, TypeScript, Rust, and at least one task in a non-OMP repo.

### Usage

`usage.exports[]` links native evidence: `{kind:"session", role, path}` for a native session JSONL (parsed with the
session loader; usage read the way the session usage statistics read it) or `{kind:"accounting", role, path}` for
`{"requests":[{"id":"...","usage":{...}|null}]}`. `role` is `primary` or `supporting` (advisors, audit, best-of-N,
retries, learning). Rules:

- Measured tokens, estimated price, premium requests, unpriced requests (tokens, no price) and unknown requests are
  reported apart. Absent usage is `unknown`, never zero; a task with no primary export is listed in `usage.unknown_tasks`.
- A request counted twice (same session linked twice, shared response id) is an error. A parent session that reports child
  spend in `task` results AND has its child session linked is an error: set `"children":"linked"` on the parent export so the
  child file is the only count.
- Failed, blocked and abandoned tasks keep their usage, interventions and time in the totals.

### Verdicts

- `check` errors (exit 1): malformed record, unresolvable path, duplicate accounting, missing evidence for a claimed false
  acceptance/blocking, known-bad check or observed stop condition.
- `check` incomplete items (exit 0; exit 3 with `--complete`): pending tasks, not 5–10 tasks, missing coverage, unknown or partial
  usage, unrecorded interventions/time/growth, config not held fixed, stop conditions not assessed.
- `summary.json` carries `complete`, `incomplete_reasons`, `fixed_config`, `fixed_config_success` (complete, config held and every
  task accepted), outcome counts with the non-accepted tasks listed, `false_acceptance` and `false_blocking` as separate
  sections (plus known-bad candidates accepted), usage, interventions, time, workflow growth and the stop conditions.

## Waiting for Chris

Chris selects five bounded tasks (Python/PostgreSQL, TypeScript, Rust, one outside OMP; a task may cover several domains) and
supplies the installed access. Implementers never wait on this: the recorder and its tests are complete without it.

1. **Init.** `bun scripts/ecc-workflow-trial.ts init --root <trial-dir> --id <trial-id>` creates `trial.json`,
   `evidence/{sessions,accounting,tasks}/` and `evidence/task.template.json` (one task skeleton to copy per task).
2. **Install.** Install the pack into the target project with the existing lifecycle:
   `bun session-system/ecc/adapter/cli.ts install --project <repo>`. Record `pack.install_hash` and `pack.pack_hash`
   from its output, the repos and baseline commits, and the effective model/effort/fallback/advisor/assistance
   configuration as both `config.before` and `config.after`. Do not change any of it until the trial ends.
3. **Per task, in the target repo, with native commands only** (the pack must not replace or shadow them):
   `/intake` (accepted criteria and the reuse record), `/plan` (approved plan), `/execute`, `/summary`, then
   `/done` (closeout). Note rules read via `rule://`, interventions with minutes, stalls, and any added step.
4. **Evidence.** Put the intake, plan, outcome, audit and closeout records, check output and rule retrievals under
   `evidence/tasks/`, link the session JSONL for the primary session and each supporting one (advisor, audit, best-of-N,
   retries) under `usage.exports`, and fill the task entry in `trial.json`. Record false acceptance and false blocking
   separately. For a known-bad candidate, link the candidate and the check output showing accept or reject.
5. **Check.** `bun scripts/ecc-workflow-trial.ts check --root <trial-dir> [--complete]` after each task; fix errors, read the
   incomplete list.
6. **Summarize.** `bun scripts/ecc-workflow-trial.ts summarize --root <trial-dir>` writes `summary.json`. Chris reads it and decides;
   the recorder decides nothing.

### §8 stop conditions

Stop or narrow the rollout, recording `observed: true` with an evidence ref, when:

- a required policy or authority boundary changes unintentionally;
- a required dependency cannot run in the qualified installation;
- a transform loses meaning, references or activation behavior;
- a change increases false acceptance or breaks recovery/cancellation;
- supporting model activity grows without useful task outcomes;
- update conflicts require a substantial independent fork of a component that was supposed to be reused.

For an isolated optional asset failure, disable that asset, keep the native workflow usable, and continue the other tasks.
