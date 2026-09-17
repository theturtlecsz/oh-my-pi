-- Stage budget resource classification and immutable quote binding (OMP-233).
ALTER TABLE omp_work.provider_accounts
  ADD COLUMN budget_resource text CHECK (budget_resource IS NULL OR budget_resource IN ('cash', 'included_credit', 'native_quota', 'local_compute'));

CREATE OR REPLACE FUNCTION omp_work.put_provider_account(
  p_account_id uuid,
  p_workspace_id uuid,
  p_provider text,
  p_account_identity text,
  p_entitlement_evidence text,
  p_evidence_observed_at timestamptz,
  p_billing_mode text,
  p_rate_card_version text,
  p_observed_balance numeric,
  p_balance_provenance text,
  p_reset_at timestamptz,
  p_concurrency_limit integer,
  p_budget_resource text
)
RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
  v_caller_workspace uuid;
  v_caller_actor uuid;
  v_card_modes text[];
  v_card_qualification text;
  row_by_id omp_work.provider_accounts%ROWTYPE;
  row_by_key omp_work.provider_accounts%ROWTYPE;
BEGIN
  -- Validate caller claims
  v_caller_workspace := omp_control.current_workspace_id();
  IF p_workspace_id IS NULL OR v_caller_workspace IS DISTINCT FROM p_workspace_id THEN
    RAISE EXCEPTION 'workspace claim mismatch';
  END IF;

  v_caller_actor := omp_control.current_actor_id();
  IF v_caller_actor IS NULL THEN
    RAISE EXCEPTION 'actor claim required';
  END IF;

  -- Validate inputs
  IF p_account_id IS NULL THEN
    RAISE EXCEPTION 'account_id required';
  END IF;
  IF p_provider IS NULL OR length(trim(p_provider)) = 0 THEN
    RAISE EXCEPTION 'provider required';
  END IF;
  IF p_account_identity IS NULL OR length(trim(p_account_identity)) = 0 THEN
    RAISE EXCEPTION 'account_identity required';
  END IF;
  IF p_entitlement_evidence IS NULL OR length(trim(p_entitlement_evidence)) = 0 THEN
    RAISE EXCEPTION 'entitlement_evidence required';
  END IF;
  IF p_evidence_observed_at IS NULL THEN
    RAISE EXCEPTION 'evidence_observed_at required';
  END IF;
  IF p_billing_mode NOT IN ('subscription', 'metered', 'purchased_credit', 'local') THEN
    RAISE EXCEPTION 'invalid billing_mode';
  END IF;
  IF p_balance_provenance NOT IN ('provider_observed', 'locally_estimated', 'unknown') THEN
    RAISE EXCEPTION 'invalid balance_provenance';
  END IF;
  IF p_concurrency_limit IS NULL OR p_concurrency_limit <= 0 THEN
    RAISE EXCEPTION 'concurrency_limit must be positive';
  END IF;
  IF p_observed_balance IS NOT NULL AND p_observed_balance < 0 THEN
    RAISE EXCEPTION 'observed_balance cannot be negative';
  END IF;
  IF p_balance_provenance = 'unknown' AND p_observed_balance IS NOT NULL THEN
    RAISE EXCEPTION 'observed_balance must be null when balance_provenance is unknown';
  END IF;
  IF p_reset_at IS NOT NULL AND p_reset_at <= p_evidence_observed_at THEN
    RAISE EXCEPTION 'reset_at must be in the future relative to evidence_observed_at';
  END IF;
  IF p_rate_card_version IS NOT NULL AND length(trim(p_rate_card_version)) = 0 THEN
    RAISE EXCEPTION 'rate_card_version cannot be empty string';
  END IF;
  IF p_budget_resource IS NOT NULL AND p_budget_resource NOT IN ('cash', 'included_credit', 'native_quota', 'local_compute') THEN
    RAISE EXCEPTION 'invalid budget_resource';
  END IF;

  -- Validate rate card reference when non-null
  IF p_rate_card_version IS NOT NULL THEN
    SELECT billing_modes, qualification INTO v_card_modes, v_card_qualification
    FROM omp_work.rate_cards
    WHERE workspace_id = p_workspace_id AND provider = p_provider AND version = p_rate_card_version;
    IF NOT FOUND THEN RETURN 'rate_card_missing'; END IF;
    IF NOT (p_billing_mode = ANY (v_card_modes)) THEN RETURN 'rate_card_incompatible'; END IF;
    IF v_card_qualification <> 'qualified' THEN RETURN 'rate_card_unqualified'; END IF;
  END IF;

  -- Advisory lock to serialize operations on this account and this provider/identity key
  PERFORM pg_advisory_xact_lock(hashtextextended(p_account_id::text, 0));
  PERFORM pg_advisory_xact_lock(hashtextextended(p_workspace_id::text || ':' || p_provider || ':' || p_account_identity, 0));

  -- Lock existing rows if any
  SELECT * INTO row_by_id
  FROM omp_work.provider_accounts
  WHERE account_id = p_account_id
  FOR UPDATE;

  SELECT * INTO row_by_key
  FROM omp_work.provider_accounts
  WHERE workspace_id = p_workspace_id AND provider = p_provider AND account_identity = p_account_identity
  FOR UPDATE;

  -- Verify identity stability and boundaries
  IF row_by_id.account_id IS NOT NULL AND row_by_id.workspace_id <> p_workspace_id THEN
    RETURN 'conflict';
  END IF;
  IF row_by_id.account_id IS NOT NULL AND (row_by_id.provider <> p_provider OR row_by_id.account_identity <> p_account_identity) THEN
    RETURN 'conflict';
  END IF;
  IF row_by_key.account_id IS NOT NULL AND row_by_key.account_id <> p_account_id THEN
    RETURN 'conflict';
  END IF;

  -- New account insertion
  IF row_by_id.account_id IS NULL AND row_by_key.account_id IS NULL THEN
    BEGIN
      INSERT INTO omp_work.provider_accounts (
        account_id,
        workspace_id,
        provider,
        account_identity,
        entitlement_evidence,
        evidence_observed_at,
        billing_mode,
        rate_card_version,
        observed_balance,
        balance_provenance,
        reset_at,
        concurrency_limit,
        budget_resource
      ) VALUES (
        p_account_id,
        p_workspace_id,
        p_provider,
        p_account_identity,
        p_entitlement_evidence,
        p_evidence_observed_at,
        p_billing_mode,
        p_rate_card_version,
        p_observed_balance,
        p_balance_provenance,
        p_reset_at,
        p_concurrency_limit,
        p_budget_resource
      );
      RETURN 'inserted';
    EXCEPTION WHEN unique_violation THEN
      RETURN 'conflict';
    END;
  END IF;

  -- Existing row update / evidence monotonicity check
  IF p_evidence_observed_at < row_by_id.evidence_observed_at THEN
    RETURN 'stale_evidence';
  END IF;

  IF p_evidence_observed_at = row_by_id.evidence_observed_at THEN
    IF row_by_id.entitlement_evidence = p_entitlement_evidence
       AND row_by_id.billing_mode = p_billing_mode
       AND row_by_id.rate_card_version IS NOT DISTINCT FROM p_rate_card_version
       AND row_by_id.observed_balance IS NOT DISTINCT FROM p_observed_balance
       AND row_by_id.balance_provenance = p_balance_provenance
       AND row_by_id.reset_at IS NOT DISTINCT FROM p_reset_at
       AND row_by_id.concurrency_limit = p_concurrency_limit
       AND row_by_id.budget_resource IS NOT DISTINCT FROM p_budget_resource THEN
      RETURN 'unchanged';
    ELSE
      RETURN 'stale_evidence';
    END IF;
  END IF;

  -- Newer evidence: revise mutable fields
  UPDATE omp_work.provider_accounts SET
    entitlement_evidence = p_entitlement_evidence,
    evidence_observed_at = p_evidence_observed_at,
    billing_mode = p_billing_mode,
    rate_card_version = p_rate_card_version,
    observed_balance = p_observed_balance,
    balance_provenance = p_balance_provenance,
    reset_at = p_reset_at,
    concurrency_limit = p_concurrency_limit,
    budget_resource = p_budget_resource
  WHERE account_id = p_account_id;

  RETURN 'updated';
END $$;

DROP FUNCTION IF EXISTS omp_work.put_provider_account(
  uuid, uuid, text, text, text, timestamptz, text, text, numeric, text, timestamptz, integer
);

ALTER FUNCTION omp_work.put_provider_account(
  uuid, uuid, text, text, text, timestamptz, text, text, numeric, text, timestamptz, integer, text
) OWNER TO omp_work_owner;

REVOKE ALL ON FUNCTION omp_work.put_provider_account(
  uuid, uuid, text, text, text, timestamptz, text, text, numeric, text, timestamptz, integer, text
) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION omp_work.put_provider_account(
  uuid, uuid, text, text, text, timestamptz, text, text, numeric, text, timestamptz, integer, text
) TO omp_work_app;

ALTER TABLE omp_work.budget_quotes NO FORCE ROW LEVEL SECURITY;
ALTER TABLE omp_work.budget_quotes
  ADD COLUMN resource text CHECK (resource IS NULL OR resource IN ('cash', 'included_credit', 'native_quota', 'local_compute')),
  ADD COLUMN scope_id uuid REFERENCES omp_work.budget_scopes(scope_id);
ALTER TABLE omp_work.budget_quotes FORCE ROW LEVEL SECURITY;
