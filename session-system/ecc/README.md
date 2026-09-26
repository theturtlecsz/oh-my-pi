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

## Overlays and re-pinning

Overlays are reviewed JSON edits applied after the metadata transform. Each pins
the upstream sha256, the resulting adapted sha256, and a list of `find`/`replace`
edits whose `find` block must match exactly once. Build refuses on any drift: a
changed upstream file, a `find` block that no longer matches once, or a resulting
hash that differs from the pinned value.

Re-pin ritual when upstream changes:

1. Replace the affected `mirror/` file and update its `upstream.sha256` pin in
   `manifest.json`.
2. Review the overlay edits; update `upstreamSha256` if the source moved.
3. Run the build; it reports the computed adapted sha256 when it rejects a stale pin.
4. Pin the reported adapted sha256 and re-run the build to confirm it passes.
5. Commit the mirror, manifest, overlay, and rebuilt `adapted.lock.json` together.

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
- **update** — re-plans against the current mirror and rewrites files this
  install owns. An owned file whose bytes no longer match the lock was modified
  after install, so update refuses instead of clobbering the local edit.
- **remove** — deletes only lock-owned files whose bytes still match the recorded
  sha256, prunes directories it emptied, and deletes the lock. A modified owned
  file is refused.

Ownership is recorded per project in `<projectRoot>/.omp/ecc/adapted.lock.json`,
so two projects never share an ownership record. Every step is idempotent:
re-installing writes nothing new, and removing an absent installation is a no-op.

Rollback is `remove`: it deletes the owned artifacts for the installed packs and
leaves every other file in the project untouched.

## Verification

```bash
bun test session-system/tests/ecc-adaptation.test.ts session-system/tests/ecc-install.test.ts
```

The adaptation test verifies source and transformed hashes, manifest and
disposition invariants, reference closure (including a failure on a missing
reference), overlay drift refusal, the preserved candidate-execution refusal, and
that an adapted rule, skill, and advisor are discovered natively. The install
test verifies deterministic install/update/remove, unowned-overwrite refusal,
per-project ownership, and that nothing activates accidentally.
