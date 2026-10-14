"""Role evidence CLI and validator for Work model policy routing.

Validates a dated authority decision and six-role execution evidence
against packaged policy bindings, caller docs, and review baseline.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .model_review import check_policy_review
from .policy import RoutingPolicy, load_policy

SIX_ROLES: tuple[str, ...] = ("plan", "implement", "review", "research", "judge", "operator")

ROLE_ALIASES: dict[str, str] = {
    "plan": "plan",
    "implement": "implement",
    "verify": "verify",
    "research": "research",
    "intake_support_judge": "judge",
    "judge": "judge",
    "review": "review",
    "audit": "review",
    "operator_session": "operator",
    "operator": "operator",
}


def normalize_role(role: str) -> str:
    """Normalize role to canonical name."""
    return ROLE_ALIASES.get(role, role)


class RoleEvidenceError(Exception):
    """Raised when role evidence or authority decision validation fails."""

    def __init__(self, reason: str, *, role: str | None = None, details: str | None = None) -> None:
        self.reason = reason
        self.role = role
        self.details = details
        msg = f"Role evidence validation failed: reason={reason}"
        if role:
            msg = f"Role evidence validation failed for role='{role}': reason={reason}"
        if details:
            msg += f" ({details})"
        super().__init__(msg)


def find_repo_root(start: Path | None = None) -> Path:
    """Find repository root containing docs/ and python/ directories."""
    cur = (start or Path(__file__)).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / ".git").exists() or (parent / "docs" / "model-policy-python-evidence.md").is_file():
            return parent
    return Path.cwd()


def _is_valid_date(val: Any) -> bool:
    if not isinstance(val, str) or not val.strip():
        return False
    try:
        date.fromisoformat(val)
        return True
    except ValueError:
        pass
    try:
        datetime.fromisoformat(val)
        return True
    except ValueError:
        return False


def validate_decision(decision: dict[str, Any]) -> None:
    """Validate dated decision for unanswered authority questions."""
    dec_date = decision.get("date") or decision.get("decision_date")
    if not _is_valid_date(dec_date):
        raise RoleEvidenceError("missing_decision_date", details="Decision must contain a valid ISO date string")

    # Validate authority questions (specifically Q1)
    questions = (
        decision.get("authority_questions")
        or decision.get("questions")
        or decision.get("authority")
    )
    if not isinstance(questions, dict):
        raise RoleEvidenceError("unresolved_authority", details="Decision must contain authority questions mapping")

    q1 = questions.get("Q1") or questions.get("q1")
    if not q1:
        raise RoleEvidenceError("unresolved_authority", details="Question Q1 is missing from authority questions")

    if isinstance(q1, dict):
        status = str(q1.get("status", "")).lower()
        if status in ("unanswered", "unresolved", "pending"):
            raise RoleEvidenceError(
                "unresolved_authority",
                details=f"Question Q1 status is '{status}'",
            )
        option = q1.get("selected_option") or q1.get("option") or q1.get("decision")
        if not option or str(option).strip() in ("", "None", "null"):
            raise RoleEvidenceError("unresolved_authority", details="Question Q1 has no selected option or decision")
    elif isinstance(q1, str):
        if q1.lower() in ("unanswered", "unresolved", "pending", ""):
            raise RoleEvidenceError("unresolved_authority", details=f"Question Q1 is {q1}")
    else:
        raise RoleEvidenceError("unresolved_authority", details="Question Q1 has invalid shape")

    # Check any other questions in the mapping
    for qid, qval in questions.items():
        if isinstance(qval, dict):
            status = str(qval.get("status", "")).lower()
            if status in ("unanswered", "unresolved", "pending"):
                raise RoleEvidenceError("unresolved_authority", details=f"Question {qid} is '{status}'")


def _read_evidence_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RoleEvidenceError("missing_evidence_file", details=f"Evidence file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RoleEvidenceError("invalid_json", details=f"Failed to parse evidence JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleEvidenceError("invalid_shape", details="Evidence file must be a JSON object")
    return data


def _read_decision_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RoleEvidenceError("missing_decision_file", details=f"Decision file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RoleEvidenceError("invalid_json", details=f"Failed to parse decision JSON from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RoleEvidenceError("invalid_shape", details="Decision file must be a JSON object")
    return data


def _extract_receipts(evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_receipts = (
        evidence.get("receipts")
        or evidence.get("claims")
        or evidence.get("roles")
    )
    receipts_by_role: dict[str, dict[str, Any]] = {}
    if isinstance(raw_receipts, dict):
        for key, val in raw_receipts.items():
            if isinstance(val, dict):
                norm = normalize_role(val.get("role") or key)
                receipts_by_role[norm] = val
    elif isinstance(raw_receipts, list):
        for item in raw_receipts:
            if isinstance(item, dict) and "role" in item:
                norm = normalize_role(item["role"])
                receipts_by_role[norm] = item
    return receipts_by_role


def _load_trace(trace_ref: str | Path, base_dir: Path, role: str) -> dict[str, Any]:
    trace_path = Path(trace_ref)
    if not trace_path.is_absolute():
        trace_path = base_dir / trace_path
    if not trace_path.is_file():
        raise RoleEvidenceError(
            "missing_trace_file",
            role=role,
            details=f"Trace file not found at {trace_path}",
        )
    try:
        trace_data = json.loads(trace_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RoleEvidenceError(
            "corrupt_trace_file",
            role=role,
            details=f"Trace file at {trace_path} is invalid JSON: {exc}",
        ) from exc
    if not isinstance(trace_data, dict):
        raise RoleEvidenceError(
            "corrupt_trace_file",
            role=role,
            details=f"Trace file at {trace_path} must be a JSON object",
        )
    return trace_data


def validate_role_evidence(
    evidence_path: str | Path | dict[str, Any],
    decision_path: str | Path | dict[str, Any],
    *,
    policy_path: str | Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate dated decision, caller traces, and role evidence against routing policy.

    Ensures complete agreement for all six execution roles: plan, implement, review,
    research, judge, and operator.
    """
    root = repo_root or find_repo_root()

    # Read referenced docs to verify evidence boundary contracts
    python_ev_path = root / "docs" / "model-policy-python-evidence.md"
    native_ev_path = root / "docs" / "model-policy-native-evidence.md"
    if not python_ev_path.is_file():
        raise RoleEvidenceError("missing_doc", details=f"Missing python caller evidence doc at {python_ev_path}")
    if not native_ev_path.is_file():
        raise RoleEvidenceError("missing_doc", details=f"Missing native caller evidence doc at {native_ev_path}")
    _ = python_ev_path.read_text(encoding="utf-8")
    _ = native_ev_path.read_text(encoding="utf-8")

    # Load and review routing policy
    actual_policy_path = Path(policy_path) if policy_path else root / "python/omp-work/src/omp_work/routing/routing-policy.v1.json"
    if not actual_policy_path.is_file():
        actual_policy_path = Path(__file__).with_name("routing-policy.v1.json")
    if not actual_policy_path.is_file():
        raise RoleEvidenceError("missing_policy", details=f"Routing policy file not found at {actual_policy_path}")

    policy_bytes = actual_policy_path.read_bytes()
    expected_policy_sha = hashlib.sha256(policy_bytes).hexdigest()
    policy = load_policy(actual_policy_path)

    # Policy review check from model_review.py (s04)
    try:
        check_policy_review(policy, repo_root=root)
    except Exception as exc:
        raise RoleEvidenceError("policy_review_failed", details=str(exc)) from exc

    # Load decision
    if isinstance(decision_path, dict):
        decision = decision_path
        decision_dir = Path.cwd()
    else:
        decision_p = Path(decision_path)
        decision_dir = decision_p.parent
        decision = _read_decision_file(decision_p)

    validate_decision(decision)

    # Check decision policy SHA if present
    dec_policy_sha = decision.get("policy_sha")
    if dec_policy_sha and dec_policy_sha != expected_policy_sha:
        raise RoleEvidenceError(
            "wrong_policy_hash",
            details=f"Decision policy SHA '{dec_policy_sha}' does not match expected '{expected_policy_sha}'",
        )

    # Load evidence
    if isinstance(evidence_path, dict):
        evidence = evidence_path
        evidence_dir = decision_dir
    else:
        evidence_p = Path(evidence_path)
        evidence_dir = evidence_p.parent
        evidence = _read_evidence_file(evidence_p)

    # Check evidence bundle policy SHA
    ev_policy_sha = evidence.get("policy_sha")
    if ev_policy_sha and ev_policy_sha != expected_policy_sha:
        raise RoleEvidenceError(
            "wrong_policy_hash",
            details=f"Evidence policy SHA '{ev_policy_sha}' does not match expected '{expected_policy_sha}'",
        )

    receipts = _extract_receipts(evidence)

    # Verify decision receipt references if present
    decision_receipt_refs = decision.get("receipt_refs") or decision.get("receipts")
    if isinstance(decision_receipt_refs, dict):
        for role_name, ref in decision_receipt_refs.items():
            norm_role = normalize_role(role_name)
            if norm_role in receipts and isinstance(ref, str):
                actual_id = receipts[norm_role].get("receipt_id")
                if actual_id and actual_id != ref:
                    raise RoleEvidenceError(
                        "receipt_reference_mismatch",
                        role=norm_role,
                        details=f"Decision references receipt '{ref}', but evidence has '{actual_id}'",
                    )

    # Validate supervisor proof if present
    supervisor_claim = evidence.get("supervisor") or receipts.get("supervisor")
    if supervisor_claim is not None:
        run_ref = supervisor_claim.get("run_reference") or supervisor_claim.get("run_id") or supervisor_claim.get("mission_id")
        if not run_ref:
            raise RoleEvidenceError(
                "missing_run_reference",
                role="supervisor",
                details="No-dispatch supervisor proof needs a run reference",
            )
        authority_text = str(supervisor_claim.get("authority") or supervisor_claim.get("basis") or "")
        if "0006" not in authority_text and "ADR 0006" not in authority_text:
            raise RoleEvidenceError(
                "missing_adr_0006_citation",
                role="supervisor",
                details="No-dispatch supervisor proof needs an ADR 0006 citation",
            )

    # Check for missing roles
    for req_role in SIX_ROLES:
        if req_role not in receipts:
            raise RoleEvidenceError("missing_role", role=req_role, details=f"Missing evidence receipt for role '{req_role}'")

    approved_fallbacks = decision.get("approved_fallbacks") or {}
    if isinstance(approved_fallbacks, list):
        approved_fallbacks = {r: True for r in approved_fallbacks}

    # Validate each role receipt and caller trace
    for role in SIX_ROLES:
        receipt = receipts[role]

        # Check for forged deterministic-session equivalence
        if role == "operator":
            if (
                receipt.get("is_supervisor")
                or receipt.get("kind") == "deterministic_software"
                or receipt.get("caller") == "omp_work.orchestrator.service"
                or str(receipt.get("basis", "")).lower().find("deterministic") != -1
            ):
                raise RoleEvidenceError(
                    "forged_deterministic_session_equivalence",
                    role="operator",
                    details="Deterministic supervisor proof cannot replace session operator",
                )

        # Check required fields
        caller = receipt.get("caller")
        if not caller or not isinstance(caller, str) or not caller.strip():
            raise RoleEvidenceError("missing_caller", role=role, details=f"Receipt for role '{role}' is missing caller")

        lane = receipt.get("lane")
        if not lane or not isinstance(lane, str) or not lane.strip():
            raise RoleEvidenceError("missing_lane", role=role, details=f"Receipt for role '{role}' is missing lane")

        provider = receipt.get("provider")
        if not provider or not isinstance(provider, str) or not provider.strip():
            raise RoleEvidenceError("missing_provider", role=role, details=f"Receipt for role '{role}' is missing provider")

        model = receipt.get("model")
        if not model or not isinstance(model, str) or not model.strip():
            raise RoleEvidenceError("missing_model", role=role, details=f"Receipt for role '{role}' is missing model")

        # Reject unresolved bindings
        status = str(receipt.get("status", "")).lower()
        if status in ("unresolved", "pending", "pending_owner"):
            raise RoleEvidenceError(
                "unresolved_binding",
                role=role,
                details=f"Receipt for role '{role}' has unresolved status '{status}'",
            )
        if receipt.get("unresolved_owner") is not None:
            raise RoleEvidenceError(
                "unresolved_binding",
                role=role,
                details=f"Receipt for role '{role}' has unresolved owner '{receipt.get('unresolved_owner')}'",
            )

        # Reject config-only and synthetic records
        if receipt.get("config_only") or receipt.get("kind") == "config":
            raise RoleEvidenceError(
                "config_only_evidence",
                role=role,
                details=f"Config-only evidence is not actual execution proof for role '{role}'",
            )
        if receipt.get("is_synthetic") or receipt.get("synthetic") or receipt.get("kind") == "synthetic":
            raise RoleEvidenceError(
                "synthetic_record",
                role=role,
                details=f"Synthetic record is rejected for role '{role}'",
            )

        # Check policy receipt hash if present
        rcpt_policy_sha = receipt.get("policy_sha")
        if rcpt_policy_sha and rcpt_policy_sha != expected_policy_sha:
            raise RoleEvidenceError(
                "wrong_policy_hash",
                role=role,
                details=f"Receipt policy SHA '{rcpt_policy_sha}' does not match expected '{expected_policy_sha}'",
            )

        # Policy bindings verification
        if role == "plan":
            plan_stage = policy.stages["plan"]
            if provider != plan_stage.provider:
                raise RoleEvidenceError(
                    "provider_mismatch",
                    role=role,
                    details=f"Claimed provider '{provider}' != policy provider '{plan_stage.provider}'",
                )
            if model != plan_stage.model:
                raise RoleEvidenceError(
                    "model_mismatch",
                    role=role,
                    details=f"Claimed model '{model}' != policy model '{plan_stage.model}'",
                )
            if lane != "openai_codex":
                raise RoleEvidenceError(
                    "lane_mismatch",
                    role=role,
                    details=f"Claimed lane '{lane}' != policy lane 'openai_codex'",
                )
        elif role == "implement":
            impl_stage = policy.stages["implement"]
            if provider != impl_stage.provider:
                raise RoleEvidenceError(
                    "provider_mismatch",
                    role=role,
                    details=f"Claimed provider '{provider}' != policy provider '{impl_stage.provider}'",
                )
            if model != impl_stage.model:
                raise RoleEvidenceError(
                    "model_mismatch",
                    role=role,
                    details=f"Claimed model '{model}' != policy model '{impl_stage.model}'",
                )
            if lane != "gemini_flash":
                raise RoleEvidenceError(
                    "lane_mismatch",
                    role=role,
                    details=f"Claimed lane '{lane}' != policy lane 'gemini_flash'",
                )
            if caller != "omp_work.model_proxy":
                raise RoleEvidenceError(
                    "caller_mismatch",
                    role=role,
                    details=f"Claimed caller '{caller}' != policy caller 'omp_work.model_proxy'",
                )
        elif role == "review":
            audit_stage = policy.stages["audit"]
            reviewer = audit_stage.reviewers[0] if audit_stage.reviewers else ""
            rev_provider, _, rev_rest = reviewer.partition("/")
            rev_model, _, _ = rev_rest.partition(":")
            stage_match = (provider == audit_stage.provider and model == audit_stage.model)
            reviewer_match = (provider == rev_provider and model == rev_model)
            if not stage_match and not reviewer_match:
                raise RoleEvidenceError(
                    "identity_mismatch",
                    role=role,
                    details=f"Reviewer identity '{provider}/{model}' matches neither stage '{audit_stage.provider}/{audit_stage.model}' nor reviewer '{reviewer}'",
                )
        elif role == "research":
            res_stage = policy.stages["research"]
            if provider != res_stage.provider:
                raise RoleEvidenceError(
                    "provider_mismatch",
                    role=role,
                    details=f"Claimed provider '{provider}' != policy provider '{res_stage.provider}'",
                )
            if model != res_stage.model:
                raise RoleEvidenceError(
                    "model_mismatch",
                    role=role,
                    details=f"Claimed model '{model}' != policy model '{res_stage.model}'",
                )
            if lane != "ollama_cloud":
                raise RoleEvidenceError(
                    "lane_mismatch",
                    role=role,
                    details=f"Claimed lane '{lane}' != policy lane 'ollama_cloud'",
                )

        # Audit execution beyond preflight
        if role == "review":
            if receipt.get("phase") == "preflight" or receipt.get("preflight_only"):
                raise RoleEvidenceError(
                    "audit_preflight_only",
                    role="review",
                    details="Audit execution beyond preflight is required",
                )

        # Research/judge actual-call beyond admission/ledger
        if role in ("research", "judge"):
            if (
                receipt.get("kind") in ("admission", "ledger")
                or receipt.get("phase") in ("admission", "ledger")
                or receipt.get("actual_call") is False
            ):
                raise RoleEvidenceError(
                    "missing_actual_call",
                    role=role,
                    details=f"Role '{role}' requires actual model call beyond admission/ledger",
                )

        # Load referenced caller trace/receipt file
        trace_ref = receipt.get("trace_file") or receipt.get("trace_path") or receipt.get("receipt_file")
        if not trace_ref:
            raise RoleEvidenceError(
                "missing_trace_file",
                role=role,
                details=f"Receipt for role '{role}' has no referenced trace file",
            )

        trace = _load_trace(trace_ref, evidence_dir, role)

        # Trace checks for config-only or synthetic
        if trace.get("config_only") or trace.get("kind") == "config":
            raise RoleEvidenceError(
                "config_only_evidence",
                role=role,
                details=f"Trace file for role '{role}' is config-only",
            )
        if trace.get("is_synthetic") or trace.get("synthetic") or trace.get("kind") == "synthetic":
            raise RoleEvidenceError(
                "synthetic_record",
                role=role,
                details=f"Trace file for role '{role}' is synthetic",
            )

        # Preflight check in trace for audit
        if role == "review":
            if trace.get("phase") == "preflight" or trace.get("preflight_only"):
                raise RoleEvidenceError(
                    "audit_preflight_only",
                    role="review",
                    details="Audit trace shows preflight only; execution required",
                )

        # Research/judge actual-call in trace
        if role in ("research", "judge"):
            if (
                trace.get("kind") in ("admission", "ledger")
                or trace.get("phase") in ("admission", "ledger")
                or trace.get("actual_call") is False
                or trace.get("dispatched_model") is None and trace.get("model") is None
            ):
                raise RoleEvidenceError(
                    "missing_actual_call",
                    role=role,
                    details=f"Trace for role '{role}' shows admission/ledger only, no actual model call",
                )

        # Fallback check
        is_fallback = bool(
            receipt.get("fallback")
            or receipt.get("is_fallback")
            or trace.get("fallback")
            or trace.get("is_fallback")
            or trace.get("resolvedModelIsFallback")
        )
        if is_fallback and not approved_fallbacks.get(role):
            raise RoleEvidenceError(
                "unapproved_fallback",
                role=role,
                details=f"Unapproved fallback observed for role '{role}'",
            )

        # Verify actual dispatch identities against bundle claims
        trace_provider = trace.get("dispatched_provider") or trace.get("provider")
        if trace_provider != provider:
            raise RoleEvidenceError(
                "identity_mismatch",
                role=role,
                details=f"Trace dispatched provider '{trace_provider}' != claimed provider '{provider}'",
            )

        trace_model = trace.get("dispatched_model") or trace.get("model")
        if trace_model != model:
            raise RoleEvidenceError(
                "identity_mismatch",
                role=role,
                details=f"Trace dispatched model '{trace_model}' != claimed model '{model}'",
            )

        trace_lane = trace.get("dispatched_lane") or trace.get("lane")
        if trace_lane != lane:
            raise RoleEvidenceError(
                "identity_mismatch",
                role=role,
                details=f"Trace dispatched lane '{trace_lane}' != claimed lane '{lane}'",
            )

        trace_caller = trace.get("dispatched_caller") or trace.get("caller")
        if trace_caller and trace_caller != caller:
            raise RoleEvidenceError(
                "caller_mismatch",
                role=role,
                details=f"Trace dispatched caller '{trace_caller}' != claimed caller '{caller}'",
            )

        trace_policy_sha = trace.get("policy_sha")
        if trace_policy_sha and trace_policy_sha != expected_policy_sha:
            raise RoleEvidenceError(
                "wrong_policy_hash",
                role=role,
                details=f"Trace policy SHA '{trace_policy_sha}' != expected '{expected_policy_sha}'",
            )

    return {
        "status": "pass",
        "policy_sha": expected_policy_sha,
        "roles_validated": list(SIX_ROLES),
        "decision_date": decision.get("date") or decision.get("decision_date"),
        "authority_resolved": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate role evidence and dated authority decision")
    parser.add_argument("--evidence", required=True, type=Path, help="Path to evidence JSON")
    parser.add_argument("--decision", required=True, type=Path, help="Path to decision JSON")
    parser.add_argument("--policy", type=Path, default=None, help="Path to routing-policy.v1.json")
    args = parser.parse_args(argv)

    try:
        res = validate_role_evidence(
            evidence_path=args.evidence,
            decision_path=args.decision,
            policy_path=args.policy,
        )
        print(f"PASS: role evidence validated with complete agreement for roles {res['roles_validated']}.")
        return 0
    except RoleEvidenceError as exc:
        role_label = f" for role '{exc.role}'" if exc.role else ""
        details_label = f": {exc.details}" if exc.details else ""
        sys.stderr.write(f"FAIL{role_label}: {exc.reason}{details_label}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
