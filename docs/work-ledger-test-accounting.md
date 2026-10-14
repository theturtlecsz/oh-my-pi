# Work-ledger skip accounting

The unit check at commit `5c57a261d6` reported 4054 passed and 5156 skipped. The separate integration check reported 858 passed. Those three numbers are historical totals. A report prints them and the deltas of the receipts it was given. It does not reconstruct the historical node ids.

`python -m omp_work.skip_accounting_report` reads receipts from `omp_work.skip_accounting`. It does not run pytest.

```
python -m omp_work.skip_accounting_report \
  --unit unit.json \
  --alternate postgres.json \
  --alternate installed.json \
  --alternate flood.json \
  --json report.json \
  --markdown report.md \
  --check
```

`--alternate` may be repeated. Both files are written before the exit status is chosen. `--check` exits 1 when any unit-skipped node is uncovered, any check failed, or any evidence is malformed, incomplete, or stale. Without `--check` the same problems are written into the files and the process exits 0.

Observed counts come from the receipts:

- `unit_passed`: selected unit nodes whose call outcome is `passed` and no phase is `skipped`
- `unit_skipped`: selected unit nodes with at least one skipped phase
- `integration_passed`: passed unskipped calls in accepted alternates whose check is `postgres`, `integration`, `pg`, `work-ledger integration`, or `work-ledger-integration`. The value is null when no such alternate was supplied.

Deltas are observed minus 4054, 5156, and 858. The source fingerprint is `skip_accounting.fingerprint` of the checkout, computed before the report files are written. That hash ignores directories named `reports` or `generated`, files ending in `.skip-receipt.json`, changelogs, and the inventory. A difference between the source fingerprint and the unit run fingerprint is recorded and is not, by itself, a `--check` failure. Stale evidence is an alternate whose fingerprint differs from the unit receipt.

## Execution rules

A unit-skipped node is covered only by an executed call with no skipped phase, in a complete receipt with the same `run_id` and the same fingerprint. The node must be selected and collected in that receipt.

Collection-only evidence (`--collect-only`, or any session the plugin marks incomplete) is rejected. It covers nothing.

Subset-only evidence, where the selected nodes are a proper subset of the collected nodes, does not cover a node it did not select. `pytest -k` is subset-only evidence for every node it deselected.

An alternate that also skips the node does not cover it. There is no allowlist, no waiver, and no deletion option.

A failed call stays a failed check. The node can be both executed and failing. The failure reason and phase stay in the report.

A malformed receipt, a stale fingerprint, a different `run_id`, or an incomplete receipt is rejected and covers nothing.

The first accepted alternate, in `--alternate` order, whose call executed with no skipped phase is the covering check. The report records that check's name, argv, prerequisites, phase outcomes, and the sha256 of the receipt bytes.

## Work-ledger unit tests

`.github/workflows/ci.yml`, job `work_ledger_postgres`, step "Run Work Ledger unit tests":

```
uv run --project python/omp-work --extra dev pytest python/omp-work/tests
```

`oh-my-pi-test.sh` runs the same command under the label "work-ledger unit tests".

This command does not set `OMP_WORK_POSTGRES_INTEGRATION`, `OMP_INSTALLED_RELEASE`, `OMP_INSTALLED_MANIFEST_SHA256`, or `OMP584_FLOOD_SOURCE`. Nodes that skip without those variables are not executed here. Namespace nodes whose unshare probe fails also stay skipped.

`package.json` script `test:py` is a different command. It runs `python -m pytest -x` on `python/omp-work/tests`, then the omp-rpc and robomp suites. The `-x` stop makes a failing session incomplete accounting evidence. Do not treat `bun run test:py` as the unit receipt.

## PostgreSQL integration

`package.json` script `test:py:work-ledger:integration` is the check. CI runs it as "Run PostgreSQL integration" (`bun run test:py:work-ledger:integration`). `oh-my-pi-test.sh` runs it under the label "work-ledger integration".

The script sets `OMP_WORK_POSTGRES_INTEGRATION=1` and runs:

```
uv run --project python/omp-work --extra dev pytest <allowlist>
```

The allowlist is the script's `python/omp-work/tests/*.py` arguments, not the whole test tree. `python/omp-work/tests/test_research_m1_investigation.py` is on the allowlist. Each generated report copies the current script text into the `work-ledger integration` check, so the file list stays the script's list.

Prerequisites:

