# 0021 — Typed audit result, evidence manifest, and close validation

## Problem

1. The close audit reporter historically emitted unstructured plain text. The control plane parsed this text using regex heuristics to extract the verdict line and five section headers, but could not enforce schema integrity, typed findings with severities and line locations, or verified criterion checks.
2. Crosswalk gaps ECC WP4 (OMP-329) and FLEET-7 (OMP-326) require a machine-verifiable audit result format binding structured findings, checked criteria, and cryptographic evidence references in the contract.
3. Autoresearch M1 R06 (OMP-315) requires the same receipt shape for research evaluation and verification receipts without coupling to specific work item archetypes.
4. Without contract-level validation, invalid or contradictory audit output (such as PASS without evidence or with unsatisfied criteria, or NEEDS_FIX without findings) could not be deterministically rejected before minting an audit evidence receipt.

## Decision

1. **Contract Models.** `v1/models.py` defines subject-agnostic, strict models published in `schema.json`:
   - `AuditFindingLocation`: `path`, `line_start` (>= 1), and optional `line_end` (>= `line_start` or None).
   - `AuditFinding`: `severity` (pattern `^[A-Z][A-Z0-9]*$`), `criterion_id`, `location`, `evidence`, `impact`, and `minimal_fix`.
   - `AuditCriterionCheck`: `criterion_id`, `status` (`met`, `not_met`, `unverifiable`), and `evidence`.
   - `EvidenceReference`: `kind` (`command`, `file`, `receipt`, `artifact`), `ref`, optional `result` string, and optional `sha256` hex64 digest.
   - `EvidenceManifest`: `references` tuple of `EvidenceReference`.
   - `AuditResult`: `schema_version` (1), `verdict` (`PASS`, `NEEDS_FIX`, `BLOCKED`), `findings`, `criteria`, `evidence` (`EvidenceManifest`), `out_of_scope`, and `remaining_questions`. Every free-text field is single-line (pattern `^[^\r\n]+$`, max 2000 characters).
   - Validation rules: `criterion_id` values in `criteria` must be unique. A `PASS` verdict requires at least one criterion, all criteria `met`, and at least one evidence reference. A `NEEDS_FIX` verdict requires at least one finding.
2. **Transport.** The close auditor agent emits typed audit output via runner yield data payload `{"audit_result": {...}}`.
3. **Control Plane Validation and Refusal.** The control plane validates the typed result during `settle_auditor_launch`. If an `audit_result` payload is present but fails validation against the contract schema, the settlement is refused with reason `audit_result_invalid`. An invalid launch mints no audit receipt and prevents close completion.
4. **Legacy Compatibility.** Stored receipts and existing plain-text reports remain accepted so historical audit trails and legacy tests continue to pass without disruption.
5. **Autoresearch M1 R06 Reuse.** The `AuditResult` and `EvidenceManifest` models are subject-agnostic and serve as the standard receipt shape for Autoresearch M1 R06 evaluations.

## Consequences

- `v1/models.py` defines and exports `AuditFindingLocation`, `AuditFinding`, `AuditCriterionCheck`, `EvidenceReference`, `EvidenceManifest`, and `AuditResult`.
- `Approval.issue` admits `"OMP-520"`.
- `manifest.json` and `schema.json` include the new decision and generated model schemas.
- Downstream slices implement the canonical summary renderer and server-side settlement validation.
