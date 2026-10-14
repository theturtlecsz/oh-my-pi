# ECC Database Reviewer advisor — retirement decision

**Decision:** Retire the current ECC database advisor asset from the ECC adapter's
install set. Owner decision, Chris, 2026-10-13 (programme decision 4).

## Basis

The OMP-525 qualification trial (`docs/report/ecc-database-advisor-trial/`) ran the
real `ECC Database Reviewer` roster entry over six case commits and tallied its
findings. Every trial receipt (`cases.json`, `labels/`, `runs/`, `SHA256SUMS`,
`summary.json`, and the historical `README.md`) is retained byte-for-byte; this file
is the only addition to the directory.

Trial result (from `summary.json`, written by `scripts/ecc-advisor-trial/tally.ts --write`):

- 6 cases, 2 known-bad.
- useful findings: 2.
- false alarms: 23.
- known defects caught: 0.
- keep rule: keep iff `useful >= false_alarm` and `known_defects_caught >= 1`, else drop.
- recommendation: **drop**.

The keep rule fails on both clauses: `useful` 2 < `false_alarm` 23, and
`known_defects_caught` 0 < 1. The two useful findings were the o3 workspace-tree
schema concern (`scope_class`/`intake_hold` added to tree items while the typed
client validates `WorkItemView` without them) and the o4 hash-chain race concern
(the previous event hash is read before the advisory lock). Neither known-bad case
was caught: k1 (E0187, concurrent same-id `run_operation` calls both run) and k2
(E0189, racy `MAX(step_index)+1` insert) both went unnoticed. With 23 false alarms
against 2 useful findings, and no known defect caught, the advisor's signal cost
more review effort than it returned.

## Scope of this decision

- This retires only the current ECC database advisor asset from the ECC adapter's
  install set.
- It does not close the broader goal. Programme obligation O4 (Complete ECC
  foundation) and ECC WP6 (Add one adapted advisor specialization) remain **open** —
  neither met nor dropped.
- Any replacement advisor requires a new Work Ledger proposal with its own
  evaluation plan. This decision builds no replacement and carries no inherited
  authority.
- The ECC installation hold and the OMP-611 installer/ownership work are separate
  and unchanged.