- PostgreSQL 18 binaries `initdb`, `postgres`, and `pg_isready` on `PATH`. `pg_native.py` runs `initdb` and the supervisor runs `postgres`; readiness uses `pg_isready`.
- CI installs `postgresql-18` and `postgresql-client-18` when `pg_config`'s major version is not 18, and puts `pg_config --bindir` on `PATH`.
- `OMP_WORK_POSTGRES_INTEGRATION=1`. Without that flag the M1 module skips.
- A successful namespace probe for modules that also skip on `unshare`, including the M1 module. The flag alone does not execute a node whose unshare probe fails. The skip reason is then `unshare probe failed`.

A node whose file is absent from the allowlist is not run by this check.

## Managed-Python jail

`oh-my-pi-test.sh` function `jail_py_managed` (label "jailed tests, managed Python") selects these files when they exist in the checkout, in this order:

- `python/omp-work/tests/test_orchestrator_verifier_jail.py`
- `python/omp-work/tests/test_research_runner.py`
- `python/omp-work/tests/test_research_m1_investigation.py`
- `python/omp-work/tests/test_orchestrator_verifier_launch.py`
- `python/omp-work/tests/test_egress_sandbox_procfs.py`

The command runs from a copy of `HEAD`'s `python/omp-work`, extracted at `<tmp>/work/oh-my-pi`, because the verifier jail checks that path shape and does not mount `HOME`:

```
py="$(uv python find --python-preference only-managed 3.13)"
tmp="$(mktemp -d)"
mkdir -p "$tmp/work/oh-my-pi"
git archive HEAD python/omp-work | tar -x -C "$tmp/work/oh-my-pi"
(cd "$tmp/work/oh-my-pi" && uv run -q --isolated --python "$py" --project python/omp-work --extra dev pytest -q -p no:cacheprovider "${jail_files[@]}")
```

`OMP_WORK_POSTGRES_INTEGRATION` is not set. The command selects `test_research_m1_investigation.py` and does not cover the M1 DB node. That module's skipif skips every node with reason `set OMP_WORK_POSTGRES_INTEGRATION=1`.

Namespace prerequisite, shared by `test_orchestrator_verifier_jail.py`, `test_research_runner.py`, and a PostgreSQL-enabled M1 run:

```
unshare --user --map-root-user --net --mount --pid --fork true
```

The probe must exit 0. Otherwise `test_orchestrator_verifier_jail.py` skips with the probe's stderr, and `test_research_runner.py` and the M1 module skip with `unshare probe failed`.

`test_egress_sandbox_procfs.py` also skips when `unshare`, `setpriv`, `ip`, or `gcc` is missing from `PATH`, or when that same unshare probe fails (`unshare probe failed`, or `<tool> unavailable`).

`test_orchestrator_verifier_launch.py` has no PostgreSQL skip and no unshare skip. The jail runs it on the managed interpreter.

A skip from a failed probe is not coverage. The jail does not set the PostgreSQL flag, so it cannot be the check that executes the M1 DB node. The integration command is the check that selects that module with the flag set. Both the flag and the unshare probe have to pass before those nodes execute.

## Installed qualification

`scripts/installed-qualification-mode.ts` chooses the targets.

Subset targets:

- `python/omp-work/tests/test_installed_runtime_isolation.py::test_candidate_edits_and_inherited_config_do_not_change_installed_processes`
- `python/omp-work/tests/test_installed_runtime_isolation.py::test_corrupted_disposable_release_is_refused_before_runtime_launch`
- `python/omp-work/tests/test_installed_execution_recovery.py::test_killed_persisted_turn_performs_one_real_criteria_seal`
- `python/omp-work/tests/test_installed_execution_recovery.py::test_repeated_child_kill_preserves_original_task_preparation`
- `python/omp-work/tests/test_installed_execution_recovery.py::test_committed_resume_post_response_loss_reconciles_same_operation`

Full targets in the selector:

- `python/omp-work/tests/test_installed_runtime_isolation.py`
- `python/omp-work/tests/test_installed_execution_recovery.py`

Mode rules in `selectQualificationMode`:

- release (`IS_RELEASE` true, `"true"`, or `"1"`, or event `release`) selects full
- `schedule` or `workflow_dispatch` selects full
- a push whose `before` is all zeros selects full (`push with all-zero before`)
- a changed path matching `PROTECTED_PATHS` selects full
- otherwise the mode is subset, reason `no protected paths modified`
- an unknown event name throws and does not select a mode

