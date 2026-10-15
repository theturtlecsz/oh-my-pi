"""Offline agreement check for owner decisions and six-role dispatch receipts.

The decision is an owner-supplied trust anchor, not an approval manufactured by
this CLI. Referenced records must come from execution boundaries, not config.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from .model_review import PolicyReviewError, check_policy_review
from .policy import RoutingPolicy, RoutingPolicyError, load_policy

SIX_ROLES = ("plan", "implement", "review", "research", "judge", "operator")
POLICY_PATH = "python/omp-work/src/omp_work/routing/routing-policy.v1.json"
ADR_0006 = "docs/adr/0006-on-demand-roles.md"
IDENTITY_FIELDS = ("provider", "model", "effort")


class RoleEvidenceError(ValueError):
    def __init__(self, reason: str, *, role: str | None = None, details: str = "") -> None:
        self.reason, self.role, self.details = reason, role, details
        super().__init__(f"{role or 'authority'}: {reason}: {details}")


def find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "docs/model-policy-python-evidence.md").is_file():
            return parent
    raise RoleEvidenceError("missing_doc", details="Repository evidence documents unavailable")


def _object(value: Any, name: str, role: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoleEvidenceError("invalid_shape", role=role, details=f"{name} must be an object")
    return value


def _text(record: dict[str, Any], field: str, role: str | None = None) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RoleEvidenceError(f"missing_{field}", role=role, details=field)
    return value


def _read(path: Path, role: str | None = None) -> dict[str, Any]:
    try:
        return _object(json.loads(path.read_text(encoding="utf-8")), str(path), role)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RoleEvidenceError("unreadable_evidence", role=role, details=f"{path}: {exc}") from exc


def _date(record: dict[str, Any], role: str | None = None) -> None:
    try:
        date.fromisoformat(_text(record, "date", role))
    except ValueError as exc:
        raise RoleEvidenceError("invalid_date", role=role, details="Expected ISO calendar date") from exc


def _equal(actual: Any, expected: Any, field: str, role: str | None = None) -> None:
    if actual != expected:
        reason = "wrong_policy_hash" if field == "policy_sha" else "identity_mismatch"
        raise RoleEvidenceError(reason, role=role, details=f"{field}: {actual!r} != {expected!r}")


def _identity(value: Any, role: str) -> dict[str, str]:
    record = _object(value, "wire identity", role)
    return {field: _text(record, field, role) for field in IDENTITY_FIELDS}


def _referenced(record: dict[str, Any], base: Path, role: str) -> dict[str, Any]:
    path = base / _text(record, "file", role)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RoleEvidenceError("unreadable_evidence", role=role, details=str(exc)) from exc
    digest = _text(record, "sha256", role)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise RoleEvidenceError("trace_digest_mismatch", role=role, details=str(path))
    try:
        return _object(json.loads(raw), str(path), role)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RoleEvidenceError("unreadable_evidence", role=role, details=str(exc)) from exc


def validate_decision(decision: dict[str, Any], policy: RoutingPolicy, policy_sha: str) -> None:
    _date(decision)
    _equal(decision.get("owner"), "Chris", "owner")
    _equal(decision.get("policy_sha"), policy_sha, "policy_sha")
    _equal(decision.get("governing_policy"), POLICY_PATH, "governing_policy")
    _equal(policy.raw["governance"]["authority"]["policy"], POLICY_PATH, "policy authority")
    questions = _object(decision.get("authority_questions"), "authority_questions")
    if "Q1" not in questions:
        raise RoleEvidenceError("unresolved_authority", details="Missing Q1")
    for name, answer in questions.items():
        if not (isinstance(answer, dict) and answer.get("status") == "answered"
                and isinstance(answer.get("selected_option"), str) and answer["selected_option"].strip()):
            raise RoleEvidenceError("unresolved_authority", details=name)
    if questions["Q1"]["selected_option"] not in ("A", "B"):
        raise RoleEvidenceError("unresolved_authority", details="Q1 must select A or B")


def _binding(policy: RoutingPolicy, decision: dict[str, Any], role: str) -> dict[str, Any]:
    bindings = _object(decision.get("bindings"), "bindings", role)
    binding = _object(bindings.get(role), "binding", role)
    if binding.get("status") != "resolved" or binding.get("unresolved_owner") is not None:
        raise RoleEvidenceError("unresolved_binding", role=role)
    _text(binding, "evidence_reference", role)
    caller, lane = _text(binding, "caller", role), _text(binding, "lane", role)
    if lane not in policy.concurrency.providers:
        raise RoleEvidenceError("unresolved_binding", role=role, details=f"Unknown lane {lane!r}")
    governance = policy.raw["governance"]
    invocation = governance["roles"][role]["invocation"]
    if invocation.get("caller") is not None:
        _equal(caller, invocation["caller"], "caller binding", role)
    if caller == governance["supervisor"]["caller"]:
        raise RoleEvidenceError("forged_deterministic_session_equivalence", role=role)
    wire = _text(binding, "wire", role)
    governed_wire = governance["lanes"][lane].get("wire")
    if governed_wire is not None:
        _equal(wire, governed_wire, "wire binding", role)
    requested = _identity(binding.get("requested"), role)
    resolved = _identity(binding.get("resolved"), role)
    selectors = governance["roles"][role]["selectors"]
    selector = binding.get("selector")
    if selectors:
        if selector not in selectors:
            raise RoleEvidenceError("unresolved_binding", role=role, details="Selector is not governed for role")
        stage = policy.stages[selector["stage"]]
        if "reviewer_index" in selector:
            # Exact reviewer string, including effort, not a normalized alias.
            base_identity = f"{requested['provider']}/{requested['model']}"
            identity = f"{base_identity}:{requested['effort']}"
            reviewer = stage.reviewers[selector["reviewer_index"]]
            if reviewer != base_identity:
                _equal(identity, reviewer, "reviewer selector", role)
        else:
            route = _object(binding.get("route"), "stage route", role)
            for field in IDENTITY_FIELDS:
                _equal(route.get(field), getattr(stage, field), f"route {field}", role)
            _equal(requested["model"], stage.model, "stage model", role)
            if requested == resolved:
                _equal(lane, stage.provider, "stage lane", role)
            elif not (lane == stage.provider and resolved["model"] == stage.model) and not any(
                         alt.from_provider == stage.provider and alt.to.provider == lane
                         and alt.to.model == resolved["model"] for alt in policy.alternates):
                raise RoleEvidenceError("unapproved_fallback", role=role,
                                        details="Resolved route is not a declared alternate")
            # Lane provider/E-tier and wire provider/effort have separate domains.
            # The explicit owner binding supplies their association.
    elif selector is not None:
        raise RoleEvidenceError("unresolved_binding", role=role, details="No packaged selector for role")
    return binding


def _execution(record: dict[str, Any], role: str) -> None:
    if record.get("config_only") or record.get("kind") == "config":
        raise RoleEvidenceError("config_only_evidence", role=role)
    if record.get("synthetic") or record.get("is_synthetic") or record.get("kind") == "synthetic":
        raise RoleEvidenceError("synthetic_record", role=role)
    if role == "operator" and (record.get("is_supervisor") or record.get("kind") == "deterministic_software"
                               or record.get("caller") == "omp_work.orchestrator.service"):
        raise RoleEvidenceError("forged_deterministic_session_equivalence", role=role)
    if role == "review" and (record.get("phase") == "preflight" or record.get("preflight_only")):
        raise RoleEvidenceError("audit_preflight_only", role=role)
    if record.get("kind") != "execution" or record.get("phase") != "execution" or record.get("actual_call") is not True:
        raise RoleEvidenceError("missing_actual_call", role=role, details="Execution boundary receipt required")
    _text(record, "run_reference", role)
    _text(record, "call_id", role)
    if record.get("status") != "completed":
        raise RoleEvidenceError("incomplete_execution", role=role)


def _fallback(
    trace: dict[str, Any], binding: dict[str, Any], decision: dict[str, Any], base: Path,
    role: str, policy_sha: str,
) -> None:
    requested, resolved = _identity(trace.get("requested"), role), _identity(trace.get("resolved"), role)
    fallback = trace.get("fallback")
    if not isinstance(fallback, bool):
        raise RoleEvidenceError("missing_fallback", role=role)
    if requested != resolved and not fallback:
        raise RoleEvidenceError("unapproved_fallback", role=role, details="Changed identity without substitution")
    if not fallback:
        return
    approvals = _object(decision.get("approved_fallbacks", {}), "approved_fallbacks", role)
    ref = approvals.get(role)
    if not isinstance(ref, dict):
        raise RoleEvidenceError("unapproved_fallback", role=role, details="Missing substitution artifact")
    approval = _referenced(ref, base, role)
    if approval.get("owner") != "Chris" or approval.get("status") != "approved":
        raise RoleEvidenceError("unapproved_fallback", role=role)
    _date(approval, role)
    _text(approval, "conformance_reference", role)
    for field, expected in {"role": role, "policy_sha": policy_sha, "requested": requested,
                            "resolved": resolved, "lane": binding["lane"], "caller": binding["caller"],
                            "run_reference": trace["run_reference"]}.items():
        _equal(approval.get(field), expected, f"substitution {field}", role)


def validate_role_evidence(
    evidence_path: str | Path, decision_path: str | Path, *,
    policy_path: str | Path | None = None, repo_root: Path | None = None,
) -> dict[str, Any]:
    root = repo_root if repo_root is not None else find_repo_root()
    for name in ("model-policy-python-evidence.md", "model-policy-native-evidence.md"):
        try:
            (root / "docs" / name).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RoleEvidenceError("missing_doc", details=str(exc)) from exc
    target = Path(policy_path) if policy_path is not None else root / POLICY_PATH
    try:
        policy_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        policy = load_policy(target)
        check_policy_review(policy, repo_root=root)
    except (OSError, RoutingPolicyError, PolicyReviewError) as exc:
        raise RoleEvidenceError("policy_review_failed", details=str(exc)) from exc
    decision_file, evidence_file = Path(decision_path), Path(evidence_path)
    decision, evidence = _read(decision_file), _read(evidence_file)
    validate_decision(decision, policy, policy_sha)
    _equal(evidence.get("policy_sha"), policy_sha, "policy_sha")
    receipts = _object(evidence.get("receipts"), "receipts")
    refs = _object(decision.get("receipt_refs"), "receipt_refs")
    for role in SIX_ROLES:
        if role not in receipts:
            raise RoleEvidenceError("missing_role", role=role)
    for role in SIX_ROLES:
        binding = _binding(policy, decision, role)
        claim = _object(receipts[role], "receipt claim", role)
        ref = _object(refs.get(role), "decision receipt reference", role)
        trace_ref = _object(claim.get("trace"), "trace reference", role)
        _equal(trace_ref.get("sha256"), _text(ref, "sha256", role), "decision trace digest", role)
        trace = _referenced(trace_ref, evidence_file.parent, role)
        _execution(trace, role)
        _execution(claim, role)
        _fallback(trace, binding, decision, decision_file.parent, role, policy_sha)
        for record in (claim, trace):
            _equal(record.get("policy_sha"), policy_sha, "policy_sha", role)
            _equal(record.get("role"), role, "role", role)
            _equal(record.get("receipt_id"), _text(ref, "receipt_id", role), "receipt_id", role)
            for field in ("caller", "lane", "wire"):
                _equal(record.get(field), binding[field], field, role)
            for field in ("requested", "resolved"):
                _equal(_identity(record.get(field), role), _identity(binding[field], role), field, role)
        for field in ("call_id", "run_reference", "fallback"):
            _equal(claim.get(field), trace.get(field), field, role)
    supervisor = evidence.get("supervisor")
    if supervisor is not None:
        proof = _object(supervisor, "supervisor", "supervisor")
        expected = policy.raw["governance"]["supervisor"]
        _text(proof, "run_reference", "supervisor")
        _equal(proof.get("authority"), ADR_0006, "authority", "supervisor")
        for field in ("kind", "caller", "model", "lane"):
            if field not in proof:
                raise RoleEvidenceError(f"missing_{field}", role="supervisor")
            _equal(proof[field], expected[field], field, "supervisor")
        _equal(proof.get("dispatch_count"), 0, "dispatch_count", "supervisor")
        _equal(proof.get("policy_sha"), policy_sha, "policy_sha", "supervisor")
    return {"status": "pass", "policy_sha": policy_sha, "roles_validated": list(SIX_ROLES),
            "decision_date": decision["date"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--decision", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate_role_evidence(args.evidence, args.decision)
    except RoleEvidenceError as exc:
        print(f"FAIL role={exc.role or 'authority'} reason={exc.reason}: {exc.details}", file=sys.stderr)
        return 1
    print(f"PASS roles={','.join(result['roles_validated'])} policy_sha={result['policy_sha']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
