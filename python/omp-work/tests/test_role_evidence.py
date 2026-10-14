"""Offline boundary fixtures; none attest a live caller or owner approval."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from omp_work.routing.policy import load_policy
from omp_work.routing.role_evidence import (
    ADR_0006, POLICY_PATH, SIX_ROLES, RoleEvidenceError, find_repo_root, validate_role_evidence,
)


def write(path: Path, data: dict) -> str:
    path.write_text(json.dumps(data), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, Path]:
    policy_file = find_repo_root() / POLICY_PATH
    policy = load_policy(policy_file)
    sha = hashlib.sha256(policy_file.read_bytes()).hexdigest()
    evidence = {"policy_sha": sha, "receipts": {}}
    decision = {
        "date": "2026-10-14", "owner": "Chris", "policy_sha": sha,
        "governing_policy": POLICY_PATH,
        "authority_questions": {"Q1": {"status": "answered", "selected_option": "A"}},
        "bindings": {}, "receipt_refs": {},
    }
    # Hypothetical external bindings, explicitly supplied by fixture decision.
    # These names do not assert that execution callers exist in the repository.
    for role in SIX_ROLES:
        stage_name = {"plan": "plan", "implement": "implement", "research": "research"}.get(role)
        route = None
        if stage_name:
            stage = policy.stages[stage_name]
            route = {"provider": stage.provider, "model": stage.model, "effort": stage.effort}
            lane = stage.provider
            selector = {"stage": stage_name}
            wire = policy.raw["governance"]["lanes"][lane]["wire"] or "responses"
            provider = {"plan": "openai-codex", "implement": "google", "research": "ollama"}[role]
            identity = {"provider": provider, "model": stage.model, "effort": "medium"}
        elif role == "review":
            selector = {"stage": "audit", "reviewer_index": 0}
            lane, wire = "openai_codex", "responses"
            identity = {"provider": "openai-codex", "model": "gpt-5.6-sol", "effort": "medium"}
        else:
            selector = None
            lane, wire = "ollama_cloud", "ollama-chat"
            identity = {"provider": "ollama", "model": "external-session-model", "effort": "none"}
        caller = "omp_work.model_proxy" if role == "implement" else f"external.fixture.{role}"
        binding = {
            "status": "resolved", "evidence_reference": f"owner-external-binding:{role}",
            "selector": selector, "caller": caller, "lane": lane, "wire": wire,
            "requested": identity, "resolved": copy.deepcopy(identity),
        }
        if route is not None:
            binding["route"] = route
        decision["bindings"][role] = binding
        trace = {
            "role": role, "receipt_id": f"receipt-{role}", "run_reference": "fixture-run",
            "call_id": f"call-{role}", "policy_sha": sha, "caller": caller, "lane": lane,
            "wire": wire, "requested": identity, "resolved": copy.deepcopy(identity),
            "kind": "execution", "phase": "execution", "actual_call": True,
            "status": "completed", "fallback": False,
        }
        trace_path = tmp_path / f"{role}.json"
        digest = write(trace_path, trace)
        evidence["receipts"][role] = {**trace, "trace": {"file": trace_path.name, "sha256": digest}}
        decision["receipt_refs"][role] = {"receipt_id": trace["receipt_id"], "sha256": digest}
    evidence_path, decision_path = tmp_path / "evidence.json", tmp_path / "decision.json"
    write(evidence_path, evidence)
    write(decision_path, decision)
    return evidence_path, decision_path


def check(bundle: tuple[Path, Path], role: str | None, reason: str) -> None:
    with pytest.raises(RoleEvidenceError) as error:
        validate_role_evidence(*bundle)
    assert (error.value.role, error.value.reason) == (role, reason)


def update_trace(bundle: tuple[Path, Path], role: str, trace: dict, *, claim: bool = False) -> None:
    """Re-pin altered fixture bytes so identity checks, not digest check, run."""
    evidence_path, decision_path = bundle
    evidence, decision = read(evidence_path), read(decision_path)
    receipt = evidence["receipts"][role]
    digest = write(evidence_path.parent / receipt["trace"]["file"], trace)
    if claim:
        receipt = {**trace, "trace": receipt["trace"]}
        evidence["receipts"][role] = receipt
    receipt["trace"]["sha256"] = digest
    decision["receipt_refs"][role]["sha256"] = digest
    write(evidence_path, evidence)
    write(decision_path, decision)


def cli(bundle: tuple[Path, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omp_work.routing.role_evidence", "--evidence", str(bundle[0]),
         "--decision", str(bundle[1])], capture_output=True, text=True, timeout=15,
    )


def test_matching_fixture_and_module_cli_require_six_roles(bundle: tuple[Path, Path]) -> None:
    assert validate_role_evidence(*bundle)["roles_validated"] == list(SIX_ROLES)
    result = cli(bundle)
    assert result.returncode == 0, result.stderr
    assert "roles=plan,implement,review,research,judge,operator" in result.stdout


def test_forged_trace_digest_fails_cli_with_role_and_reason(bundle: tuple[Path, Path]) -> None:
    path = bundle[0].parent / "review.json"
    trace = read(path)
    trace["resolved"]["model"] = "forged"
    write(path, trace)
    result = cli(bundle)
    assert result.returncode == 1
    assert "role=review reason=trace_digest_mismatch" in result.stderr


@pytest.mark.parametrize("role,field,value", [
    ("plan", "model", "forged"), ("review", "provider", "openai"),
    ("implement", "effort", "high"),
])
def test_dispatch_identity_must_match_binding_even_when_claim_agrees(
    bundle: tuple[Path, Path], role: str, field: str, value: str,
) -> None:
    trace = read(bundle[0].parent / f"{role}.json")
    trace["requested"][field] = value
    trace["resolved"][field] = value
    update_trace(bundle, role, trace, claim=True)
    check(bundle, role, "identity_mismatch")


@pytest.mark.parametrize("field,value", [
    ("lane", "kimi"), ("caller", "another.caller"), ("wire", "wrong-protocol"),
    ("receipt_id", "another-receipt"), ("role", "judge"),
])
def test_trace_claim_cannot_override_explicit_binding(
    bundle: tuple[Path, Path], field: str, value: str,
) -> None:
    trace = read(bundle[0].parent / "review.json")
    trace[field] = value
    update_trace(bundle, "review", trace, claim=True)
    check(bundle, "review", "identity_mismatch")


@pytest.mark.parametrize("role,changes,reason", [
    ("research", {"kind": "ledger"}, "missing_actual_call"),
    ("judge", {"kind": "admission"}, "missing_actual_call"),
    ("research", {"actual_call": None}, "missing_actual_call"),
    ("review", {"phase": "preflight"}, "audit_preflight_only"),
    ("implement", {"config_only": True}, "config_only_evidence"),
    ("plan", {"synthetic": True}, "synthetic_record"),
    ("operator", {"kind": "deterministic_software"}, "forged_deterministic_session_equivalence"),
    ("operator", {"caller": "omp_work.orchestrator.service"}, "forged_deterministic_session_equivalence"),
    ("review", {"status": "started"}, "incomplete_execution"),
])
def test_non_execution_records_never_qualify(
    bundle: tuple[Path, Path], role: str, changes: dict, reason: str,
) -> None:
    trace = read(bundle[0].parent / f"{role}.json")
    trace.update(changes)
    update_trace(bundle, role, trace)
    check(bundle, role, reason)


@pytest.mark.parametrize("field,reason", [
    ("caller", "identity_mismatch"), ("lane", "identity_mismatch"),
    ("policy_sha", "wrong_policy_hash"), ("effort", "missing_effort"),
    ("call_id", "missing_call_id"), ("run_reference", "missing_run_reference"),
])
def test_omitted_trace_fields_fail_closed(bundle: tuple[Path, Path], field: str, reason: str) -> None:
    trace = read(bundle[0].parent / "judge.json")
    if field == "effort":
        del trace["resolved"][field]
    else:
        del trace[field]
    update_trace(bundle, "judge", trace)
    check(bundle, "judge", reason)


def test_missing_audit_receipt(bundle: tuple[Path, Path]) -> None:
    evidence = read(bundle[0])
    del evidence["receipts"]["review"]
    write(bundle[0], evidence)
    check(bundle, "review", "missing_role")


@pytest.mark.parametrize("target", ["evidence", "decision", "trace", "claim"])
def test_policy_hash_required_and_exact(bundle: tuple[Path, Path], target: str) -> None:
    if target == "trace":
        trace = read(bundle[0].parent / "implement.json")
        trace["policy_sha"] = "0" * 64
        update_trace(bundle, "implement", trace)
    else:
        path = bundle[1] if target == "decision" else bundle[0]
        data = read(path)
        record = data["receipts"]["implement"] if target == "claim" else data
        record["policy_sha"] = "0" * 64
        write(path, data)
    check(bundle, "implement" if target in ("trace", "claim") else None, "wrong_policy_hash")


@pytest.mark.parametrize("question", ["Q1", "Q2"])
def test_all_authority_questions_require_answer(bundle: tuple[Path, Path], question: str) -> None:
    decision = read(bundle[1])
    decision["authority_questions"][question] = {"status": "pending", "selected_option": "A"}
    write(bundle[1], decision)
    check(bundle, None, "unresolved_authority")


@pytest.mark.parametrize("field,value,reason", [
    ("date", "yesterday", "invalid_date"), ("owner", "fixture", "identity_mismatch"),
    ("governing_policy", "docs/models.md", "identity_mismatch"),
])
def test_dated_owner_authority_required(bundle: tuple[Path, Path], field: str, value: str, reason: str) -> None:
    decision = read(bundle[1])
    decision[field] = value
    write(bundle[1], decision)
    check(bundle, None, reason)


@pytest.mark.parametrize("role,field,value", [
    ("judge", "status", "pending_owner"), ("operator", "unresolved_owner", "Chris"),
    ("research", "lane", "invented_lane"), ("plan", "selector", {"stage": "implement"}),
    ("implement", "wire", "invented-wire"), ("implement", "caller", "forged-caller"),
    ("review", "requested", {"provider": "openai", "model": "gpt-5.6-sol", "effort": "medium"}),
])
def test_decision_cannot_override_resolved_policy(
    bundle: tuple[Path, Path], role: str, field: str, value: object,
) -> None:
    decision = read(bundle[1])
    decision["bindings"][role][field] = value
    write(bundle[1], decision)
    reason = "unresolved_binding" if field in ("status", "unresolved_owner", "lane", "selector") else "identity_mismatch"
    check(bundle, role, reason)


def test_stage_effort_is_separate_from_wire_effort_and_must_agree(bundle: tuple[Path, Path]) -> None:
    decision = read(bundle[1])
    decision["bindings"]["plan"]["route"]["effort"] = "E4"
    write(bundle[1], decision)
    check(bundle, "plan", "identity_mismatch")


@pytest.mark.parametrize("changed_identity", [False, True])
def test_fallback_needs_referenced_owner_artifact(bundle: tuple[Path, Path], changed_identity: bool) -> None:
    trace = read(bundle[0].parent / "implement.json")
    trace["fallback"] = not changed_identity
    if changed_identity:
        trace["resolved"]["model"] = "alternate-model"
    update_trace(bundle, "implement", trace, claim=True)
    check(bundle, "implement", "unapproved_fallback")


def test_exact_substitution_artifact_allows_approved_fallback(bundle: tuple[Path, Path]) -> None:
    trace = read(bundle[0].parent / "implement.json")
    trace["fallback"] = True
    trace["resolved"] = {"provider": "ollama", "model": "deepseek-v4.1-flash:cloud", "effort": "medium"}
    trace["lane"], trace["wire"] = "ollama_cloud", "ollama-chat"
    update_trace(bundle, "implement", trace, claim=True)
    decision = read(bundle[1])
    decision["bindings"]["implement"]["resolved"] = trace["resolved"]
    decision["bindings"]["implement"].update(lane=trace["lane"], wire=trace["wire"])
    approval = {key: trace[key] for key in
                ("role", "policy_sha", "requested", "resolved", "lane", "caller", "run_reference")}
    approval.update(owner="Chris", status="approved", date="2026-10-14", conformance_reference="external-run-123")
    path = bundle[0].parent / "substitution.json"
    digest = write(path, approval)
    decision["approved_fallbacks"] = {"implement": {"file": path.name, "sha256": digest}}
    write(bundle[1], decision)
    assert validate_role_evidence(*bundle)["status"] == "pass"
    approval["resolved"]["provider"] = "another-provider"
    decision["approved_fallbacks"]["implement"]["sha256"] = write(path, approval)
    write(bundle[1], decision)
    check(bundle, "implement", "identity_mismatch")


def test_supervisor_proof_requires_run_and_exact_adr_and_keeps_operator(bundle: tuple[Path, Path]) -> None:
    evidence = read(bundle[0])
    proof = {
        "kind": "deterministic_software", "caller": "omp_work.orchestrator.service",
        "run_reference": "fixture-run", "authority": ADR_0006, "model": None, "lane": None,
        "dispatch_count": 0, "policy_sha": evidence["policy_sha"],
    }
    evidence["supervisor"] = proof
    write(bundle[0], evidence)
    assert validate_role_evidence(*bundle)["status"] == "pass"
    proof["authority"] = "made-up-0006"
    write(bundle[0], evidence)
    check(bundle, "supervisor", "identity_mismatch")
    proof["authority"] = ADR_0006
    del proof["run_reference"]
    write(bundle[0], evidence)
    check(bundle, "supervisor", "missing_run_reference")
    del evidence["receipts"]["operator"]
    write(bundle[0], evidence)
    check(bundle, "operator", "missing_role")


def test_missing_explicit_policy_never_silently_uses_packaged_policy(bundle: tuple[Path, Path]) -> None:
    with pytest.raises(RoleEvidenceError, match="policy_review_failed"):
        validate_role_evidence(*bundle, policy_path=bundle[0].parent / "absent-policy.json")


def test_malformed_receipt_is_cli_failure_without_traceback(bundle: tuple[Path, Path]) -> None:
    evidence = read(bundle[0])
    evidence["receipts"]["review"] = []
    write(bundle[0], evidence)
    result = cli(bundle)
    assert result.returncode == 1
    assert "role=review reason=invalid_shape" in result.stderr
    assert "Traceback" not in result.stderr


def test_reviewer_without_policy_effort_still_requires_explicit_wire_binding(bundle: tuple[Path, Path]) -> None:
    decision = read(bundle[1])
    binding = decision["bindings"]["review"]
    binding.update(selector={"stage": "acceptance_review", "reviewer_index": 0}, lane="kimi", wire="external-wire")
    identity = {"provider": "kimi-code", "model": "k3-256k", "effort": "none"}
    binding.update(requested=identity, resolved=copy.deepcopy(identity))
    write(bundle[1], decision)
    trace = read(bundle[0].parent / "review.json")
    trace.update(lane=binding["lane"], wire=binding["wire"], requested=identity, resolved=copy.deepcopy(identity))
    update_trace(bundle, "review", trace, claim=True)
    assert validate_role_evidence(*bundle)["status"] == "pass"
    trace["resolved"]["effort"] = "high"
    update_trace(bundle, "review", trace, claim=True)
    check(bundle, "review", "unapproved_fallback")
