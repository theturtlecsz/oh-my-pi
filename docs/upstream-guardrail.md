# Standing upstream-compatibility guardrail (OMP-229)

The proven 18.0.6 upgrade method (OMP-156) generalized into a recurring process:
weekly upstream discovery, a fork-behavior inventory enforced on every ordinary
pull request, a full compatibility review enforced on upstream-update pull
requests, and a guarded updater. No incorporation path bypasses the guardrail.

## Concepts

- **Accepted upstream baseline** — the immutable upstream commit most recently
  incorporated with full compatibility proof. Recorded in
  `docs/upstream-baseline.json` together with the review pins (base/fork/target
  commits, changelog version range) and the four record files that proved it.
- **Upstream candidate** — the newest stable, non-draft, non-preview upstream
  release found by weekly discovery, resolved to one immutable commit (never a
  moving branch reference).
- **Fork-behavior inventory** — `docs/upstream-fork-inventory.tsv`: one row per
  path whose content diverges from the accepted baseline commit, carrying the
  machine-verified divergence fingerprint (scope/state/head blob) and the
  human-owned behavior description and classification
  (`retained` · `re-fitted` · `dropped`; `dropped` requires an explicit
  `owner-ruling:` reference).
- **Review record** — `docs/upstream-review-<target-sha12>.json` (same schema
  as `docs/upstream-baseline.json`) plus its recorded companion files
  `docs/upstream-review-<target-sha12>-sources.tsv`, `-matrix.tsv`,
  `-changelog.tsv`, and `-handoff.md`.

## Weekly discovery

`.github/workflows/upstream-watch.yml` (Mondays, or manual dispatch) runs

```
bun scripts/upstream-discovery.ts --json
```

which lists upstream GitHub releases, filters to stable final `vX.Y.Z`
releases above the accepted baseline version, groups intervening releases into
the single newest candidate, and resolves its tag to one immutable commit via
`git ls-remote` with annotated-tag peeling. When a candidate exists the
workflow records the candidate tag, immutable commit, and baseline in the job
summary (`$GITHUB_STEP_SUMMARY`) and uploads them as a small
`upstream-candidate` JSON artifact.

## Ordinary pull requests: inventory consistency

CI (`check` job, PR-required) runs `bun scripts/upstream-inventory.ts`. The
check recomputes the divergence set `baseline.target..HEAD` via `git diff-tree`
and fails, itemized, when:

- a diverging path has no inventory row,
- a row's scope/state/blob fingerprint is stale,
- a row outlives its divergence (path no longer differs from upstream),
- a behavior description is empty, a classification is invalid, or a `dropped`
  row lacks an `owner-ruling:` reference.

A PR that changes fork behavior therefore must refresh the inventory:

```
bun scripts/upstream-inventory.ts --write
```

then edit the behavior text of the touched rows where the description changed.
`--write` preserves human columns, refreshes machine columns, seeds new rows
from commit subjects, and drops rows whose divergence disappeared. The
inventory file itself is guardrail bookkeeping and excluded from its own
divergence set (a self-row hash has no fixpoint).

## Upstream-update pull requests: full review

An upstream-update PR is any PR that modifies `docs/upstream-baseline.json`
(the acceptance act). CI additionally runs the full record review, strict:

```
bun scripts/verify-upstream-handoff.ts --record docs/upstream-baseline.json --report <file>
```

The review recomputes the frozen source manifest (`base..fork`), the record's
embedded per-path upstream-change manifest (`base..target`, `upstream_changes`
entries `"<status> <old12>><new12> <path>"` — every upstream change, including
target-only paths the fork never touched, must be enumerated), the fork matrix
coverage, the changelog ledger for `(version_min..version_max]` at the pinned
target, predicted merge conflicts (`git merge-tree --merge-base base fork
target` — every conflicted path needs a matrix row), and handoff link
completeness. Any unaccounted fork behavior, unaccounted upstream change,
unresolved conflict, or failed proof fails the PR and emits the itemized
incompatibility report, grouped by those categories, into the step summary.

## Repeatable intake

Upstream updates follow a disciplined intake procedure to incorporate upstream releases without breaking the fork's modifications, tests, or build.

### Weekly patch check

As part of weekly discovery (`.github/workflows/upstream-watch.yml`), immediately after a new stable upstream candidate commit `$C` is discovered, the workflow runs:

```
bun scripts/upstream-patch-check.ts --target $C
```

The patch check uses `git merge-tree` to test the three-way merge between the accepted baseline target, the fork HEAD, and the candidate commit. When an upstream edit touches the exact lines modified by a shared fork patch, the check fails with exit code 1, reports `BROKEN <path> [shared]`, prints the inventory behavior description, and records the broken patch in the workflow job summary. A patch-check failure warns the team before intake begins that upstream changes break an active fork patch and will require code re-fitting during integration.

### Merge, not rebase

The fork incorporates upstream releases strictly via **merge** (`git merge --no-ff`), never by rebasing the fork onto upstream. Rebasing would rewrite fork commit hashes, disrupt active worktrees, replay thousands of upstream commits through the fork history, and invalidate the immutable divergence base required by the fork-behavior inventory and handoff verification oracles. Merging preserves linear fork development while cleanly bounding upstream integration commits.

