"""OMP-520-s03: the one owner-facing renderer and typed-payload parser for the
`AuditResult` contract model (OMP-520-s01). No store wiring lives here.

`render_audit_result` renders the canonical headed plain text that
`normalize_auditor_report` accepts and that the owner reads. Each section header
stands alone on its line with a blank line between sections; empty lists render
as `- none`.

`parse_typed_audit_payload` narrows one transport payload to the typed result.
The only accepted shape is a dict (or a JSON string decoding to one) carrying
the single key `audit_result`. Any other shape returns None so the caller can
fall back to text normalization, while a present-but-broken typed payload is
reported as `audit_result_invalid`.
"""

from __future__ import annotations

import json

from pydantic import ValidationError

from .models import AuditFinding, AuditResult, EvidenceReference

_EM_DASH = "\u2014"
_ARROW = "\u2192"
_STATUS_LABELS = {"met": "met", "not_met": "not met", "unverifiable": "unverifiable"}
_EMPTY = "- none"


def _render_finding(finding: AuditFinding) -> str:
    location = finding.location
    end = location.line_end
    ref = (
        f"{location.path}:{location.line_start}"
        if end is None
        else f"{location.path}:{location.line_start}-{end}"
    )
    return (
        f"- [{finding.severity}] {finding.criterion_id} {ref} {_EM_DASH} "
        f"evidence: {finding.evidence}; impact: {finding.impact}; "
        f"minimal fix: {finding.minimal_fix}"
    )


def _render_check(reference: EvidenceReference) -> str:
    line = (
        f"- {reference.ref} {_ARROW} {reference.result}"
        if reference.result is not None
        else f"- {reference.ref}"
    )
    if reference.sha256 is not None:
        line = f"{line} (sha256: {reference.sha256})"
    return line


def _section(header: str, lines: tuple[str, ...]) -> str:
    body = lines if lines else (_EMPTY,)
    return "\n".join((header, *body))


def render_audit_result(result: AuditResult) -> str:
    """Render the one canonical headed report for a typed audit result."""
    return "\n\n".join(
        (
            f"VERDICT: {result.verdict}",
            _section(
                "FINDINGS",
                tuple(_render_finding(finding) for finding in result.findings),
            ),
            _section(
                "ACCEPTANCE COVERAGE",
                tuple(
                    f"| {check.criterion_id} | {_STATUS_LABELS[check.status]} | {check.evidence} |"
                    for check in result.criteria
                ),
            ),
            _section(
                "OUT OF SCOPE",
                tuple(f"- {item}" for item in result.out_of_scope),
            ),
            _section(
                "CHECKS RUN",
                tuple(_render_check(reference) for reference in result.evidence.references),
            ),
            _section(
                "REMAINING QUESTIONS",
                tuple(f"- {item}" for item in result.remaining_questions),
            ),
        )
    )


def parse_typed_audit_payload(payload: object) -> AuditResult | str | None:
    """Narrow one transport payload to the typed audit result.

    Returns the validated `AuditResult` when `payload` is (or decodes to) a dict
    with the only key `audit_result`; `"audit_result_invalid"` when a typed
    payload is present but malformed or carries extra keys; None otherwise.
    """
    candidate: object = payload
    if isinstance(payload, str):
        try:
            candidate = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(candidate, dict) or "audit_result" not in candidate:
        return None
    if set(candidate) != {"audit_result"}:
        return "audit_result_invalid"
    try:
        return AuditResult.model_validate(candidate["audit_result"])
    except ValidationError:
        return "audit_result_invalid"
