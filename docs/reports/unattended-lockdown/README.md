# Unattended Lockdown Report

Deep-dive report on lockdown restrictions and safety boundaries for unattended execution (OMP-399).

## Scope

This report analyzes locks across four operational surfaces:
- **robomp**: Autonomous agent runtime executing pull request reviews, automated tasks, and evaluation loops on untrusted workspaces and branches.
- **flood**: Multi-worktree build and test automation harness.
- **child agents**: Subagents spawned dynamically by task executors (`task/executor.ts`) to perform delegated work.
- **metaharness**: Local benchmark runner service and HTTP management server.

## Report Structure and Columns

Every lockdown report table follows the standard `LOCK_COLUMNS` specification:
- **ID**: Unique, stable identifier for the proposed lock (e.g., `L-RT-01`).
- **Lock**: Description of the proposed lockdown restriction or safety policy.
- **Location**: Relative markdown link (`../../../`) to the codebase file(s) and the target symbol or entrypoint.
- **Affected runs**: Execution environments, agent roles, or run modes affected by the restriction. Note that flood is excluded from runtime locks because omp is not invoked inside flood itself.
- **Breaks**: Explicit breakdown of backwards incompatibilities, disrupted workflows, or caller expectations broken if the lock is applied.
- **Upstream divergence**: Upstream maintenance classification for the proposed change.

## Upstream Divergence Taxonomy

The divergence classifications characterize the upstream maintenance impact of each proposed lock:
- `none`: No repository changes required (e.g., operational policy or external configuration).
- `fork-only`: Changes touch only files with `fork-only` scope in `docs/upstream-fork-inventory.tsv`.
- `adds`: Modifies an upstream-tracked file that currently has a `shared` inventory row, adding fork-specific logic.
- `new`: Edits an upstream-identical file that currently has no inventory row, establishing a new `shared` inventory row.
- `modified`: The lock has already been implemented and applied; the divergence is already tracked in an existing `shared` inventory row.

## Governance and Application Policy

Every lock documented in this report is a candidate proposal. Following owner decision Q5, **no slice applies a lock** until the owner (Chris) reviews and records explicit choices in `DECISIONS.md`. The only exception is locks that have already shipped in prior tasks (such as `L-RT-05` in `OMP-396-s02`), which are recorded with status `modified` and annotated `(applied: OMP-396-s02)`.

## Section Reports

- [Runtime Defaults](./runtime-defaults.md) — Analyzes defaults for approval modes, subagent execution modes, project trust boundaries, extension discovery, and local service bindings.