### Why intake is an owner session

Intake is executed exclusively in an owner session (by Chris or an authorized owner session), never by automated background workers, for three reasons:
1. **Flood rebase avoidance**: Automated flood worker tasks use rebase and fast-forward (`--ff-only`) merges. Running intake under flood would attempt to replay upstream commits onto task worktrees.
2. **Main branch freeze**: The `main` branch must remain frozen while the merge, conflict resolution, test repairs, and gate suites run to avoid racing concurrent changes.
3. **Deployment**: The completed intake result is deployed directly to the running environment (`bash /home/thetu/flood/deploy-omp.sh`).

### Ordered intake procedure

Run these blocks in order for one upstream intake (OMP-401-s07). Keep one shell so `C`, `V`, and `R` stay set. `$C` is the candidate's full 40-hex commit (`update.sh` rejects a short id).

Stop flood, fetch upstream, and bind `C` and `V` from discovery (`candidate_commit`, `candidate_version`):

```bash
set -euo pipefail
systemctl --user stop flood
cd /home/thetu/flood-repos/oh-my-pi
git remote get-url upstream || git remote add upstream https://github.com/can1357/oh-my-pi.git
git fetch upstream
DISC=$(bun scripts/upstream-discovery.ts --json)
C=$(printf '%s' "$DISC" | bun -e 'const j = await Bun.stdin.json(); if (j.newer !== true || typeof j.candidate_commit !== "string") throw new Error("no upstream candidate"); process.stdout.write(j.candidate_commit)')
V=$(printf '%s' "$DISC" | bun -e 'const j = await Bun.stdin.json(); if (typeof j.candidate_version !== "string") throw new Error("no candidate version"); process.stdout.write(j.candidate_version)')
git worktree add ../oh-my-pi-intake -b "intake/${C:0:12}" main
cd ../oh-my-pi-intake
bun scripts/upstream-review-seed.ts --target "$C" --version "$V"
git switch -c "scratch/${C:0:12}"
git config rerere.enabled true
git merge --no-ff "$C" || merge_rc=$?
if [ "${merge_rc:-0}" -ne 0 ]; then
	git rev-parse -q --verify MERGE_HEAD >/dev/null
fi
```

A non-zero merge that does not leave `MERGE_HEAD` stops here. When `MERGE_HEAD` is set, edit every unmerged path until the worktree holds the resolution. `rerere.enabled` records that resolution when the merge commit is created. It does not stage the files, and it does not commit. The paths stay unmerged until `git add`. After the edits:

```bash
set -euo pipefail
if git rev-parse -q --verify MERGE_HEAD >/dev/null; then
	git diff --name-only --diff-filter=U
	git add -u
	git commit --no-edit
fi
```

Commit any further test repairs as ordinary commits on `scratch/${C:0:12}`. Then run the same commands as `session-system/update.sh` gates 3–12, after the frozen install and native refresh that script runs before its gates. Each command must exit 0. Gate 12's output must contain `PASS`, and the tracked tree must still be clean:

```bash
set -euo pipefail
bun install --frozen-lockfile
bash session-system/refresh-natives.sh
bun test session-system/tests packages/work-client/test scripts/verify-upstream-handoff.test.ts
./node_modules/.bin/tsc --noEmit -p session-system
bun run check:ts
cargo fmt --all -- --check
cargo clippy --workspace --exclude brush-core --no-deps -- -D warnings
bun run test:ts
bun run test:scripts
bun run test:py
cargo nextest run --workspace --exclude brush-core --status-level=fail --final-status-level=fail
SMOKE_LOG=$(mktemp)
OMP_WORK_POSTGRES_INTEGRATION=1 bun run test:session:smoke | tee "$SMOKE_LOG"
grep -q 'PASS' "$SMOKE_LOG"
rm -f "$SMOKE_LOG"
test -z "$(git status --porcelain --untracked-files=no)"
```

Settle against that scratch commit. Settle rewrites a fork-only proof when the path is identical at the record's fork and `HEAD`, and rewrites a proof equal to `pending:session-system/update.sh gates 3-12`. Conflict rows (`pending:resolve and name the focused test`) and Breaking Changes / Removed rows (`pending:decide re-fitted or not-applicable`) stay pending, so the command exits 1 and lists them:

```bash
set -euo pipefail
R="docs/upstream-review-${C:0:12}"
bun scripts/upstream-review-seed.ts --record "$R.json" --settle --gates-passed-at "$(git rev-parse HEAD)" || test $? -eq 1
```

Exit 2 (the commit does not contain the target) stops the shell. Exit 1 is the pending list this step fills next.

Fill those listed rows before the strict review:

- In `$R-matrix.tsv`, replace `pending:resolve and name the focused test` with the focused test command that covers that conflict resolution.
- In `$R-changelog.tsv`, for each Breaking Changes or Removed row, set disposition to `re-fitted` or `not-applicable` and replace `pending:decide re-fitted or not-applicable` with the decision.

