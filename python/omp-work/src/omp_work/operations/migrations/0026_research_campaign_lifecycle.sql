-- Research campaign lifecycle, action vocabulary, and policy-fingerprint compatibility (R02-S2).
ALTER TABLE omp_research.campaigns DROP CONSTRAINT campaigns_state_check;
ALTER TABLE omp_research.campaigns ADD CONSTRAINT campaigns_state_check
  CHECK (state IN ('draft','admitted','running','paused','evaluating','blocked','concluded','cancelled'));
ALTER TABLE omp_research.campaigns
  ADD COLUMN outcome text CHECK (outcome IS NULL OR outcome IN ('supported','refuted','inconclusive','resource_exhausted','externally_blocked')),
  ADD COLUMN outcome_reason text CHECK (outcome_reason IS NULL OR octet_length(outcome_reason) <= 4096),
  ADD COLUMN concluded_at timestamptz,
  ADD COLUMN blocked_dependency jsonb CHECK (blocked_dependency IS NULL OR octet_length(blocked_dependency::text) <= 8192),
  ADD COLUMN blocked_from_state text CHECK (blocked_from_state IS NULL OR blocked_from_state IN ('admitted','running','paused','evaluating')),
  ADD CONSTRAINT campaigns_post_admission_policy CHECK (state IN ('draft','cancelled') OR policy_sha256 IS NOT NULL),
  ADD CONSTRAINT campaigns_post_admission_admitted_at CHECK (state IN ('draft','cancelled') OR admitted_at IS NOT NULL),
  ADD CONSTRAINT campaigns_admitted_provenance CHECK (
    (admitted_at IS NULL AND policy_sha256 IS NULL) OR
    (admitted_at IS NOT NULL AND policy_sha256 IS NOT NULL)
  ),
  ADD CONSTRAINT campaigns_draft_provenance CHECK (
    state <> 'draft' OR (policy_sha256 IS NULL AND admitted_at IS NULL)
  ),
  ADD CONSTRAINT campaigns_concluded_outcome CHECK (
    CASE WHEN state = 'concluded' THEN outcome IS NOT NULL AND outcome_reason IS NOT NULL AND concluded_at IS NOT NULL
    ELSE outcome IS NULL AND outcome_reason IS NULL AND concluded_at IS NULL END
  ),
  ADD CONSTRAINT campaigns_blocked_dependency CHECK (
    CASE WHEN state = 'blocked' THEN blocked_dependency IS NOT NULL AND blocked_from_state IS NOT NULL
    ELSE blocked_dependency IS NULL AND blocked_from_state IS NULL END
  );
ALTER TABLE omp_research.trials
  ADD COLUMN action text CHECK (action IS NULL OR action IN ('retrieve','draft','repair','refine','challenge','combine','evaluate','replicate','deepen','prune','synthesize','escalate','conclude')),
  ADD COLUMN reason text CHECK (reason IS NULL OR octet_length(reason) <= 2048);

ALTER TABLE omp_research.campaigns NO FORCE ROW LEVEL SECURITY;

CREATE OR REPLACE FUNCTION omp_control.enforce_research_campaign_transition() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'research campaigns are immutable history';
  END IF;

  IF OLD.campaign_id IS DISTINCT FROM NEW.campaign_id
     OR OLD.workspace_id IS DISTINCT FROM NEW.workspace_id
     OR OLD.work_id IS DISTINCT FROM NEW.work_id
     OR OLD.revision_id IS DISTINCT FROM NEW.revision_id
     OR OLD.domain IS DISTINCT FROM NEW.domain
     OR OLD.spec IS DISTINCT FROM NEW.spec
     OR OLD.spec_sha256 IS DISTINCT FROM NEW.spec_sha256
     OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
    RAISE EXCEPTION 'campaign identity and specification are immutable';
  END IF;

  IF OLD.state IN ('concluded', 'cancelled') AND NEW.state <> OLD.state THEN
    RAISE EXCEPTION 'terminal campaign state is immutable';
  END IF;

  IF OLD.state = 'concluded' AND (
     NEW.outcome IS DISTINCT FROM OLD.outcome
     OR NEW.outcome_reason IS DISTINCT FROM OLD.outcome_reason
     OR NEW.concluded_at IS DISTINCT FROM OLD.concluded_at
  ) THEN
    RAISE EXCEPTION 'concluded campaign outcome is immutable';
  END IF;

  IF OLD.state = 'cancelled' AND (
     NEW.cancelled_at IS DISTINCT FROM OLD.cancelled_at
     OR NEW.cancel_reason IS DISTINCT FROM OLD.cancel_reason
  ) THEN
    RAISE EXCEPTION 'cancelled campaign metadata is immutable';
  END IF;

  IF OLD.admitted_at IS NOT NULL AND NEW.admitted_at IS DISTINCT FROM OLD.admitted_at THEN
    RAISE EXCEPTION 'campaign admitted_at is immutable once set';
  END IF;

  IF OLD.policy_sha256 IS NOT NULL AND NEW.policy_sha256 IS DISTINCT FROM OLD.policy_sha256 THEN
    RAISE EXCEPTION 'campaign policy_sha256 is immutable once set';
  END IF;

  IF OLD.admitted_at IS NULL AND NEW.state IN ('draft', 'cancelled') AND (
     NEW.policy_sha256 IS NOT NULL OR NEW.admitted_at IS NOT NULL
  ) THEN
    RAISE EXCEPTION 'unadmitted campaign cannot set admission provenance';
  END IF;

  IF OLD.state <> NEW.state THEN
    IF NOT (
      (OLD.state = 'draft' AND NEW.state IN ('admitted', 'cancelled'))
      OR (OLD.state = 'admitted' AND NEW.state IN ('running', 'blocked', 'cancelled'))
      OR (OLD.state = 'running' AND NEW.state IN ('paused', 'evaluating', 'blocked', 'cancelled'))
      OR (OLD.state = 'paused' AND NEW.state IN ('running', 'blocked', 'cancelled'))
      OR (OLD.state = 'evaluating' AND NEW.state IN ('running', 'blocked', 'concluded', 'cancelled'))
      OR (OLD.state = 'blocked' AND NEW.state IN ('admitted', 'running', 'paused', 'evaluating', 'concluded', 'cancelled'))
    ) THEN
      RAISE EXCEPTION 'illegal campaign transition % -> %', OLD.state, NEW.state;
    END IF;

    IF OLD.state = 'blocked' AND NEW.state NOT IN ('concluded', 'cancelled') AND NEW.state <> OLD.blocked_from_state THEN
      RAISE EXCEPTION 'blocked campaign can only resume to %', OLD.blocked_from_state;
    END IF;

    IF OLD.state = 'blocked' AND NEW.state = 'concluded' AND NEW.outcome NOT IN ('resource_exhausted', 'externally_blocked') THEN
      RAISE EXCEPTION 'blocked campaign cannot conclude with outcome %', NEW.outcome;
    END IF;
  END IF;

  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS enforce_research_campaign_transition_trigger ON omp_research.campaigns;
CREATE TRIGGER enforce_research_campaign_transition_trigger
  BEFORE UPDATE OR DELETE ON omp_research.campaigns
  FOR EACH ROW EXECUTE FUNCTION omp_control.enforce_research_campaign_transition();

ALTER TABLE omp_research.campaigns FORCE ROW LEVEL SECURITY;
