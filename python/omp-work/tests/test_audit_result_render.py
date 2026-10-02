"""OMP-520-s03: renderer and typed-payload parser for AuditResult.

Contracts:
- `render_audit_result` emits the one canonical headed report that
  `normalize_auditor_report` accepts, one coverage row per criterion, and
  `- none` for every empty section.
- `parse_typed_audit_payload` returns the validated AuditResult for a dict or
  JSON-string `{"audit_result": {...}}`, `"audit_result_invalid"` for a
  present-but-malformed typed payload, and None for any other transport shape.
"""

from __future__ import annotations

import json

from omp_work.v1.audit_result import parse_typed_audit_payload, render_audit_result
from omp_work.v1.models import (
    AuditCriterionCheck,
    AuditFinding,
    AuditFindingLocation,
    AuditResult,
    EvidenceManifest,
    EvidenceReference,
)
from omp_work.v1.semantics import normalize_auditor_report


def _finding() -> AuditFinding:
    return AuditFinding(
        severity="HIGH",
        criterion_id="AC-2",
        location=AuditFindingLocation(path="src/a.py", line_start=10, line_end=12),
        evidence="null deref observed on line 11",
        impact="auditor run panics on empty input",
        minimal_fix="guard the deref",
    )


def _pass_result() -> AuditResult:
    return AuditResult(
        verdict="PASS",
        criteria=(
            AuditCriterionCheck(
                criterion_id="AC-1", status="met", evidence="renderer test passed"
            ),
            AuditCriterionCheck(
                criterion_id="AC-2", status="met", evidence="parser test passed"
            ),
        ),
        evidence=EvidenceManifest(
            references=(
                EvidenceReference(
                    kind="command",
                    ref="pytest tests/test_audit_result_render.py",
                    result="2 passed",
                    sha256="a" * 64,
                ),
                EvidenceReference(
                    kind="file", ref="python/omp-work/src/omp_work/v1/audit_result.py"
                ),
            )
        ),
        out_of_scope=(),
        remaining_questions=(),
    )


def test_render_pass_normalizes_and_lists_every_criterion() -> None:
    result = _pass_result()
    rendered = render_audit_result(result)

    assert normalize_auditor_report(rendered) == (rendered, "PASS")

    coverage_rows = [
        line for line in rendered.splitlines() if line.startswith("| ")
    ]
    assert coverage_rows == [
        "| AC-1 | met | renderer test passed |",
        "| AC-2 | met | parser test passed |",
    ]

    assert rendered.splitlines()[0] == "VERDICT: PASS"
    findings_section = rendered.split("FINDINGS\n", 1)[1].split("\n\n", 1)[0]
    assert findings_section == "- none"

    # SHA-256 surfaces on the check line when the reference carries one.
    assert f"- pytest tests/test_audit_result_render.py \u2192 2 passed (sha256: {'a' * 64})" in rendered
    # A result-less reference still renders as a check line.
    assert "- python/omp-work/src/omp_work/v1/audit_result.py" in rendered


def test_render_needs_fix_finding_and_unmet_row() -> None:
    result = AuditResult(
        verdict="NEEDS_FIX",
        findings=(_finding(),),
        criteria=(
            AuditCriterionCheck(
                criterion_id="AC-2", status="not_met", evidence="defect reproduced"
            ),
        ),
        evidence=EvidenceManifest(
            references=(EvidenceReference(kind="command", ref="pytest", result="1 failed"),)
        ),
        out_of_scope=("nightly fuzzing",),
        remaining_questions=("is the guard hot path?",),
    )
    rendered = render_audit_result(result)

    assert normalize_auditor_report(rendered)[1] == "NEEDS_FIX"

    finding_line = next(
        line for line in rendered.splitlines() if line.startswith("- [HIGH]")
    )
    assert "[HIGH] AC-2 src/a.py:10-12" in finding_line
    assert "| AC-2 | not met | defect reproduced |" in rendered
    assert "- nightly fuzzing" in rendered
    assert "- is the guard hot path?" in rendered


def test_parse_typed_payload_accepts_dict_and_json_string() -> None:
    result = _pass_result()
    payload = {"audit_result": result.model_dump(mode="json")}

    assert parse_typed_audit_payload(payload) == result
    assert parse_typed_audit_payload(json.dumps(payload)) == result


def test_parse_typed_payload_rejects_broken_and_foreign_shapes() -> None:
    invalid_pass = {"audit_result": {"verdict": "PASS", "criteria": (), "evidence": {"references": ()}}}
    assert parse_typed_audit_payload(invalid_pass) == "audit_result_invalid"

    extra_key = {
        "audit_result": _pass_result().model_dump(mode="json"),
        "notes": "extra",
    }
    assert parse_typed_audit_payload(extra_key) == "audit_result_invalid"

    assert parse_typed_audit_payload({"report": "VERDICT: PASS"}) is None
    assert (
        parse_typed_audit_payload(
            "VERDICT: PASS\nFINDINGS\n- none\nACCEPTANCE COVERAGE\ncovered\n"
            "OUT OF SCOPE\nnone\nCHECKS RUN\npytest\nREMAINING QUESTIONS\nnone"
        )
        is None
    )