CI job `work_ledger_postgres` stages the runtime, then resolves the mode, then runs the selected targets. Staging:

```
uv python install 3.13
installed_python=$(uv python find 3.13)
bun session-system/runtime/stage.ts \
  --source "$GITHUB_WORKSPACE" \
  --destination "$RUNNER_TEMP/omp-installed-runtime" \
  --bun "$(command -v bun)" --bun-version "$(bun --version)" \
  --uv "$(command -v uv)" --python "$installed_python" \
  --native "$GITHUB_WORKSPACE/packages/natives/native/pi_natives.linux-x64-baseline.node" \
  --native "$GITHUB_WORKSPACE/packages/natives/native/pi_natives.linux-x64-modern.node"
```

Qualification:

```
ulimit -c 0
command -v bwrap >/dev/null || sudo apt-get install -y bubblewrap
OMP_INSTALLED_RELEASE="$RUNNER_TEMP/omp-installed-runtime" \
OMP_INSTALLED_MANIFEST_SHA256="$(sha256sum "$RUNNER_TEMP/omp-installed-runtime/manifest.json" | awk '{print $1}')" \
uv run --project python/omp-work --extra dev pytest -n 3 \
  <targets from the selector> \
  --basetemp="$RUNNER_TEMP/omp-installed-runtime-test-state" \
  --junitxml="$RUNNER_TEMP/omp-installed-runtime-tests.xml"
```

When the changed-file diff fails, CI forces the two `FULL_TARGETS` files. It does not invent a third file.

`oh-my-pi-test.sh` (`qualify_installed_runtime`) uses the same selector when it has a base (`main` for a task test, `FLOOD_GATE_BASE` for a pre-batch gate). Subset mode replaces the file list with `SUBSET_TARGETS`. When it does not narrow to subset, its own default list also includes `python/omp-work/tests/test_installed_runtime_lifetime.py` if that file exists. The selector's `FULL_TARGETS` does not list the lifetime file. A subset run is subset-only evidence for every installed node it did not select.

Prerequisites:

- the staged release directory and the sha256 of its `manifest.json`
- Bun, uv, and uv-managed Python 3.13
- both Linux x64 native addons, `pi_natives.linux-x64-baseline.node` and `pi_natives.linux-x64-modern.node`
- bubblewrap (`bwrap`)
- PostgreSQL 18 (`initdb`, `postgres`, `pg_isready`), because the installed tests start a disposable server
- pytest-xdist, because the command passes `-n 3`

Missing `OMP_INSTALLED_RELEASE` or `OMP_INSTALLED_MANIFEST_SHA256` skips with `installed-process qualification requires OMP_INSTALLED_RELEASE and OMP_INSTALLED_MANIFEST_SHA256`. That skip is not coverage.

## Flood source

Five repository tests execute a copy of external Flood source. They are not on the unit command's success path: without a source they skip, and a skip is not coverage.

| Test | What a missing source does |
| --- | --- |
| `python/omp-work/tests/test_flood_independence_gate.py` | Skips when `OMP584_FLOOD_SOURCE` is unset and `infra/automation-user/omp584-flood-stage/flood.py` is absent. The reason starts `no staged flood.py to test: set OMP584_FLOOD_SOURCE to an already-patched directory`. That stage directory is not in this checkout. An explicit source that has no `flood.py` fails the test instead of skipping. |
| `python/omp-work/tests/test_flood_planner_prompt_cli.py` | Skips with `set OMP584_FLOOD_SOURCE to an already-patched Flood directory`. |
| `python/omp-work/tests/test_flood_planner_rollout.py` | Skips with `rollout qualification requires explicit OMP584_FLOOD_SOURCE`. |
| `python/omp-work/tests/test_flood_rollout_provenance.py` | Skips with `rollout qualification requires explicit OMP584_FLOOD_SOURCE`. |
| `python/omp-work/tests/test_flood_start_metrics.py` | Skips with `no deployed flood.py to test (set OMP584_FLOOD_SOURCE)` only when the variable is unset and `~/flood/flood.py` is also absent. Otherwise it copies `~/flood/flood.py`. |

Rollout and provenance tests copy the selected directory and re-apply `infra/automation-user/omp584-dependencies.patch`, `omp584-start-metrics.patch`, and `omp584-planner-prompt.patch` (the rollout test accepts `omp584-prompt.patch` when the planner-prompt patch is absent) onto that copy. They need `git` to init the temporary repository. A source that cannot take those patches fails. That failure stays a failing check.

