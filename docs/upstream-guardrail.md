# Standing upstream-compatibility guardrail (OMP-229)

The proven 18.0.6 upgrade method (OMP-156) generalized into a recurring process:
weekly upstream discovery, a fork-behavior inventory enforced on every ordinary
pull request, a full compatibility review enforced on upstream-update pull
requests, and a guarded updater. No incorporation path bypasses the guardrail.

## Concepts

- **Accepted upstream baseline** — the immutable upstream commit most recently
  incorporated with full compatibility proof. Recorded in
  `docs/upstream/baseline.json` together with the review pins (base/fork/target
  commits, changelog version range) and the four record files that proved it.
- **Upstream candidate** — the newest stable, non-draft, non-preview upstream
  release found by weekly discovery, resolved to one immutable commit (never a
  moving branch reference).
- **Fork-behavior inventory** — `docs/upstream/fork-inventory.tsv`: one row per
  path whose content diverges from the accepted baseline commit, carrying the
  machine-verified divergence fingerprint (scope/state/head blob) and the
  human-owned behavior description and classification
  (`retained` · `re-fitted` · `dropped`; `dropped` requires an explicit
  `owner-ruling:` reference).
- **Review record** — `docs/upstream/reviews/<first-12-hex-of-candidate>/`
  containing `review.json` (same schema as `baseline.json`) plus its
  sources/matrix/changelog TSVs and handoff document.

## Weekly discovery

`.github/workflows/upstream-watch.yml` (Mondays, or manual dispatch) runs

```
bun scripts/upstream-discovery.ts --json
```

which lists upstream GitHub releases, filters to stable final `vX.Y.Z`
releases above the accepted baseline version, groups intervening releases into
the single newest candidate, and resolves its tag to one immutable commit via
`git ls-remote` with annotated-tag peeling. When a candidate exists the
workflow creates or refreshes one tracked review issue
(`Upstream compatibility review: <version>`).

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

An upstream-update PR is any PR that modifies `docs/upstream/baseline.json`
(the acceptance act). CI additionally runs the full record review, strict:

```
bun scripts/verify-upstream-handoff.ts --record docs/upstream/baseline.json --report <file>
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

## Preparing a review (candidate → record)

1. Freeze the fork commit the review covers (`git rev-parse HEAD`) and create
   `docs/upstream/reviews/<sha12>/review.json` with: `upstream_repo`,
   `upstream_version` (candidate version), `base` (previous accepted target or
   merge base), `fork` (frozen fork commit), `target` (candidate commit),
   `version_min` (first version above the accepted baseline), `version_max`
   (candidate version), and the four record file paths inside the review dir.
2. Freeze the source and upstream-change manifests once:
   `bun scripts/verify-upstream-handoff.ts --record <review.json> --write-sources`
   (writes the sources TSV and embeds the `upstream_changes` manifest in the
   record JSON).
3. Build the matrix and changelog ledger; keep `pending:<command>` proofs while
   work is in flight and iterate manually with `--allow-pending`.
4. Acceptance is strict everywhere it counts: the updater's pre-merge review,
   its post-merge gate 1, and CI's upstream-update review all run without
   `--allow-pending` — no pending proof can ride an incorporation.

Committing the review record is expected and safe: the updater allows HEAD to
differ from the record's `fork` pin only by changes under `docs/upstream/`
(guardrail bookkeeping); any other divergence refuses the merge until the
review is re-run against the current fork state.

## Guarded updater

`session-system/update.sh <full-40-hex-candidate-commit>`:

- **Pre-merge** (candidate not an ancestor): refuses to merge unless
  `docs/upstream/reviews/<sha12>/review.json` exists, pins exactly the supplied
  target with `fork` equal to the current HEAD (drift confined to
  `docs/upstream/` allowed), and passes the strict
  `verify-upstream-handoff --record …` review (no pending proofs). Only then
  `git merge --no-ff <commit>`, then stop.
- **Post-merge** (candidate is an ancestor): gate 1 re-verifies the accepted
  baseline record (strict), gate 2 checks the fork-behavior inventory, then the
  full TS/Rust/Python/PostgreSQL gate chain (gates 3–12) with a clean-tree check.

## Advancing the baseline

After a guarded incorporation is accepted and cut over, the update PR replaces
`docs/upstream/baseline.json` with the candidate's review record (plus
`accepted_at`) and regenerates the fork inventory against the new baseline in
the same PR — CI's full review gates exactly that PR. The 18.0.6 record
(`docs/upstream-18.0.6-*.tsv`, `docs/upstream-18.0.6-upgrade.md`) remains the
accepted baseline and the guardrail's known-good calibration case
(`PASS: sources=869 forkPaths=378 shared=50 changelogEntries=129 upstreamPaths=1961`).
