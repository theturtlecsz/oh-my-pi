"""OMP-520-s01: typed audit result and evidence manifest contract tests.

Validates that:
- Full PASS and NEEDS_FIX results validate according to contract schema.
- Each constraint rejects:
  - PASS with not_met criterion.
  - PASS without evidence.
  - NEEDS_FIX without findings.
  - Duplicate criterion_id in criteria.
  - Bad severity patterns.
  - line_end < line_start in finding locations.
  - Newlines in evidence or free-text fields.
  - Unknown keys (strict models forbid extra fields).
- Approval.issue admits OMP-520.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omp_work import generate_schema
from omp_work.v1.models import (
    Approval,
    AuditCriterionCheck,
    AuditFinding,
    AuditFindingLocation,
    AuditResult,
    EvidenceManifest,
    EvidenceReference,
)


def _location(**overrides: object) -> AuditFindingLocation:
    payload: dict[str, object] = {
        "path": "python/omp-work/src/omp_work/v1/models.py",
        "line_start": 10,
        "line_end": 20,
    }
    payload.update(overrides)
    return AuditFindingLocation.model_validate(payload)


def _finding(**overrides: object) -> AuditFinding:
    payload: dict[str, object] = {
        "severity": "HIGH",
        "criterion_id": "CRIT-01",
        "location": _location(),
        "evidence": "Unchecked pointer dereference at line 10",
        "impact": "Potential service panic on null input",
        "minimal_fix": "Add non-null assertion or guard",
    }
    payload.update(overrides)
    return AuditFinding.model_validate(payload)


def _criterion(**overrides: object) -> AuditCriterionCheck:
    payload: dict[str, object] = {
        "criterion_id": "CRIT-01",
        "status": "met",
        "evidence": "Verification test passed with exit code 0",
    }
    payload.update(overrides)
    return AuditCriterionCheck.model_validate(payload)


def _evidence_ref(**overrides: object) -> EvidenceReference:
    payload: dict[str, object] = {
        "kind": "command",
        "ref": "pytest python/omp-work/tests",
        "result": "11 passed in 0.30s",
        "sha256": "a" * 64,
    }
    payload.update(overrides)
    return EvidenceReference.model_validate(payload)


def _evidence_manifest(**overrides: object) -> EvidenceManifest:
    payload: dict[str, object] = {
        "references": (_evidence_ref(),),
    }
    payload.update(overrides)
    return EvidenceManifest.model_validate(payload)


def _pass_result(**overrides: object) -> AuditResult:
    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "PASS",
        "findings": (),
        "criteria": (_criterion(),),
        "evidence": _evidence_manifest(),
        "out_of_scope": ("Fuzzing stress suite deferred to nightlies",),
        "remaining_questions": ("Can we cache compiled schemas?",),
    }
    payload.update(overrides)
    return AuditResult.model_validate(payload)


def _needs_fix_result(**overrides: object) -> AuditResult:
    payload: dict[str, object] = {
        "schema_version": 1,
        "verdict": "NEEDS_FIX",
        "findings": (_finding(),),
        "criteria": (_criterion(status="not_met"),),
        "evidence": _evidence_manifest(),
        "out_of_scope": (),
        "remaining_questions": (),
    }
    payload.update(overrides)
    return AuditResult.model_validate(payload)


def test_full_pass_result_validates() -> None:
    result = _pass_result()
    assert result.schema_version == 1
    assert result.verdict == "PASS"
    assert len(result.findings) == 0
    assert len(result.criteria) == 1
    assert result.criteria[0].status == "met"
    assert len(result.evidence.references) == 1
    assert result.out_of_scope == ("Fuzzing stress suite deferred to nightlies",)
    assert result.remaining_questions == ("Can we cache compiled schemas?",)


def test_needs_fix_result_validates() -> None:
    result = _needs_fix_result()
    assert result.schema_version == 1
    assert result.verdict == "NEEDS_FIX"
    assert len(result.findings) == 1
    assert result.findings[0].severity == "HIGH"
    assert result.findings[0].location.path == "python/omp-work/src/omp_work/v1/models.py"
    assert result.findings[0].location.line_start == 10
    assert result.findings[0].location.line_end == 20
    assert len(result.criteria) == 1
    assert result.criteria[0].status == "not_met"


def test_blocked_result_validates() -> None:
    blocked = AuditResult(
        verdict="BLOCKED",
        evidence=EvidenceManifest(references=()),
        out_of_scope=(),
        remaining_questions=("Blocked on upstream credentials",),
    )
    assert blocked.verdict == "BLOCKED"
    assert blocked.findings == ()
    assert blocked.criteria == ()
    assert blocked.evidence.references == ()


def test_pass_with_not_met_rejected() -> None:
    crit_not_met = _criterion(status="not_met")
    with pytest.raises(ValidationError, match="PASS requires all criteria to be met"):
        AuditResult(
            verdict="PASS",
            criteria=(crit_not_met,),
            evidence=_evidence_manifest(),
        )

    crit_unverifiable = _criterion(status="unverifiable")
    with pytest.raises(ValidationError, match="PASS requires all criteria to be met"):
        AuditResult(
            verdict="PASS",
            criteria=(crit_unverifiable,),
            evidence=_evidence_manifest(),
        )


def test_pass_without_evidence_rejected() -> None:
    with pytest.raises(ValidationError, match="PASS requires at least 1 evidence reference"):
        AuditResult(
            verdict="PASS",
            criteria=(_criterion(),),
            evidence=EvidenceManifest(references=()),
        )


def test_pass_without_criteria_rejected() -> None:
    with pytest.raises(ValidationError, match="PASS requires at least 1 criterion"):
        AuditResult(
            verdict="PASS",
            criteria=(),
            evidence=_evidence_manifest(),
        )


def test_needs_fix_without_findings_rejected() -> None:
    with pytest.raises(ValidationError, match="NEEDS_FIX requires at least 1 finding"):
        AuditResult(
            verdict="NEEDS_FIX",
            findings=(),
            criteria=(_criterion(status="not_met"),),
            evidence=_evidence_manifest(),
        )


def test_duplicate_criterion_id_rejected() -> None:
    c1 = _criterion(criterion_id="CRIT-01")
    c2 = _criterion(criterion_id="CRIT-01")
    with pytest.raises(ValidationError, match="duplicate criterion_id: CRIT-01"):
        AuditResult(
            verdict="PASS",
            criteria=(c1, c2),
            evidence=_evidence_manifest(),
        )


def test_bad_severity_rejected() -> None:
    # Lowercase rejected
    with pytest.raises(ValidationError):
        _finding(severity="high")

    # Leading digit rejected
    with pytest.raises(ValidationError):
        _finding(severity="1HIGH")

    # Underscore rejected
    with pytest.raises(ValidationError):
        _finding(severity="HIGH_SEV")

    # Empty string rejected
    with pytest.raises(ValidationError):
        _finding(severity="")

    # Special characters rejected
    with pytest.raises(ValidationError):
        _finding(severity="HIGH!")

    # Valid severities accepted
    for valid_sev in ("HIGH", "LOW", "MEDIUM", "CRITICAL", "P0", "S1", "SEVERITY2"):
        finding = _finding(severity=valid_sev)
        assert finding.severity == valid_sev


def test_line_end_less_than_line_start_rejected() -> None:
    with pytest.raises(ValidationError, match="line_end must be greater than or equal to line_start"):
        AuditFindingLocation(path="test.py", line_start=10, line_end=9)

    # line_start < 1 rejected
    with pytest.raises(ValidationError):
        AuditFindingLocation(path="test.py", line_start=0, line_end=5)

    # line_end == line_start is accepted (single-line finding)
    loc_same = AuditFindingLocation(path="test.py", line_start=10, line_end=10)
    assert loc_same.line_start == 10
    assert loc_same.line_end == 10

    # line_end None is accepted
    loc_open = AuditFindingLocation(path="test.py", line_start=10, line_end=None)
    assert loc_open.line_start == 10
    assert loc_open.line_end is None


def test_newline_in_evidence_and_free_text_rejected() -> None:
    # Newline in AuditFinding.evidence
    with pytest.raises(ValidationError):
        _finding(evidence="Line 1\nLine 2")

    with pytest.raises(ValidationError):
        _finding(evidence="Line 1\rLine 2")

    # Newline in AuditCriterionCheck.evidence
    with pytest.raises(ValidationError):
        _criterion(evidence="Check passed\nExtra details")

    # Newline in AuditFindingLocation.path
    with pytest.raises(ValidationError):
        AuditFindingLocation(path="src/v1/\nmodels.py", line_start=1)

    # Newline in finding impact, minimal_fix, criterion_id
    with pytest.raises(ValidationError):
        _finding(impact="Impact\nDetail")

    with pytest.raises(ValidationError):
        _finding(minimal_fix="Fix line 1\nFix line 2")

    with pytest.raises(ValidationError):
        _finding(criterion_id="CRIT\n01")

    # Newline in EvidenceReference ref and result
    with pytest.raises(ValidationError):
        _evidence_ref(ref="pytest\n")

    with pytest.raises(ValidationError):
        _evidence_ref(result="passed\nfailed")

    # Newline in out_of_scope and remaining_questions
    with pytest.raises(ValidationError):
        _pass_result(out_of_scope=("scope item 1\nscope item 2",))

    with pytest.raises(ValidationError):
        _pass_result(remaining_questions=("question 1\nquestion 2",))


def test_unknown_key_rejected() -> None:
    # AuditResult rejects unknown key
    raw_pass = _pass_result().model_dump()
    raw_pass["unexpected_key"] = "bogus"
    with pytest.raises(ValidationError):
        AuditResult.model_validate(raw_pass)

    # AuditFinding rejects unknown key
    raw_finding = _finding().model_dump()
    raw_finding["unknown_field"] = 123
    with pytest.raises(ValidationError):
        AuditFinding.model_validate(raw_finding)

    # AuditFindingLocation rejects unknown key
    raw_loc = _location().model_dump()
    raw_loc["column"] = 5
    with pytest.raises(ValidationError):
        AuditFindingLocation.model_validate(raw_loc)

    # AuditCriterionCheck rejects unknown key
    raw_crit = _criterion().model_dump()
    raw_crit["score"] = 100
    with pytest.raises(ValidationError):
        AuditCriterionCheck.model_validate(raw_crit)

    # EvidenceReference rejects unknown key
    raw_ev = _evidence_ref().model_dump()
    raw_ev["timestamp"] = "2026-10-02"
    with pytest.raises(ValidationError):
        EvidenceReference.model_validate(raw_ev)

    # EvidenceManifest rejects unknown key
    with pytest.raises(ValidationError):
        EvidenceManifest.model_validate({"references": (), "extra": True})


def test_approval_issue_omp_520() -> None:
    approval = Approval(
        contract_version="work.omp.dev/v1",
        contract_sha256="0" * 64,
        approved_by="owner",
        approved_at=datetime.now(UTC),
        issue="OMP-520",
    )
    assert approval.issue == "OMP-520"


def test_schema_includes_audit_result_models() -> None:
    schema = generate_schema()
    models = schema["models"]
    for expected_model in (
        "AuditFindingLocation",
        "AuditFinding",
        "AuditCriterionCheck",
        "EvidenceReference",
        "EvidenceManifest",
        "AuditResult",
    ):
        assert expected_model in models, f"missing {expected_model} in generated schema"
