-- WorkService-owned stage preflight dispatch admission (OMP-233, slice 2).
-- Preflight intents must be admitted to dispatch before provider probe leaves host.

-- 0. Migration RLS window: lift FORCE (owner bypass) for backfill and restore at end.
ALTER TABLE omp_work.stage_preflight_intents NO FORCE ROW LEVEL SECURITY;

-- 1. Add new columns
ALTER TABLE omp_work.stage_preflight_intents
  ADD COLUMN dispatched_at timestamptz,
  ADD COLUMN dispatch_operation_id uuid,
  ADD COLUMN dispatch_owner_id uuid,
  ADD COLUMN cancelled_at timestamptz,
  ADD COLUMN cancelled_by uuid,
  ADD COLUMN cancel_reason text;

-- 2. Drop only the exact 0030 status and composite state CHECK constraints.
--    Preserves role, task_sha256, probe_sha256, ordinal, logical_sha256, group_sha256.
ALTER TABLE omp_work.stage_preflight_intents
  DROP CONSTRAINT stage_preflight_intents_status_check,
  DROP CONSTRAINT stage_preflight_intents_check;

-- 3. Backfill legacy begun rows to dispatched (uncertain effect, cannot cancel or retry).
--    Non-authorizing legacy marker: dispatch_operation_id = transport_attempt_id satisfies
--    exact dispatched invariant (all 3 dispatch fields non-null) while preventing any incoming
--    command operation from matching or authorizing replay.
UPDATE omp_work.stage_preflight_intents
SET
  status = 'dispatched',
  dispatched_at = created_at,
  dispatch_owner_id = host_owner_id,
  dispatch_operation_id = transport_attempt_id
WHERE status = 'begun';

-- 4. Add named status and composite state CHECK constraints
ALTER TABLE omp_work.stage_preflight_intents
  ADD CONSTRAINT stage_preflight_intents_status_check
  CHECK (status IN ('begun', 'dispatched', 'cancelled_undispatched', 'settled'));

ALTER TABLE omp_work.stage_preflight_intents
  ADD CONSTRAINT stage_preflight_intents_state_check
  CHECK (
    (
      status = 'begun'
      AND host_owner_id IS NOT NULL
      AND settled_at IS NULL
      AND dispatched_at IS NULL
      AND dispatch_operation_id IS NULL
      AND dispatch_owner_id IS NULL
      AND cancelled_at IS NULL
      AND cancelled_by IS NULL
      AND cancel_reason IS NULL
    )
    OR
    (
      status = 'dispatched'
      AND host_owner_id IS NOT NULL
      AND dispatched_at IS NOT NULL
      AND dispatch_operation_id IS NOT NULL
      AND dispatch_owner_id IS NOT NULL
      AND settled_at IS NULL
      AND cancelled_at IS NULL
      AND cancelled_by IS NULL
      AND cancel_reason IS NULL
    )
    OR
    (
      status = 'cancelled_undispatched'
      AND host_owner_id IS NOT NULL
      AND cancelled_at IS NOT NULL
      AND cancelled_by IS NOT NULL
      AND cancel_reason IS NOT NULL
      AND dispatched_at IS NULL
      AND dispatch_operation_id IS NULL
      AND dispatch_owner_id IS NULL
      AND settled_at IS NULL
    )
    OR
    (
      status = 'settled'
      AND settled_at IS NOT NULL
      AND cancelled_at IS NULL
      AND cancelled_by IS NULL
      AND cancel_reason IS NULL
      AND (
        (dispatched_at IS NULL AND dispatch_operation_id IS NULL AND dispatch_owner_id IS NULL)
        OR
        (dispatched_at IS NOT NULL AND dispatch_operation_id IS NOT NULL AND dispatch_owner_id IS NOT NULL)
      )
    )
  );

-- 5. Grant UPDATE on transition columns to omp_work_app; DENY DELETE and TRUNCATE
GRANT UPDATE (status, settled_at, dispatched_at, dispatch_operation_id, dispatch_owner_id, cancelled_at, cancelled_by, cancel_reason)
  ON omp_work.stage_preflight_intents TO omp_work_app;

REVOKE DELETE, TRUNCATE ON omp_work.stage_preflight_intents FROM omp_work_app, omp_work_readonly, omp_work_importer;

-- 6. Restore owner-inclusive FORCE ROW LEVEL SECURITY
ALTER TABLE omp_work.stage_preflight_intents FORCE ROW LEVEL SECURITY;