`package.json`, `ci.yml`, and `oh-my-pi-test.sh` do not run these five files as an accounting check today.

## Waiting for Chris

Chris chooses the Flood source and the permission to commit it. This task does not choose a directory, does not copy `flood.py`, does not edit `/home/ompbot/flood/flood.py`, and does not edit `oh-my-pi-test.sh`. There is no waiver and no deletion option. Uncovered nodes stay uncovered until a pinned source executes them. Deleting a test, or adding an allowlist entry, is not a way to make the report pass.

### Source pin

Chris selects one already-patched directory that contains `flood.py` and the sibling `.py` modules and `.md` files those tests copy. The bytes have to be compatible with the current OMP-584 patches: rollout tests apply those patches to a copy, and a patch failure is a failed check, not a reason to skip.

The pin is the top-level `*.py` and `*.md` of that directory. It includes `flood.py`. It does not include secrets, `flood-state`, logs, or home configuration.

Record:

- the origin directory Chris names
- the origin revision (a git commit, or another immutable id if the directory is not a checkout)
- the sha256 of a tar of those top-level names, in sorted name order
- Chris's statement that those bytes may be committed as test-only source

The owner step that writes the artifacts, after that choice, is:

```sh
read -r -p "Approved Flood source directory: " OMP617_SOURCE
test -f "$OMP617_SOURCE/flood.py"
mkdir -p /home/ompbot/flood/tasks/evidence/OMP-617
find "$OMP617_SOURCE" -maxdepth 1 -type f \( -name '*.py' -o -name '*.md' \) -printf '%f\n' | sort | tar -cf /home/ompbot/flood/tasks/evidence/OMP-617/flood-source.tar -C "$OMP617_SOURCE" -T -
sha256sum /home/ompbot/flood/tasks/evidence/OMP-617/flood-source.tar
read -r -p "Chris approval/provenance, including origin revision and permission to commit: " OMP617_APPROVAL
printf '%s\n' "$OMP617_APPROVAL" > /home/ompbot/flood/tasks/evidence/OMP-617/source-approval.txt
sha256sum /home/ompbot/flood/tasks/evidence/OMP-617/flood-source.tar >> /home/ompbot/flood/tasks/evidence/OMP-617/source-approval.txt
```

This task does not run that procedure and does not create those artifacts.

Do not commit a private `flood.py` under `infra/automation-user/omp584-flood-stage` as a substitute. That path is a raw stage for `test_flood_independence_gate.py`, and it is absent. The pin is the source Chris approves.

### Local-gate activation

Activation is a later owner step, after the pin exists and a later slice has committed the bundle and a gate patch. This task does not apply it.

The activated check extracts only the pinned relative `.py` and `.md` files into an owned temporary directory, sets `OMP584_FLOOD_SOURCE` to that directory, and runs all five modules with the accounting plugin. The run uses the same run id and the same repository fingerprint as the unit receipt. The check name is `flood`.

```sh
OMP584_FLOOD_SOURCE=<extracted pin> \
OMP_ACCOUNTING_DIR=<dir> \
OMP_ACCOUNTING_RUN=<same run id as the unit receipt> \
OMP_ACCOUNTING_CHECK=flood \
OMP_ACCOUNTING_ROOT=<repository> \
uv run --project python/omp-work --extra dev \
  pytest -p omp_work.skip_accounting \
  python/omp-work/tests/test_flood_independence_gate.py \
  python/omp-work/tests/test_flood_planner_prompt_cli.py \
  python/omp-work/tests/test_flood_planner_rollout.py \
  python/omp-work/tests/test_flood_rollout_provenance.py \
  python/omp-work/tests/test_flood_start_metrics.py
```

`OMP584_FLOOD_SOURCE` is required so `test_flood_start_metrics.py` does not fall back to `~/flood/flood.py`. The home file is not the pin.

The flood receipt is then an `--alternate` of:

```sh
python -m omp_work.skip_accounting_report \
  --unit <unit.json> \
  --alternate <flood.json> \
  --json <report.json> \
  --markdown <report.md> \
  --check
```

A remaining skip, a subset, or a collection-only run is not activation. `--check` stays nonzero until every unit-skipped node those files account for has an executed unskipped call, and no check has failed. A product assertion failure stays a failing check. It is not suppressed, waived, or deleted.