```bash
set -euo pipefail
if git grep -n -e 'pending:resolve and name the focused test' -e 'pending:decide re-fitted or not-applicable' -- "$R-matrix.tsv" "$R-changelog.tsv"; then
	echo "conflict and Breaking/Removed proofs are still pending" >&2
	exit 1
fi
bun scripts/verify-upstream-handoff.ts --record "$R.json"
```

The verifier must print `PASS`. Commit the review on the intake branch (HEAD may move past the fork pin only under `docs/upstream-review-`):

```bash
set -euo pipefail
git switch "intake/${C:0:12}"
git add "$R"*
git commit -m "OMP-401: review $V"
```

`update.sh` merges `$C` with `--no-ff` after the strict review passes. When the merge conflicts, `update.sh` exits on `git merge` (`set -e`) before it can commit. Default rerere writes the scratch resolution into the worktree and leaves the index unmerged. Cherry-pick runs only after that merge commit exists, so stage the replayed files and commit:

```bash
set -euo pipefail
if ! bash session-system/update.sh "$C"; then
	git rev-parse -q --verify MERGE_HEAD >/dev/null
	git diff --name-only --diff-filter=U
	git add -u
	git commit --no-edit
fi
SCRATCH_TIP=$(git rev-parse "scratch/${C:0:12}")
SCRATCH_MERGE=$(git rev-list --merges --ancestry-path "${C}..${SCRATCH_TIP}" | tail -n 1)
test -n "$SCRATCH_MERGE"
REPAIRS=$(git rev-list --reverse "${SCRATCH_MERGE}..${SCRATCH_TIP}" || true)
if [ -n "$REPAIRS" ]; then
	git cherry-pick $REPAIRS
fi
```

That `git diff --name-only --diff-filter=U` line still lists the replayed paths: rerere does not stage them, so the list is empty only after `git add -u`. If a listed file still contains conflict markers, edit it before `git add -u`, then run `git commit --no-edit`.

Advance the baseline by copying the review record to `docs/upstream-baseline.json` and setting `accepted_at` to today's UTC date. Refresh the inventory, reject a shared placeholder, and record the patch list:

```bash
set -euo pipefail
export R
bun -e 'const rec = await Bun.file(`${process.env.R}.json`).json(); const ordered = {}; for (const [k, v] of Object.entries(rec)) { if (k === "upstream_changes") ordered.accepted_at = new Date().toISOString().slice(0, 10); ordered[k] = v; } if (typeof ordered.accepted_at !== "string") ordered.accepted_at = new Date().toISOString().slice(0, 10); await Bun.write("docs/upstream-baseline.json", `${JSON.stringify(ordered, null, "\t")}\n`);'
bun scripts/upstream-inventory.ts --write
```

Edit the behavior text of every inventory row `--write` added or whose description changed. A shared row whose behavior still starts with `fork change (describe)` fails the inventory check. Then check, print the patch list, and commit the baseline:

```bash
set -euo pipefail
bun scripts/upstream-inventory.ts
bun scripts/upstream-inventory.ts --patches
git add docs/upstream-baseline.json docs/upstream-fork-inventory.tsv
git commit -m "OMP-401: advance baseline to $V"
```

With `$C` an ancestor of HEAD, `update.sh` runs gates 1–12:

```bash
set -euo pipefail
bash session-system/update.sh "$C"
```

Push the intake branch, open the pull request, and merge it with a merge commit (`gh pr merge --merge`). Then fast-forward the frozen `main` checkout, deploy, and start flood:

```bash
set -euo pipefail
git push -u origin "intake/${C:0:12}"
gh pr create --base main --head "intake/${C:0:12}" --title "OMP-401: intake upstream $V" --body "Incorporate upstream ${C} (${V}) by merge commit."
gh pr merge --merge
cd /home/thetu/flood-repos/oh-my-pi
git fetch origin
git switch main
git merge --ff-only origin/main
bash /home/thetu/flood/deploy-omp.sh
systemctl --user start flood
```

### Fork patches

The shared rows in `docs/upstream-fork-inventory.tsv` (`scope == shared`) represent the single canonical list of all fork patches modifying upstream-owned files.
- `bun scripts/upstream-inventory.ts --patches` lists every shared row along with its line delta (`+added -removed`), classification, and behavior description, followed by a total patch and line summary.
- The seed placeholder (`fork change (describe)`) is strictly rejected on shared rows by `checkInventory` — every shared fork patch must be accompanied by an accurate human description of its behavior and classification (`retained` · `re-fitted` · `dropped`).

### Settled gate proofs and semantic validation (E1058)

Running `--settle` records gate passage proofs in the matrix and changelog ledgers:
```
session-system/update.sh gates 3-12 passed at <commit12> (operator-recorded)
```
As noted in architecture finding E1058, this proof is an operator assertion confirming that the automated test suite executed and passed at that specific commit. It is **not** semantic validation of individual upstream behavioral changes or feature additions. Semantic validation of upstream breaking changes, removed functionality, and conflict resolutions requires human analysis, targeted test coverage, and deliberate disposition recorded in the review ledger.
