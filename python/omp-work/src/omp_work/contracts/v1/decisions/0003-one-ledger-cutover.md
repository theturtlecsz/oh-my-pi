# 0003 — One ledger cutover

## Decision

One active writer epoch. The one-hour cutover order is freeze, drain, final export, import, parity check, restore proof, atomic PostgreSQL activation, WorkService command smoke, permanent Linear writer disable, and credential revocation. The pre-activation freeze already refuses Linear writes; no dual-write window exists.

Before activation: discard candidate state and remain Linear-backed. After activation: restore PostgreSQL and never re-enable Linear writes. Post-cutover Linear edits are permanently ignored.

Blocking anomalies are pagination/count/hash gaps, duplicate UUID/key mappings, missing endpoints, cycles, multiple focus slots, source/local conflicts, and legacy-authority claims. Only `attachment_content_unavailable` with preserved metadata and `unsupported_non_workflow_object` may be quarantined. A manifest cannot validate with a blocking anomaly or unexplained parity difference.
