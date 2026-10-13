# Planner independence

Find disjoint files and outputs before choosing slice boundaries or dependencies.
Where the actual scope permits independent work, prefer at least two roots with
`depends_on: []`. Do not pad the plan, invent work, or split a cohesive change to
meet a root count. There is no universal root quota; genuinely serial work stays
serial.

For file overlap, only shared non-union edits require ordering. Paths mentioned
only under "Do not touch", "# Assumptions", or "# Deferred" are not edits and do
not create dependencies. Shared `docs/upstream-fork-inventory.tsv` bookkeeping
is union-merged: each slice updates its own rows, so it does not create an edge.

Every dependency must order shared non-union edits or identify the producer's
path or artifact consumed by the dependent slice. Name that shared edit or
consumed output in the slice body. Topic similarity, slice numbering, a preferred
reading order, and general caution are not dependency reasons. Reject a two-slice
chain supported only by order when the slices can run independently.

Preserve final changelog aggregation: only the final aggregation slice touches a
changelog, after the contributing slices. Preserve owner-result dependencies and
contract approval rules above: only consumers of an owner result depend on it;
a contract approval depends only on the implementation slice changing the
contract. Independence does not remove a real producer/consumer or approval edge.

Plan reviewer: inspect the checkout's code and proposed edit/output boundaries
for unnecessary edges and missed independence. Fail an order-only two-slice
chain; explain which work can run independently or which named shared edit or
consumed output requires serialization. Do not demand multiple roots when scope
is genuinely serial, or fail omissions explicitly listed under Deferred unless
an assumption contradicts the item. Apply these rules on initial plans and
continuations alike.
