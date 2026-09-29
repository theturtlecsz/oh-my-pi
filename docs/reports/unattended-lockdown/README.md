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

## Sections

- [Runtime Defaults](./runtime-defaults.md) — Analyzes defaults for approval modes, subagent execution modes, project trust boundaries, extension discovery, and local service bindings.
- [Robomp](./robomp.md) — Analyzes lockdown restrictions, execution boundaries, and credential exposure for autonomous pull request reviews and unattended agent execution in robomp.
- [Child Budgets](./child-budgets.md) — Analyzes runtime budget controls for subagents spawned by the task executor, and the gaps that leave a child's spend unbounded.
- [Flood](./flood.md) — Analyzes flood's job user, spend limits, CLI permission flags, and reviewer write access.

## All locks

Status is derived from each section row: `applied: <ref>` when the row carries an `(applied: <ref>)` marker (L-RT-05), `decided: OMP-<n>` when its `Lock` cell ends `(decided: D<n>, OMP-<n>)`, and `open` otherwise.

| ID | Section | Upstream divergence | Status |
| --- | --- | --- | --- |
| L-RT-01 | [Runtime Defaults](./runtime-defaults.md) | adds | open |
| L-RT-02 | [Runtime Defaults](./runtime-defaults.md) | adds | open |
| L-RT-03 | [Runtime Defaults](./runtime-defaults.md) | adds | open |
| L-RT-04 | [Runtime Defaults](./runtime-defaults.md) | adds | open |
| L-RT-05 | [Runtime Defaults](./runtime-defaults.md) | modified | applied: OMP-396-s02 |
| L-RB-01 | [Robomp](./robomp.md) | new | open |
| L-RB-02 | [Robomp](./robomp.md) | new | open |
| L-RB-03 | [Robomp](./robomp.md) | none | open |
| L-CB-01 | [Child Budgets](./child-budgets.md) | adds | decided: OMP-404 |
| L-CB-02 | [Child Budgets](./child-budgets.md) | adds | decided: OMP-404 |
| L-CB-03 | [Child Budgets](./child-budgets.md) | adds | open |
| L-CB-04 | [Child Budgets](./child-budgets.md) | adds | decided: OMP-404 |
| L-FL-01 | [Flood](./flood.md) | none | decided: OMP-402 |
| L-FL-02 | [Flood](./flood.md) | none | decided: OMP-404 |
| L-FL-03 | [Flood](./flood.md) | none | open |
| L-FL-04 | [Flood](./flood.md) | none | open |

Tallies: 6 applied/decided — L-RT-05 applied (OMP-396-s02); decided OMP-404 (L-CB-01, L-CB-02, L-CB-04, L-FL-02) and OMP-402 (L-FL-01). 10 open — L-RT-01..04, L-RB-01..03, L-CB-03, L-FL-03, L-FL-04.

## Ties to Q4

Every lock here traces its upstream divergence to [the fork inventory](../../upstream-fork-inventory.tsv) and its maintenance procedure to [the guardrail](../../upstream-guardrail.md).

- L-RT-05 is already applied in `OMP-396-s02`.
- D15 (OMP-402) decides L-FL-01.
- D17 (OMP-404) decides L-CB-01, L-CB-02, L-CB-04, and L-FL-02, but not L-CB-03.

## Waiting for Chris

Nothing is applied before OMP-399-s07.

- [ ] L-RT-01
- [ ] L-RT-02
- [ ] L-RT-03
- [ ] L-RT-04
- [ ] L-RB-01
- [ ] L-RB-02
- [ ] L-RB-03
- [ ] L-CB-03
- [ ] L-FL-03
- [ ] L-FL-04
