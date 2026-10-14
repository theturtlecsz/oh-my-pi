"""Offline tests for role_evidence CLI and validator (OMP-615-s08-s07).

Asserts complete agreement for six execution roles, detection of forged traces,
mutated lanes/providers, missing research/judge actual-call evidence, missing
audit receipts, config-only evidence, wrong policy hashes, unresolved authority,
and forged deterministic-session equivalence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from omp_work.routing.role_evidence import (
    SIX_ROLES,
    RoleEvidenceError,
    find_repo_root,
    main,
    validate_role_evidence,
)


@pytest.fixture
def repo_root() -> Path:
    return find_repo_root()


@pytest.fixture
def policy_sha(repo_root: Path) -> str:
    policy_path = repo_root / "python/omp-work/src/omp_work/routing/routing-policy.v1.json"
    return hashlib.sha256(policy_path.read_bytes()).hexdigest()


@pytest.fixture
def valid_fixture(tmp_path: Path, policy_sha: str) -> dict[str, Path]:
    """Create offline matching fixtures for decision, evidence bundle, and traces."""
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir(parents=True, exist_ok=True)

    trace_files: dict[str, Path] = {}

    traces_data = {
        "plan": {
            "trace_id": "tr-plan-001",
            "receipt_id": "rcpt-plan-001",
            "role": "plan",
            "dispatched_caller": "omp_work.plan_runner",
            "dispatched_provider": "openai_codex",
            "dispatched_model": "gpt-5.6-sol",
            "dispatched_lane": "openai_codex",
            "dispatched_effort": "E2",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "fallback": False,
            "is_synthetic": False,
        },
        "implement": {
            "trace_id": "tr-impl-001",
            "receipt_id": "rcpt-impl-001",
            "role": "implement",
            "dispatched_caller": "omp_work.model_proxy",
            "dispatched_provider": "gemini_flash",
            "dispatched_model": "gemini-3.8-flash-high",
            "dispatched_lane": "gemini_flash",
            "dispatched_effort": "E1",
            "wire": "google-generative-ai",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "fallback": False,
            "is_synthetic": False,
        },
        "review": {
            "trace_id": "tr-rev-001",
            "receipt_id": "rcpt-rev-001",
            "role": "review",
            "dispatched_caller": "session-system/extensions/workflow/auditor-runner.ts",
            "dispatched_provider": "openai-codex",
            "dispatched_model": "gpt-5.6-sol",
            "dispatched_lane": "openai_codex",
            "dispatched_effort": "medium",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "fallback": False,
            "is_synthetic": False,
        },
        "research": {
            "trace_id": "tr-res-001",
            "receipt_id": "rcpt-res-001",
            "role": "research",
            "dispatched_caller": "omp_work.research_runner",
            "dispatched_provider": "ollama_cloud",
            "dispatched_model": "deepseek-v4.1-flash:cloud",
            "dispatched_lane": "ollama_cloud",
            "dispatched_effort": "E1",
            "wire": "ollama-chat",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "actual_call": True,
            "fallback": False,
            "is_synthetic": False,
        },
        "judge": {
            "trace_id": "tr-jdg-001",
            "receipt_id": "rcpt-jdg-001",
            "role": "judge",
            "dispatched_caller": "omp_work.judge_runner",
            "dispatched_provider": "typesafe",
            "dispatched_model": "jev-latest",
            "dispatched_lane": "judge_lane",
            "dispatched_effort": "E1",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "actual_call": True,
            "fallback": False,
            "is_synthetic": False,
        },
        "operator": {
            "trace_id": "tr-opr-001",
            "receipt_id": "rcpt-opr-001",
            "role": "operator",
            "dispatched_caller": "session-system/operator_session",
            "dispatched_provider": "anthropic_fable",
            "dispatched_model": "claude-opus-5-5",
            "dispatched_lane": "anthropic_fable",
            "dispatched_effort": "E2",
            "policy_sha": policy_sha,
            "phase": "execution",
            "kind": "execution",
            "fallback": False,
            "is_synthetic": False,
        },
    }

    for role_name, data in traces_data.items():
        t_path = traces_dir / f"{role_name}_trace.json"
        t_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        trace_files[role_name] = t_path

    evidence_data = {
        "policy_sha": policy_sha,
        "receipts": {
            "plan": {
                "receipt_id": "rcpt-plan-001",
                "role": "plan",
                "caller": "omp_work.plan_runner",
                "provider": "openai_codex",
                "model": "gpt-5.6-sol",
                "lane": "openai_codex",
                "effort": "E2",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "trace_file": "traces/plan_trace.json",
            },
            "implement": {
                "receipt_id": "rcpt-impl-001",
                "role": "implement",
                "caller": "omp_work.model_proxy",
                "provider": "gemini_flash",
                "model": "gemini-3.8-flash-high",
                "lane": "gemini_flash",
                "effort": "E1",
                "wire": "google-generative-ai",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "trace_file": "traces/implement_trace.json",
            },
            "review": {
                "receipt_id": "rcpt-rev-001",
                "role": "review",
                "caller": "session-system/extensions/workflow/auditor-runner.ts",
                "provider": "openai-codex",
                "model": "gpt-5.6-sol",
                "lane": "openai_codex",
                "effort": "medium",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "trace_file": "traces/review_trace.json",
            },
            "research": {
                "receipt_id": "rcpt-res-001",
                "role": "research",
                "caller": "omp_work.research_runner",
                "provider": "ollama_cloud",
                "model": "deepseek-v4.1-flash:cloud",
                "lane": "ollama_cloud",
                "effort": "E1",
                "wire": "ollama-chat",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "actual_call": True,
                "trace_file": "traces/research_trace.json",
            },
            "judge": {
                "receipt_id": "rcpt-jdg-001",
                "role": "judge",
                "caller": "omp_work.judge_runner",
                "provider": "typesafe",
                "model": "jev-latest",
                "lane": "judge_lane",
                "effort": "E1",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "actual_call": True,
                "trace_file": "traces/judge_trace.json",
            },
            "operator": {
                "receipt_id": "rcpt-opr-001",
                "role": "operator",
                "caller": "session-system/operator_session",
                "provider": "anthropic_fable",
                "model": "claude-opus-5-5",
                "lane": "anthropic_fable",
                "effort": "E2",
                "policy_sha": policy_sha,
                "status": "resolved",
                "phase": "execution",
                "trace_file": "traces/operator_trace.json",
            },
        },
    }

    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence_data, indent=2), encoding="utf-8")

    decision_data = {
        "date": "2026-10-14",
        "policy_sha": policy_sha,
        "authority_questions": {
            "Q1": {
                "question": "Do stage policy or native session bindings govern session callers?",
                "selected_option": "A",
                "status": "answered",
                "decision": "Option A: Policy stage/reviewer selectors with evidenced lane mapping govern",
            }
        },
        "receipt_refs": {
            "plan": "rcpt-plan-001",
            "implement": "rcpt-impl-001",
            "review": "rcpt-rev-001",
            "research": "rcpt-res-001",
            "judge": "rcpt-jdg-001",
            "operator": "rcpt-opr-001",
        },
    }

    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps(decision_data, indent=2), encoding="utf-8")

    return {
        "evidence": evidence_path,
        "decision": decision_path,
        "traces_dir": traces_dir,
        "root": tmp_path,
    }


def test_matching_fixture_validates(valid_fixture: dict[str, Path], capsys: pytest.CaptureFixture) -> None:
    res = validate_role_evidence(valid_fixture["evidence"], valid_fixture["decision"])
    assert res["status"] == "pass"
    assert set(res["roles_validated"]) == set(SIX_ROLES)
    assert res["authority_resolved"] is True

    # Test CLI invocation
    ret = main(["--evidence", str(valid_fixture["evidence"]), "--decision", str(valid_fixture["decision"])])
    assert ret == 0
    captured = capsys.readouterr()
    assert "PASS" in captured.out


def test_forged_trace_fails(valid_fixture: dict[str, Path], capsys: pytest.CaptureFixture) -> None:
    plan_trace_path = valid_fixture["traces_dir"] / "plan_trace.json"
    data = json.loads(plan_trace_path.read_text(encoding="utf-8"))
    data["dispatched_model"] = "forged-model"
    plan_trace_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], valid_fixture["decision"])
    assert exc_info.value.reason == "identity_mismatch"
    assert exc_info.value.role == "plan"

    ret = main(["--evidence", str(valid_fixture["evidence"]), "--decision", str(valid_fixture["decision"])])
    assert ret == 1
    err = capsys.readouterr().err
    assert "plan" in err
    assert "identity_mismatch" in err


def test_mutated_provider_same_model_fails(valid_fixture: dict[str, Path]) -> None:
    # Mutate plan provider to 'openai' instead of 'openai_codex' while keeping model gpt-5.6-sol
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["plan"]["provider"] = "openai"
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    plan_trace_path = valid_fixture["traces_dir"] / "plan_trace.json"
    trace_data = json.loads(plan_trace_path.read_text(encoding="utf-8"))
    trace_data["dispatched_provider"] = "openai"
    plan_trace_path.write_text(json.dumps(trace_data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason in ("provider_mismatch", "identity_mismatch")
    assert exc_info.value.role == "plan"


def test_mutated_lane_same_model_fails(valid_fixture: dict[str, Path]) -> None:
    # Mutate plan lane to 'kimi' instead of 'openai_codex' while keeping model gpt-5.6-sol
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["plan"]["lane"] = "kimi"
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    plan_trace_path = valid_fixture["traces_dir"] / "plan_trace.json"
    trace_data = json.loads(plan_trace_path.read_text(encoding="utf-8"))
    trace_data["dispatched_lane"] = "kimi"
    plan_trace_path.write_text(json.dumps(trace_data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason in ("lane_mismatch", "identity_mismatch")
    assert exc_info.value.role == "plan"


def test_missing_research_actual_call_fails(valid_fixture: dict[str, Path]) -> None:
    # Research ledger-only or admission-only receipt/trace
    res_trace_path = valid_fixture["traces_dir"] / "research_trace.json"
    trace_data = json.loads(res_trace_path.read_text(encoding="utf-8"))
    trace_data["kind"] = "ledger"
    trace_data["actual_call"] = False
    trace_data["dispatched_model"] = None
    res_trace_path.write_text(json.dumps(trace_data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], valid_fixture["decision"])
    assert exc_info.value.reason == "missing_actual_call"
    assert exc_info.value.role == "research"


def test_missing_judge_actual_call_fails(valid_fixture: dict[str, Path]) -> None:
    # Judge admission-only without actual model call
    jdg_trace_path = valid_fixture["traces_dir"] / "judge_trace.json"
    trace_data = json.loads(jdg_trace_path.read_text(encoding="utf-8"))
    trace_data["kind"] = "admission"
    trace_data["actual_call"] = False
    trace_data["dispatched_model"] = None
    jdg_trace_path.write_text(json.dumps(trace_data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], valid_fixture["decision"])
    assert exc_info.value.reason == "missing_actual_call"
    assert exc_info.value.role == "judge"


def test_missing_audit_receipt_fails(valid_fixture: dict[str, Path]) -> None:
    # 1. Audit receipt completely missing
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    del data["receipts"]["review"]
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "missing_role"
    assert exc_info.value.role == "review"


def test_audit_preflight_only_fails(valid_fixture: dict[str, Path]) -> None:
    # 2. Audit receipt preflight only
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["review"]["phase"] = "preflight"
    data["receipts"]["review"]["preflight_only"] = True
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "audit_preflight_only"
    assert exc_info.value.role == "review"


def test_config_only_evidence_fails(valid_fixture: dict[str, Path]) -> None:
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["implement"]["config_only"] = True
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "config_only_evidence"
    assert exc_info.value.role == "implement"


def test_synthetic_evidence_fails(valid_fixture: dict[str, Path]) -> None:
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["implement"]["is_synthetic"] = True
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "synthetic_record"
    assert exc_info.value.role == "implement"


def test_wrong_policy_hash_fails(valid_fixture: dict[str, Path]) -> None:
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["policy_sha"] = "0000000000000000000000000000000000000000000000000000000000000000"
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "wrong_policy_hash"


def test_unresolved_authority_fails(valid_fixture: dict[str, Path]) -> None:
    dec_path = valid_fixture["decision"]
    data = json.loads(dec_path.read_text(encoding="utf-8"))
    data["authority_questions"]["Q1"]["status"] = "unanswered"
    dec_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], dec_path)
    assert exc_info.value.reason == "unresolved_authority"


def test_missing_decision_date_fails(valid_fixture: dict[str, Path]) -> None:
    dec_path = valid_fixture["decision"]
    data = json.loads(dec_path.read_text(encoding="utf-8"))
    del data["date"]
    dec_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], dec_path)
    assert exc_info.value.reason == "missing_decision_date"


def test_forged_deterministic_session_equivalence_fails(valid_fixture: dict[str, Path]) -> None:
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    # Claim supervisor deterministic software replaces session operator
    data["receipts"]["operator"]["is_supervisor"] = True
    data["receipts"]["operator"]["caller"] = "omp_work.orchestrator.service"
    data["receipts"]["operator"]["kind"] = "deterministic_software"
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "forged_deterministic_session_equivalence"
    assert exc_info.value.role == "operator"


def test_unapproved_fallback_fails(valid_fixture: dict[str, Path]) -> None:
    impl_trace_path = valid_fixture["traces_dir"] / "implement_trace.json"
    data = json.loads(impl_trace_path.read_text(encoding="utf-8"))
    data["fallback"] = True
    impl_trace_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(valid_fixture["evidence"], valid_fixture["decision"])
    assert exc_info.value.reason == "unapproved_fallback"
    assert exc_info.value.role == "implement"


def test_unresolved_binding_fails(valid_fixture: dict[str, Path]) -> None:
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["receipts"]["judge"]["status"] = "pending_owner"
    data["receipts"]["judge"]["unresolved_owner"] = "Chris"
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "unresolved_binding"
    assert exc_info.value.role == "judge"


def test_supervisor_proof_validation(valid_fixture: dict[str, Path]) -> None:
    # 1. Valid supervisor proof with run_ref and ADR 0006 citation passes
    ev_path = valid_fixture["evidence"]
    data = json.loads(ev_path.read_text(encoding="utf-8"))
    data["supervisor"] = {
        "kind": "deterministic_software",
        "caller": "omp_work.orchestrator.service",
        "run_reference": "run-eng-exec-101",
        "authority": "docs/adr/0006-on-demand-roles.md",
        "note": "Software coordination verified; no LLM dispatch",
    }
    ev_path.write_text(json.dumps(data), encoding="utf-8")

    res = validate_role_evidence(ev_path, valid_fixture["decision"])
    assert res["status"] == "pass"

    # 2. Missing run_reference in supervisor proof fails
    bad_data = copy.deepcopy(data)
    del bad_data["supervisor"]["run_reference"]
    ev_path.write_text(json.dumps(bad_data), encoding="utf-8")
    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "missing_run_reference"

    # 3. Missing ADR 0006 citation in supervisor proof fails
    bad_data_2 = copy.deepcopy(data)
    bad_data_2["supervisor"]["authority"] = "docs/adr/0005-mission-first-architecture.md"
    ev_path.write_text(json.dumps(bad_data_2), encoding="utf-8")
    with pytest.raises(RoleEvidenceError) as exc_info:
        validate_role_evidence(ev_path, valid_fixture["decision"])
    assert exc_info.value.reason == "missing_adr_0006_citation"
