-- OMP-568: index the per-workspace domain-event read paths.
-- The watermark MAX(sequence), the lateral page scan, subscription replay,
-- and the mission link walk all filter on workspace_id and order by sequence.
-- With only the global unique btree on sequence they walked entries from
-- every workspace. The per-aggregate head read needs aggregate_id before
-- sequence. Both indexes inherit the table's RLS and privileges.

CREATE INDEX domain_events_workspace_sequence_idx
  ON omp_audit.domain_events (workspace_id, sequence);

CREATE INDEX domain_events_workspace_aggregate_sequence_idx
  ON omp_audit.domain_events (workspace_id, aggregate_id, sequence);
