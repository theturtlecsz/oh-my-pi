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

The exact ordered commands to execute an upstream intake (OMP-401-s07):

```bash
# 1. Stop background workers and prepare repository
systemctl --user stop flood
cd /home/thetu/flood-repos/oh-my-pi
git remote get-url upstream || git remote add upstream https://github.com/can1357/oh-my-pi.git
git fetch upstream

# 2. Discover the candidate commit C and version V
bun scripts/upstream-discovery.ts --json   # C, V (e.g. 8b25ad4a0562, 18.4.6)

# 3. Create isolated intake worktree branched from main
git worktree add ../oh-my-pi-intake -b intake/${C:0:12} main
cd ../oh-my-pi-intake

# 4. Seed the review record and companions
bun scripts/upstream-review-seed.ts --target $C --version $V

# 5. Switch to a scratch branch for integration and testing
git switch -c scratch/${C:0:12}
git config rerere.enabled true
git merge --no-ff $C   # resolve conflicts, repair tests, run gates 3-12

# 6. Settle automated proofs against the passing scratch commit
R=docs/upstream-review-${C:0:12}
bun scripts/upstream-review-seed.ts --record $R.json --settle --gates-passed-at $(git rev-parse HEAD)

# 7. Fill remaining pending proofs manually:
#    - In $R-matrix.tsv: fill conflict rows with concrete resolutions and focused test proofs.
#    - In $R-changelog.tsv: fill Breaking Changes / Removed rows with dispositions (re-fitted or not-applicable) and proof details.
#    Verify strict review passes:
bun scripts/verify-upstream-handoff.ts --record $R.json

# 8. Return to intake branch and commit the settled review record
git switch intake/${C:0:12}
git add $R* && git commit -m "OMP-401: review $V"

# 9. Execute guarded pre-merge (rerere applies scratch resolutions)
bash session-system/update.sh $C   # merge; rerere; commit

# 10. Cherry-pick repairs from scratch branch, then advance the baseline
git cherry-pick <repairs>
# Advance baseline: replace docs/upstream-baseline.json with review record plus accepted_at
# Regenerate inventory and check patches:
bun scripts/upstream-inventory.ts --write
bun scripts/upstream-inventory.ts --patches
git add docs/upstream-baseline.json docs/upstream-fork-inventory.tsv && git commit -m "OMP-401: advance baseline to $V"

# 11. Run full post-merge verification gates (gates 1-12)
bash session-system/update.sh $C

# 12. Push intake branch and merge PR (merge commit only, no squash/rebase)
git push origin intake/${C:0:12}

# 13. Fast-forward main, deploy, and resume workers
cd ../oh-my-pi && git fetch origin && git merge --ff-only origin/main
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
