# ECC packs: pinned mirror and OMP adapter

This directory holds a pinned mirror of upstream [ECC](https://github.com/affaan-m/ECC.git)
and the deterministic adapter that turns a small selected subset of it into two
installable packs for this repository. Nothing here fetches the network or needs
a git submodule: the pinned upstream bytes are vendored under `mirror/`, so every
invariant below is verifiable offline with `bun test`.

## Layout

| Path                       | Purpose                                                                                                                                  |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `mirror/`                  | Upstream bytes at the pinned revision, one file per declared asset. This is the source of truth for the upstream sha256 pins.             |
| `manifest.json`            | Upstream pin, the two packs, every asset record (target, adaptation kind, disposition, dependencies), reserved native command names, and disposition rules. |
| `adapter/`                 | The deterministic transform, overlay engine, install plan, and install/update/remove lifecycle.                                          |
| `adapter/emit-lock.ts`     | Build step: rebuilds `adapted.lock.json` from the mirror.                                                                                |
| `adapter/upstream-check.ts` | Compares a candidate checkout with `mirror/` and prints one JSON report. Each file row has `status`. An overlay conflict clears when the candidate primary bytes equal the `mirror/` bytes. |
| `adapter/repin.ts`         | Applies an accepted review. It copies the changed mirror files and writes `reviews/<candidate commit>.json`. It does not copy `newFiles`. |
| `adapter/review.ts`        | Validates an `UpdateReview` and refuses unknown keys. A review change row has no `status` field.                                         |
| `adapter/pin.ts`           | Builds the lock: `manifestSha256` and the review pin. `emit-lock.ts --check` runs that check.                                            |
| `reviews/`                 | Accepted reviews as `<candidate commit>.json`, written by `repin.ts` and pinned by the lock `review` field. Absent until the first re-pin. |
| `adapted.lock.json`        | Committed record of each asset's upstream sha256, adapted sha256, and owned files.                                                       |
| `overlays/`                | Reviewed `find`/`replace` edits applied after the metadata transform, pinned to both the upstream and adapted sha256.                    |
| `advisor/`                 | The static read-only policy prepended to the database advisor's instructions.                                                            |

Upstream pin: repository `https://github.com/affaan-m/ECC.git`, commit
`8321021c54d670126ce3b2969d5deb880b4b0c2a`, tree
`a7489fb4da00fc7b4995df3a3c59c018d08a3807`, version `2.2.1`
(`v2.2.1-33-g8321021c`), MIT.

## Packs

- **`ecc-engineering`** — the two coding-style rules, the search-first and
  verification-loop skills, and the database-reviewer advisor adaptation.
- **`ecc-research`** — the two coding-style rules plus the research, deep-research,
  MLE, benchmark-optimization, agent-self-evaluation, eval-harness, and
  scholar-evaluation skills.

Assets are only ever installed through a declared pack; arbitrary subsets are not
part of the contract.

## Adaptation kinds

| Kind                        | What it does                                                                                                                     |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `rule-metadata`             | Emits OMP frontmatter (`description`, `globs`) and rewrites relative links to the installed targets; body is preserved verbatim.  |
| `skill-namespaced`          | Renames the skill to `ecc-<upstream name>` so it can never claim a bare native name; helper files install verbatim.               |
| `agent-metadata`            | Emits a read-only OMP task agent (`ecc-` prefix, read/grep/glob only, disabled).                                                  |
| `database-advisor-profile`  | Emits a project `.omp/WATCHDOG.yml` roster entry: read-only tools, `enabled: false`, static policy prepended.                     |

Every installable asset's mirror bytes must hash to its manifest pin, and the
transform must reproduce the adapted sha256 recorded in `adapted.lock.json`.

## Dispositions

`supported-without-changes` and `supported-with-adaptation` install.
`reference-only`, `deferred`, and `excluded` are recorded but never install and
never activate. `ecc-harness-optimizer` is `deferred` because no skill directory
exists at the pinned revision; `ecc-eval-harness` is `supported-without-changes`
and preserves upstream's candidate-execution refusal text, so the refusal stays
intact until an independently qualified integration supersedes it.

## Overlays

Overlays are reviewed JSON edits applied after the metadata transform. Each pins
the upstream sha256, the resulting adapted sha256, and a list of `find`/`replace`
edits whose `find` block must match exactly once. Build refuses on any drift: a
changed upstream file, a `find` block that no longer matches once, or a resulting
hash that differs from the pinned value.

## Checking upstream for pack changes

The check is manual; no schedule runs it. It reads local files and writes nothing.

```bash
git clone --filter=blob:none https://github.com/affaan-m/ECC.git /tmp/ecc-candidate
git -C /tmp/ecc-candidate checkout --detach <ref>
bun session-system/ecc/adapter/upstream-check.ts --candidate-root /tmp/ecc-candidate --commit "$(git -C /tmp/ecc-candidate rev-parse HEAD)"
```

The script prints a one-line JSON report and exits 0 (on bad arguments it prints `{"error": ...}` and exits 1):

- `baseCommit`: manifest `upstream.commit`; `candidateCommit`: the `--commit` value.
- `files`: one row per consumed file (primary file of each installable packed asset, plus declared helper files of `skill-namespaced` assets): `assetId`, `path`, `status` (`unchanged`/`changed`/`missing`), `fromSha256` (mirror bytes), `toSha256` (candidate bytes, null when missing).
- `newFiles`: candidate files in a consumed skill directory that are neither SKILL.md nor a declared dependency. The packs do not consume them.
- `overlayConflicts`: overlay-backed asset ids whose primary file is not `unchanged`. Overlay JSON is not read.
- `changed`: true when any row is not `unchanged` or `newFiles` is not empty.

## Building

```bash
bun session-system/ecc/adapter/emit-lock.ts
```

Writes `session-system/ecc/adapted.lock.json`. Commit it whenever the mirror, a
manifest record, an overlay, or a transform changes.

## Install, update, and remove

The lifecycle is programmatic (`session-system/ecc/adapter/apply.ts`); it plans
the complete file set before writing anything.

- **install** — writes only destinations that are absent. A destination that
  exists and is not owned by this installation aborts the whole install, so a
  user file is never overwritten.
- **update** — `cli.ts update` plans every installable asset of both packs
  against the current mirror and writes those destinations. An owned file
  whose bytes no longer match the lock was modified after install, so update
  refuses instead of clobbering the local edit.
- **remove** — deletes only lock-owned files whose bytes still match the recorded
  sha256, prunes directories it emptied, and deletes the lock. A modified owned
  file is refused.

Ownership is recorded per project in `<projectRoot>/.omp/ecc/adapted.lock.json`,
so two projects never share an ownership record. Every step is idempotent:
re-installing writes nothing new, and removing an absent installation is a no-op.

Rollback restores the ECC root to its pre-update bytes and runs `update` again.
That returns the project tree and `<projectRoot>/.omp/ecc/adapted.lock.json` to
the bytes from the first install, and a second update reports unchanged.

## Verification

```bash
bun test session-system/tests/ecc-adaptation.test.ts session-system/tests/ecc-install.test.ts session-system/tests/ecc-upstream-check.test.ts session-system/tests/ecc-repin.test.ts session-system/tests/ecc-manifest-pin.test.ts session-system/tests/ecc-rollback.test.ts
```

The adaptation test verifies source and transformed hashes, manifest and
disposition invariants, reference closure (including a failure on a missing
reference), overlay drift refusal, the preserved candidate-execution refusal, and
that an adapted rule, skill, and advisor are discovered natively. The install
test verifies deterministic install/update/remove, unowned-overwrite refusal,
per-project ownership, and that nothing activates accidentally. The upstream-check
test verifies an identical copy, a changed primary file, a missing helper, an
added skill file, an overlay conflict that clears when the candidate primary
bytes equal the mirror bytes, and one JSON object from the CLI. The repin test
verifies that an accepted review re-pins the mirror, the manifest, the review
file, and the lock, and that a refusal leaves the ECC root unchanged. The
manifest-pin test verifies the committed `manifestSha256`, review-pin refusal,
`UpdateReview` rejection of unknown keys, and `emit-lock.ts --check` exit codes.
The rollback test verifies that restoring the pre-update ECC root and running
update returns the project tree and its lock to the first install, and that a
second update reports unchanged.
