# ECC packs: pinned mirror and OMP adapter

This directory holds a pinned mirror of upstream [ECC](https://github.com/affaan-m/ECC.git)
and the deterministic adapter that turns a small selected subset of it into two
installable packs for this repository. Nothing here fetches the network or needs
a git submodule: the pinned upstream bytes are vendored under `mirror/`, so every
invariant below is verifiable offline with `bun test`.

## Layout

| Path                       | Purpose                                                                                                                                  |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `mirror/`                  | The full upstream tree at the pinned revision (3716 files), inert: never installed or activated by mirroring. Its git tree id equals `upstream.tree`; the upstream sha256 pins read from here. |
| `manifest.json`            | Upstream pin, the two packs, every asset record (target, adaptation kind, disposition, dependencies), reserved native command names, and disposition rules. |
| `catalog.json`             | Every mirrored file, sorted by path, with sha256, classification, reason, and asset owner (or null). Rebuilt by `adapter/emit-catalog.ts`; `--check` names stale or unclassified files. |
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

- **`ecc-domain-rust`** — both coding-style rules, Rust hooks, patterns, security,
  testing, skills, and reviewer.
- **`ecc-domain-typescript`** — TypeScript rules plus coding-standards,
  hexagonal-architecture, architecture-decision-records, and api-design skills.
- **`ecc-domain-python`** — Python rules, patterns/testing skills, and reviewer.
- **`ecc-domain-postgresql`** — postgres-patterns and database-migrations skills
  plus the existing database-reviewer advisor.
- **`ecc-domain-security`** — common, Rust, TypeScript, and Python security rules,
  security reviewer, and security-scan skill.
- **`ecc-maintenance`** — benchmark-optimization-loop plus the agent-architecture-audit,
  loop-design-check, skill-stocktake, rules-distill, and context-budget skills.

New domain members are inactive and deferred pending WP3 adaptation. The
maintenance members are inactive and deferred pending WP3 adaptation of their
helper paths and result locations. Domain packs reuse existing adapted assets:
Rust installs both coding-style rules, PostgreSQL installs the disabled database
advisor, and the other domain packs install no content until their deferred
members are adapted. The maintenance pack installs only the already-adapted
benchmark-optimization-loop skill: its other five members are deferred and
install nothing.

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

Every mirror file is classified by its asset owner or the longest matching
manifest disposition-rule prefix. Rules name each top-level family explicitly;
there is no empty-prefix catch-all. Unclassified files and competing asset
owners abort catalog generation. Hooks are deferred to WP7B, continuous-learning
to WP7A, and the selected workflow skill families to WP3.

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

## Reviewing and applying a pack update

### Hand steps before the review

1. New files. For each `newFiles` path the packs must use: add it (skill-relative) to the asset's manifest `dependencies`, copy the candidate file to `mirror/<path>`, run `bun session-system/ecc/adapter/emit-lock.ts`, then run the check again. The file is then an `unchanged` row. An unused new file stays in `newFiles` and the review; `repin.ts` compares it, never copies it, and moves the pin.
2. Overlay conflict (`repin.ts` refuses). Update the overlay hashes by hand first, for each id in `overlayConflicts`: copy its candidate primary file (row `path`) to `mirror/`; set the asset's manifest `upstream.sha256` and `upstreamSha256` in `overlays/<id>.json` to the row `toSha256`; fix each `find` block that no longer matches; run `bun session-system/ecc/adapter/emit-lock.ts`; on `adapted sha256 mismatch (expected X, got Y)` set `adaptedSha256` to Y and run it again.
3. Run the check again: `overlayConflicts` is empty and the other `changed` rows remain. Write the review from this new report; `repin.ts` applies those rows and moves the pin. If the new report has `changed: false`, `repin.ts` refuses (`nothing to re-pin`): `git restore session-system/ecc`, delete the review, stop.
4. Commit changed `overlays/<id>.json` with `mirror/`, `manifest.json`, `adapted.lock.json`, and the review.

1. Write an `UpdateReview` to `reviews/<candidate commit>.json` relative to the ECC root.
   Set `schemaVersion` to `1`.
   Use `baseCommit` from the report and `candidate.commit` from `candidateCommit`.
   Read the candidate tree and description from its checkout:

   ```bash
   git -C /tmp/ecc-candidate rev-parse 'HEAD^{tree}'
   git -C /tmp/ecc-candidate describe --tags
   ```

   Use the version tag without its `v` prefix for `candidate.version`.
   Copy only report rows with `status: "changed"` into `changes`.
   Keep only `assetId`, `path`, `fromSha256`, and `toSha256` in each change row.
   Do not include `status`.
   Copy the report's `newFiles` list into `newFiles`.
   Supply the reviewer, an ISO date-time for `reviewedAt`, the decision `"accept"`, and review notes.

   This JSON example shows the required fields. Replace the example values with the candidate data and report values.

   ```json
   {
     "schemaVersion": 1,
     "baseCommit": "8321021c54d670126ce3b2969d5deb880b4b0c2a",
     "candidate": {
       "commit": "1111111111111111111111111111111111111111",
       "tree": "2222222222222222222222222222222222222222",
       "version": "2.3.0",
       "describe": "v2.3.0"
     },
     "changes": [
       {
         "assetId": "ecc-search-first",
         "path": "skills/search-first/SKILL.md",
         "fromSha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
         "toSha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
       }
     ],
     "newFiles": [],
     "reviewer": "Chris",
     "reviewedAt": "2026-10-02T12:00:00Z",
     "decision": "accept",
     "notes": "The changed skill retains the required behavior."
   }
   ```

2. Apply the review from the repository root:

   ```bash
   bun session-system/ecc/adapter/repin.ts --candidate-root /tmp/ecc-candidate --review session-system/ecc/reviews/<candidate commit>.json
   ```

   The command returns a JSON `error`, exits 1, and writes nothing for these conditions:

   - The review is invalid, including unknown keys.
   - The candidate has no changes (`nothing to re-pin`).
   - The report contains a `missing` row or an overlay conflict.
   - The decision is not `"accept"`.
   - The `baseCommit` differs from the manifest's pinned `upstream.commit`.
   - The review's `changes` or `newFiles` differs from the report. List order does not affect the comparison.

   On success, the command copies every `changed` row, including helper files, into `mirror/`. It does not copy `newFiles`.
   It sets the manifest's `upstream` commit, tree, version, and describe fields from the review.
   It sets `upstream.commit` on every installable asset and `upstream.sha256` on each changed primary file's asset.
   It copies the review into `reviews/` and rebuilds the lock with the review pin.

3. Check the lock:

   ```bash
   bun session-system/ecc/adapter/emit-lock.ts --check
   ```

   The pinned manifest hash is `manifestSha256` in `adapted.lock.json`.
   This check compares that hash with the `manifest.json` bytes and compares the lock and manifest `upstream.commit` values.
   When a review pin exists, it checks the review's hash, validity, accepted decision, and candidate commit against the lock.
   It does not check `mirror/` bytes.

4. Run the ECC tests to check the mirror and adapter behavior:

   ```bash
   bun test session-system/tests/ecc-*.test.ts
   ```

5. Commit `mirror/`, `manifest.json`, `adapted.lock.json`, the review, and any changed `overlays/<id>.json` together.

## Rolling back a pack update

1. Revert the update commit on the normal merge path:

   ```bash
   git revert <update commit>
   ```

   The revert restores every file the update changed and deletes the review file.

2. Flood `deploy-omp.sh` moves the live copy.

3. For each project, from the repository root:

   ```bash
   bun session-system/ecc/adapter/cli.ts update --project <dir>
   ```

   No project lock: the command exits 1 and the JSON `error` is `no adapted.lock.json to update; run install first`.

   The command writes every installable asset of both packs where the bytes differ or are absent. A one-pack project gets both. A primary file is written as transformed bytes (lock `adaptedSha256`). A `skill-namespaced` helper is written as mirror bytes (lock `files[].sha256`).

   An unowned file with other bytes is refused before any write. An owned file unlike the project lock errors at that file. Earlier files may be written. The project lock is not written.

   On success, the project lock has the restored `upstream.commit`.

4. Confirm the repository lock:

   ```bash
   bun session-system/ecc/adapter/emit-lock.ts --check
   ```

   The check compares the repository lock with the `manifest.json` bytes, `upstream.commit`, and the pinned review. It does not check `mirror/` or a project.

The drill in `session-system/tests/ecc-rollback.test.ts` re-pins, updates, reverts, and updates through `apply.ts` and `cli.ts`, restores the first install's tree and lock byte for byte, and reports one more update as `unchanged`.

## Building

```bash
bun session-system/ecc/adapter/emit-catalog.ts
bun session-system/ecc/adapter/emit-catalog.ts --check
bun session-system/ecc/adapter/emit-lock.ts
```

Commit `catalog.json` whenever mirror bytes or manifest classifications change.
Writes `session-system/ecc/adapted.lock.json`. Commit it whenever the mirror, a
manifest record, an overlay, or a transform changes.

## Install, update, and remove

The lifecycle is programmatic (`session-system/ecc/adapter/apply.ts`); it plans
the complete file set before writing anything.

- **install** — writes only destinations that are absent. A destination that
  exists and is not owned by this installation aborts the whole install, so a
  user file is never overwritten.
- **update** — `cli.ts update --project <dir>` exits 1 when the project has no
  lock. The JSON `error` is `no adapted.lock.json to update; run install first`.
  It writes every installable asset of both packs where the bytes differ or are
  absent. A one-pack project gets both. A primary file is written as transformed
  bytes (lock `adaptedSha256`). A `skill-namespaced` helper is written as mirror
  bytes (lock `files[].sha256`). An unowned file with other bytes is refused
  before any write. An owned file unlike the project lock errors at that file.
  Earlier files may be written, and the project lock is not. On success the
  project lock has the restored `upstream.commit`.
- **remove** — deletes only lock-owned files whose bytes still match the recorded
  sha256, prunes directories it emptied, and deletes the lock. A modified owned
  file is refused.

Ownership is recorded per project in `<projectRoot>/.omp/ecc/adapted.lock.json`,
so two projects never share an ownership record. Every step is idempotent:
re-installing writes nothing new, and removing an absent installation is a no-op.

`remove` uninstalls the packs. A pack update rolls back per [Rolling back a pack update](#rolling-back-a-pack-update).

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
The rollback test re-pins, updates, reverts, and updates through `apply.ts` and
`cli.ts`, restores the first install's tree and lock byte for byte, and reports
one more update as `unchanged`.
