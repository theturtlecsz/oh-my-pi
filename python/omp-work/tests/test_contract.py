from __future__ import annotations

import io
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
import urllib.error

import omp_work
import omp_work.__main__
from omp_work.v1.models import (
    Anomaly,
    Approval,
    AuditManifest,
    AuditorLaunch,
    Candidate,
    CloseAttempt,
    CommandEnvelope,
    CompletionArtifactReference,
    CompletionBlocker,
    CompletionCheckDefinition,
    CompletionDeliveryBinding,
    CompletionEvidence,
    CompletionInput,
    CompletionRunnerIdentity,
    CompletionSubject,
    CutoverManifest,
    EvidenceKind,
    EvidenceReceipt,
    RelationEdge,
    RelationKind,
    WorkAlias,
)
from omp_work.v1.canonical import command_sha256, sha256, text_sha256
from omp_work.v1.semantics import (
    completion_blockers,
    normalize_auditor_report,
    replay_decision,
    revision_decision,
    validate_completion_evidence,
    validate_cutover_manifest,
    would_create_cycle,
)

NOW = datetime(2026, 8, 15, tzinfo=UTC)
WORK = UUID("00000000-0000-7000-8000-000000000001")
REVISION = UUID("00000000-0000-7000-8000-000000000002")
CANDIDATE = UUID("00000000-0000-7000-8000-000000000003")


def receipt(kind: EvidenceKind, **updates: object) -> EvidenceReceipt:
    data: dict[str, object] = {
        "receipt_id": UUID(f"00000000-0000-7000-8000-{(len(updates) + 4):012x}"),
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_id": CANDIDATE,
        "kind": kind,
        "payload": {"note": kind.value},
        "payload_sha256": "a" * 64,
        "issuer": "owner",
        "issued_at": NOW,
        "candidate_sha256": "b" * 64,
        "candidate_commit": "c" * 40,
    }
    data.update(updates)
    return EvidenceReceipt.model_validate(data)


def candidate(**updates: object) -> Candidate:
    data: dict[str, object] = {
        "candidate_id": CANDIDATE,
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_sha256": "b" * 64,
        "commit_sha": "c" * 40,
        "kind": "final",
        "allocated_at": NOW,
    }
    data.update(updates)
    return Candidate.model_validate(data)


def close_attempt(**updates: object) -> CloseAttempt:
    data: dict[str, object] = {
        "attempt_id": UUID("00000000-0000-7000-8000-0000000000aa"),
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_id": CANDIDATE,
        "plan_receipt_id": UUID("00000000-0000-7000-8000-0000000000ab"),
        "candidate_sha256": "b" * 64,
        "candidate_commit": "c" * 40,
        "owner_session_id": "session-1",
        "owner_session_started_at": NOW,
        "owner_session_start_commit": "e" * 40,
        "repository": "/repo",
        "diff_sha256": "f" * 64,
        "starting_dirty_paths": (),
        "authorization_kind": "summary",
        "authorization_ref": "summary:1",
        "launch_count": 1,
        "accepted_report_count": 1,
        "in_flight_launch_id": None,
        "state": "closeout_requested",
        "requested_at": NOW,
        "closeout_requested_at": NOW,
    }
    data.update(updates)
    return CloseAttempt.model_validate(data)


def test_work_alias_binds_origin_to_immutable_key_namespace() -> None:
    assert (
        WorkAlias(work_id=WORK, key="HOME-142", primary=True, origin="imported").key
        == "HOME-142"
    )
    assert (
        WorkAlias(work_id=WORK, key="OMP-1", primary=True, origin="local").key
        == "OMP-1"
    )
    with pytest.raises(ValueError, match="imported aliases"):
        WorkAlias(work_id=WORK, key="OMP-1", primary=True, origin="imported")


def test_immutable_revision_content_requires_append() -> None:
    assert revision_decision("a" * 64, "a" * 64) == "noop"
    assert revision_decision("a" * 64, "b" * 64) == "append"


def test_relation_cycle_rejects_back_edge_and_allows_related_triangle() -> None:
    a, b, c = (
        UUID(f"00000000-0000-7000-8000-0000000000{n:02d}") for n in range(10, 13)
    )
    edges = (
        RelationEdge(
            workspace_id=WORK,
            source_work_id=a,
            target_work_id=b,
            kind=RelationKind.BLOCKS,
        ),
        RelationEdge(
            workspace_id=WORK,
            source_work_id=b,
            target_work_id=c,
            kind=RelationKind.BLOCKS,
        ),
    )
    assert would_create_cycle(
        edges,
        RelationEdge(
            workspace_id=WORK,
            source_work_id=c,
            target_work_id=a,
            kind=RelationKind.BLOCKS,
        ),
    )
    assert not would_create_cycle(
        (),
        RelationEdge(
            workspace_id=WORK,
            source_work_id=a,
            target_work_id=b,
            kind=RelationKind.RELATED,
        ),
    )


def test_command_envelope_rejects_unknown_nested_payload_fields() -> None:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORK),
        "operation_id": "00000000-0000-7000-8000-000000000010",
        "request_id": "00000000-0000-7000-8000-000000000011",
        "correlation_id": "00000000-0000-7000-8000-000000000012",
        "command": {
            "type": "create_work_batch",
            "payload": {
                "items": [{"client_ref": "one", "title": "one"}],
                "unknown": True,
            },
        },
    }
    with pytest.raises(ValueError, match="unknown"):
        CommandEnvelope.model_validate(envelope)


def test_cutover_rejects_blocking_anomaly_or_parity_difference() -> None:
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest(
            (Anomaly(code="relation_cycle", disposition="blocking"),), ()
        )
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest(
            (Anomaly(code="attachment_content_unavailable", disposition="blocking"),),
            (),
        )
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest((), ("unexplained work count",))
    validate_cutover_manifest(
        (Anomaly(code="attachment_content_unavailable", disposition="quarantined"),), ()
    )


def test_cutover_manifest_requires_every_dimension_count_and_hash() -> None:
    manifest = {
        "epoch_id": "00000000-0000-7000-8000-000000000020",
        "contract_version": "work.omp.dev/v1",
        "contract_sha256": "a" * 64,
        "schema_sha256": "a" * 64,
        "transform_version": "v1",
        "transform_sha256": "a" * 64,
        "source_boundary": "HOME workflow",
        "source_watermark": "2026-08-15T00:00:00Z",
        "raw_export_sha256": "a" * 64,
        "import_batch_id": "00000000-0000-7000-8000-000000000021",
        "dimension_counts": {},
        "dimension_hashes": {},
        "anomalies": [],
        "backup_receipt_sha256": "a" * 64,
        "restore_receipt_sha256": "a" * 64,
        "command_smoke_results": [],
        "code_fingerprint": "code",
        "config_fingerprint": "config",
        "freeze_at": "2026-08-15T00:00:00Z",
        "actor": "owner",
    }
    with pytest.raises(ValueError, match="dimension_counts"):
        CutoverManifest.model_validate(manifest)


def test_idempotent_retry_replays_only_identical_body() -> None:
    request = "c" * 64
    assert replay_decision(request, request) == "replay"
    assert replay_decision(request, "d" * 64) == "conflict"


def test_command_hash_ignores_attempt_identifiers() -> None:
    command = {
        "type": "create_work_batch",
        "payload": {"items": [{"client_ref": "one", "title": "one"}]},
    }
    first = CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(WORK),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )
    retry = first.model_copy(update={"request_id": uuid4(), "correlation_id": uuid4()})
    assert command_sha256(first) == command_sha256(retry)


def test_auditor_report_normalizes_one_serialized_report_wrapper() -> None:
    report = "VERDICT: PASS\nFINDINGS\nnone\nACCEPTANCE COVERAGE\ncovered\nOUT OF SCOPE\nnone\nCHECKS RUN\npytest\nREMAINING QUESTIONS\nnone"
    assert normalize_auditor_report(json.dumps({"report": report})) == (report, "PASS")
    assert (
        normalize_auditor_report(
            json.dumps({"report": json.dumps({"report": report})})
        )[1]
        == "report_wrapper_nested"
    )
    # OMP-123: raw wrapper from task tool yield payload.
    assert normalize_auditor_report({"raw": report}) == (report, "PASS")
    assert normalize_auditor_report(json.dumps({"raw": report})) == (report, "PASS")
    assert normalize_auditor_report(json.dumps({"raw": report}, indent=2)) == (
        report,
        "PASS",
    )
    assert (
        normalize_auditor_report(json.dumps({"raw": json.dumps({"report": report})}))[1]
        == "report_wrapper_nested"
    )
    assert (
        normalize_auditor_report(json.dumps({"raw": json.dumps({"raw": report})}))[1]
        == "report_wrapper_nested"
    )


def test_auditor_report_normalizes_verdict_and_report_wrapper() -> None:
    # Incident 2026-08-21 (OMP-67, specimen 6214e47d): the auditor emitted the
    # verdict as a sibling wrapper key; the report inside was complete.
    report = "VERDICT: PASS\nFINDINGS\nnone\nACCEPTANCE COVERAGE\ncovered\nOUT OF SCOPE\nnone\nCHECKS RUN\npytest\nREMAINING QUESTIONS\nnone"
    assert normalize_auditor_report({"verdict": "PASS", "report": report}) == (
        report,
        "PASS",
    )
    assert normalize_auditor_report(
        json.dumps({"verdict": "PASS", "report": report})
    ) == (report, "PASS")
    # Pretty-printed serialization is the shape every 2026-08-21 burn carried.
    assert normalize_auditor_report(json.dumps({"report": report}, indent=2)) == (
        report,
        "PASS",
    )
    # Wrapper verdict is decoration and must not override the report: mismatch
    # refuses BEFORE section validation (typed precedence).
    assert normalize_auditor_report(
        json.dumps({"verdict": "NEEDS_FIX", "report": report})
    ) == (None, "report_wrapper_verdict_mismatch")
    assert normalize_auditor_report(
        json.dumps({"verdict": "NEEDS_FIX", "report": "VERDICT: PASS\nno sections"})
    ) == (None, "report_wrapper_verdict_mismatch")
    # Present-but-non-string verdict (incl. null) is out of contract.
    assert normalize_auditor_report(
        json.dumps({"verdict": None, "report": report})
    ) == (None, "report_wrapper_invalid")
    assert normalize_auditor_report(
        json.dumps({"verdict": "PASS", "extra": "x", "report": report})
    ) == (None, "report_wrapper_invalid")
    assert normalize_auditor_report(json.dumps({"report": report, "text": report})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report(json.dumps({"raw": report, "report": report})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report(json.dumps({"raw": report, "text": report})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report(json.dumps({"raw": None})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report(json.dumps({"raw": 123})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report(json.dumps({"raw": {"report": report}})) == (
        None,
        "report_wrapper_invalid",
    )
    assert normalize_auditor_report({"verdict": "PASS", "raw": report}) == (
        report,
        "PASS",
    )
    assert normalize_auditor_report(json.dumps({"verdict": "PASS", "raw": report})) == (
        report,
        "PASS",
    )
    assert normalize_auditor_report(
        json.dumps({"verdict": "NEEDS_FIX", "raw": report})
    ) == (None, "report_wrapper_verdict_mismatch")
    assert normalize_auditor_report(
        "Missing `context`. Provide the shared background for this batch."
    ) == (None, "verdict_missing")
    # Strict-parse successes route through the wrapper parser: a decoded
    # top-level non-object is a malformed wrapper (plan: non-object decoded
    # value → report_wrapper_invalid). Decode failures keep the raw path.
    assert normalize_auditor_report(json.dumps([])) == (None, "report_wrapper_invalid")
    assert normalize_auditor_report(json.dumps("x")) == (None, "report_wrapper_invalid")
    assert normalize_auditor_report(json.dumps(None)) == (
        None,
        "report_wrapper_invalid",
    )
    # Leading whitespace strips before the canonical byte-zero VERDICT check.
    assert normalize_auditor_report("\n  " + report) == (report, "PASS")


def test_stale_evidence_blocks_completion_after_revision_changes() -> None:
    stale = receipt(
        EvidenceKind.VERIFICATION,
        revision_id=UUID("00000000-0000-7000-8000-000000000099"),
    )
    result = completion_blockers(
        CompletionInput(
            work_id=WORK,
            current_revision_id=REVISION,
            candidate=candidate(),
            receipts=(
                receipt(EvidenceKind.PLAN),
                stale,
                receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"),
                receipt(EvidenceKind.PUSH, remote_commit="c" * 40),
            ),
            closeout_requested=True,
        )
    )
    assert {blocker.code for blocker in result} >= {
        "verification_missing",
        "stale_evidence",
    }


def test_pushed_branch_requires_remote_candidate_and_preserves_closeout() -> None:
    base = (
        receipt(EvidenceKind.PLAN),
        receipt(EvidenceKind.VERIFICATION),
        receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"),
        receipt(EvidenceKind.CLOSEOUT),
    )
    # Neither shape: remote tip differs and the receipt does not attest the candidate.
    blocked = completion_blockers(
        CompletionInput(
            work_id=WORK,
            current_revision_id=REVISION,
            candidate=candidate(),
            receipts=(
                *base,
                receipt(
                    EvidenceKind.PUSH, remote_commit="d" * 40, candidate_commit=None
                ),
            ),
            closeout_requested=True,
        ),
        attempt=close_attempt(),
    )
    assert {blocker.code for blocker in blocked} == {"push_unverified"}
    # OMP-99 containment shape: candidate_commit names the candidate, remote_commit a distinct non-null tip.
    contained = completion_blockers(
        CompletionInput(
            work_id=WORK,
            current_revision_id=REVISION,
            candidate=candidate(),
            receipts=(
                *base,
                receipt(
                    EvidenceKind.PUSH, remote_commit="d" * 40, candidate_commit="c" * 40
                ),
            ),
            closeout_requested=True,
        ),
        attempt=close_attempt(),
    )
    assert contained == ()
    # candidate_commit alone never satisfies: a null remote_commit still blocks.
    null_remote = completion_blockers(
        CompletionInput(
            work_id=WORK,
            current_revision_id=REVISION,
            candidate=candidate(),
            receipts=(
                *base,
                receipt(
                    EvidenceKind.PUSH, remote_commit=None, candidate_commit="c" * 40
                ),
            ),
            closeout_requested=True,
        ),
        attempt=close_attempt(),
    )
    assert {blocker.code for blocker in null_remote} == {"push_unverified"}


def test_completion_requires_requested_attempt_and_resolved_deliveries() -> None:
    receipts = (
        receipt(EvidenceKind.PLAN),
        receipt(EvidenceKind.VERIFICATION),
        receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"),
        receipt(EvidenceKind.CLOSEOUT),
        receipt(EvidenceKind.PUSH, remote_commit="c" * 40),
    )
    input = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(),
        receipts=receipts,
        closeout_requested=True,
    )
    assert {blocker.code for blocker in completion_blockers(input)} == {
        "attempt_missing"
    }
    audited = close_attempt(state="audited", closeout_requested_at=None)
    assert {
        blocker.code for blocker in completion_blockers(input, attempt=audited)
    } == {"attempt_not_requested"}
    drifted = close_attempt(candidate_commit="d" * 40)
    assert {
        blocker.code for blocker in completion_blockers(input, attempt=drifted)
    } == {"stale_evidence"}
    assert {
        blocker.code
        for blocker in completion_blockers(
            input, attempt=close_attempt(), pending_delivery_count=2
        )
    } == {"delivery_pending"}
    assert completion_blockers(input, attempt=close_attempt()) == ()


def test_completion_rejects_work_and_revision_binding_mismatches() -> None:
    receipts = (
        receipt(EvidenceKind.PLAN),
        receipt(EvidenceKind.VERIFICATION),
        receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"),
        receipt(EvidenceKind.PUSH, remote_commit="c" * 40),
    )
    wrong_work = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(work_id=UUID("00000000-0000-7000-8000-000000000098")),
        receipts=receipts,
        closeout_requested=True,
    )
    wrong_revision = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(revision_id=UUID("00000000-0000-7000-8000-000000000097")),
        receipts=receipts,
        closeout_requested=True,
    )
    wrong_receipt = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(),
        receipts=(
            *receipts[:-1],
            receipt(
                EvidenceKind.PUSH,
                work_id=UUID("00000000-0000-7000-8000-000000000096"),
                remote_commit="c" * 40,
            ),
        ),
        closeout_requested=True,
    )
    for input in (wrong_work, wrong_revision, wrong_receipt):
        assert "stale_evidence" in {
            blocker.code for blocker in completion_blockers(input)
        }



def make_valid_completion_evidence_bundle() -> tuple[
    CompletionEvidence,
    UUID,
    UUID,
    Candidate,
    CloseAttempt,
    AuditManifest,
    AuditorLaunch,
    EvidenceReceipt,
    EvidenceReceipt,
    EvidenceReceipt,
]:
    work_id = WORK
    revision_id = REVISION
    cand = candidate()
    attempt_id = UUID("00000000-0000-7000-8000-0000000000aa")
    manifest_id = UUID("00000000-0000-7000-8000-0000000000bb")
    launch_id = UUID("00000000-0000-7000-8000-0000000000cc")
    verif_id = UUID("00000000-0000-7000-8000-000000000010")
    audit_id = UUID("00000000-0000-7000-8000-000000000020")
    push_id = UUID("00000000-0000-7000-8000-000000000030")

    task_sha = "1" * 64
    audit_report = "VERDICT: PASS\nFINDINGS\nnone"
    audit_payload = {
        "report": audit_report,
        "manifest_id": str(manifest_id),
        "launch_id": str(launch_id),
    }
    push_payload = {
        "repository": "theturtlecsz/oh-my-pi",
        "remote_url": "https://github.com/theturtlecsz/oh-my-pi.git",
        "remote_ref": "refs/heads/main",
        "candidate_commit": cand.commit_sha,
        "result_tip": "d" * 40,
        "prior_tip": "e" * 40,
    }

    verif_receipt = receipt(
        EvidenceKind.VERIFICATION,
        receipt_id=verif_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=cand.candidate_id,
        candidate_sha256=cand.candidate_sha256,
        candidate_commit=cand.commit_sha,
        payload_sha256="2" * 64,
        artifact_sha256="3" * 64,
    )
    audit_receipt = receipt(
        EvidenceKind.AUDIT,
        receipt_id=audit_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=cand.candidate_id,
        candidate_sha256=cand.candidate_sha256,
        candidate_commit=cand.commit_sha,
        payload=audit_payload,
        payload_sha256=sha256(audit_payload),
        artifact_sha256=text_sha256(audit_report),
        issuer="work-service/auditor-settle",
        independent=True,
        verdict="PASS",
    )
    push_receipt = receipt(
        EvidenceKind.PUSH,
        receipt_id=push_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=cand.candidate_id,
        candidate_sha256=cand.candidate_sha256,
        candidate_commit=cand.commit_sha,
        payload=push_payload,
        payload_sha256=sha256(push_payload),
        artifact_sha256=None,
        remote_ref="refs/heads/main",
        remote_commit="d" * 40,
    )

    attempt = close_attempt(
        attempt_id=attempt_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=cand.candidate_id,
        candidate_sha256=cand.candidate_sha256,
        candidate_commit=cand.commit_sha,
        repository="theturtlecsz/oh-my-pi",
    )

    manifest = AuditManifest(
        manifest_id=manifest_id,
        work_id=work_id,
        attempt_id=attempt_id,
        manifest_version=1,
        plan_receipt_id=UUID("00000000-0000-7000-8000-000000000001"),
        verification_receipt_id=verif_id,
        candidate_id=cand.candidate_id,
        candidate_sha256=cand.candidate_sha256,
        candidate_commit=cand.commit_sha,
        task_body="audit task body",
        task_sha256=task_sha,
        section_hashes={},
        created_at=NOW,
    )

    launch = AuditorLaunch(
        launch_id=launch_id,
        attempt_id=attempt_id,
        manifest_id=manifest_id,
        launch_number=1,
        task_sha256=task_sha,
        tool_call_id="call_123",
        reserved_at=NOW,
    )

    evidence = CompletionEvidence(
        runner=CompletionRunnerIdentity(
            issuer="work-service/auditor-settle",
            launch_id=launch_id,
            tool_call_id="call_123",
            task_sha256=task_sha,
            judge_sha256=None,
        ),
        subject=CompletionSubject(
            work_id=work_id,
            revision_id=revision_id,
            candidate_id=cand.candidate_id,
            candidate_sha256=cand.candidate_sha256,
            candidate_commit=cand.commit_sha,
        ),
        check=CompletionCheckDefinition(
            definition="sealed_audit_manifest",
            version=1,
            manifest_id=manifest_id,
            task_sha256=task_sha,
        ),
        result="PASS",
        artifacts=(
            CompletionArtifactReference(
                receipt_id=verif_id,
                kind="verification",
                payload_sha256=verif_receipt.payload_sha256,
                artifact_sha256=verif_receipt.artifact_sha256,
            ),
            CompletionArtifactReference(
                receipt_id=audit_id,
                kind="audit",
                payload_sha256=audit_receipt.payload_sha256,
                artifact_sha256=audit_receipt.artifact_sha256,
            ),
            CompletionArtifactReference(
                receipt_id=push_id,
                kind="push",
                payload_sha256=push_receipt.payload_sha256,
                artifact_sha256=None,
            ),
        ),
        delivery=CompletionDeliveryBinding(
            repository="theturtlecsz/oh-my-pi",
            remote_url="https://github.com/theturtlecsz/oh-my-pi.git",
            remote_ref="refs/heads/main",
            candidate_commit=cand.commit_sha,
            remote_commit="d" * 40,
        ),
    )

    return (
        evidence,
        work_id,
        revision_id,
        cand,
        attempt,
        manifest,
        launch,
        verif_receipt,
        audit_receipt,
        push_receipt,
    )


def test_completion_evidence_model_validation_rejects_invalid_inputs() -> None:
    (
        evidence,
        work_id,
        revision_id,
        cand,
        attempt,
        manifest,
        launch,
        verif_r,
        audit_r,
        push_r,
    ) = make_valid_completion_evidence_bundle()

    # 1. Missing artifact kind (e.g. only 2 artifacts)
    with pytest.raises(ValueError, match="artifacts must contain exactly one"):
        CompletionEvidence(
            runner=evidence.runner,
            subject=evidence.subject,
            check=evidence.check,
            result="PASS",
            artifacts=evidence.artifacts[:2],
            delivery=evidence.delivery,
        )

    # 2. Duplicate artifact kinds
    with pytest.raises(ValueError, match="artifacts must contain exactly one"):
        CompletionEvidence(
            runner=evidence.runner,
            subject=evidence.subject,
            check=evidence.check,
            result="PASS",
            artifacts=(
                evidence.artifacts[0],
                CompletionArtifactReference(
                    receipt_id=uuid4(),
                    kind="verification",
                    payload_sha256="4" * 64,
                    artifact_sha256="5" * 64,
                ),
                evidence.artifacts[2],
            ),
            delivery=evidence.delivery,
        )

    # 3. Duplicate receipt_id
    with pytest.raises(ValueError, match="duplicate artifact receipt_id"):
        CompletionEvidence(
            runner=evidence.runner,
            subject=evidence.subject,
            check=evidence.check,
            result="PASS",
            artifacts=(
                evidence.artifacts[0],
                CompletionArtifactReference(
                    receipt_id=evidence.artifacts[0].receipt_id,
                    kind="audit",
                    payload_sha256="4" * 64,
                    artifact_sha256="5" * 64,
                ),
                evidence.artifacts[2],
            ),
            delivery=evidence.delivery,
        )

    # 4. Malformed task_sha256
    with pytest.raises(ValueError):
        CompletionRunnerIdentity(
            issuer="work-service/auditor-settle",
            launch_id=uuid4(),
            tool_call_id="call_1",
            task_sha256="not-a-hash",
        )
    # 5. Non-PASS result
    with pytest.raises(ValueError):
        CompletionEvidence.model_validate(
            {
                **evidence.model_dump(mode="json"),
                "result": "NEEDS_FIX",
            }
        )


def test_validate_completion_evidence_pure_semantics() -> None:
    (
        evidence,
        work_id,
        revision_id,
        cand,
        attempt,
        manifest,
        launch,
        verif_r,
        audit_r,
        push_r,
    ) = make_valid_completion_evidence_bundle()

    # Valid claim passes with no blockers
    blockers = validate_completion_evidence(
        evidence,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=audit_r,
        push_receipt=push_r,
    )
    assert blockers == ()

    # Altered subject work_id
    bad_subject = evidence.model_copy(
        update={"subject": evidence.subject.model_copy(update={"work_id": uuid4()})}
    )
    b = validate_completion_evidence(
        bad_subject,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=audit_r,
        push_receipt=push_r,
    )
    assert any("subject work_id" in x.detail for x in b)

    # Altered manifest target bindings
    for field, bad_val in [
        ("work_id", uuid4()),
        ("candidate_id", uuid4()),
        ("candidate_sha256", "9" * 64),
        ("candidate_commit", "9" * 40),
    ]:
        bad_manifest = manifest.model_copy(update={field: bad_val})
        b = validate_completion_evidence(
            evidence,
            expected_work_id=work_id,
            expected_revision_id=revision_id,
            expected_candidate=cand,
            attempt=attempt,
            manifest=bad_manifest,
            launch=launch,
            verification_receipt=verif_r,
            audit_receipt=audit_r,
            push_receipt=push_r,
        )
        assert any("manifest target binding mismatch" in x.detail for x in b)

    # Altered runner launch_id
    bad_runner = evidence.model_copy(
        update={"runner": evidence.runner.model_copy(update={"launch_id": uuid4()})}
    )
    b = validate_completion_evidence(
        bad_runner,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=audit_r,
        push_receipt=push_r,
    )
    assert any("runner launch_id mismatch" in x.detail for x in b)

    # Altered audit payload launch_id / manifest_id
    bad_audit_payload = audit_r.model_copy(
        update={"payload": {**audit_r.payload, "launch_id": str(uuid4())}}
    )
    b = validate_completion_evidence(
        evidence,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=bad_audit_payload,
        push_receipt=push_r,
    )
    assert any("audit receipt payload launch_id mismatch" in x.detail for x in b)

    bad_audit_manifest_payload = audit_r.model_copy(
        update={"payload": {**audit_r.payload, "manifest_id": str(uuid4())}}
    )
    b = validate_completion_evidence(
        evidence,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=bad_audit_manifest_payload,
        push_receipt=push_r,
    )
    assert any("audit receipt payload manifest_id mismatch" in x.detail for x in b)

    # Non-PASS audit receipt
    bad_verdict_audit = audit_r.model_copy(update={"verdict": "NEEDS_FIX"})
    b = validate_completion_evidence(
        evidence,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=bad_verdict_audit,
        push_receipt=push_r,
    )
    assert any("audit receipt verdict is NEEDS_FIX, not PASS" in x.detail for x in b)

    # Altered delivery repository
    bad_delivery = evidence.model_copy(
        update={
            "delivery": evidence.delivery.model_copy(
                update={"repository": "other/repo"}
            )
        }
    )
    b = validate_completion_evidence(
        bad_delivery,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=audit_r,
        push_receipt=push_r,
    )
    assert any("push receipt repository mismatch" in x.detail for x in b)

    # Same-basename foreign-path refusal: "/foreign/path/theturtlecsz/oh-my-pi" vs "theturtlecsz/oh-my-pi"
    foreign_path_delivery = evidence.model_copy(
        update={
            "delivery": evidence.delivery.model_copy(
                update={"repository": "/foreign/path/theturtlecsz/oh-my-pi"}
            )
        }
    )
    b = validate_completion_evidence(
        foreign_path_delivery,
        expected_work_id=work_id,
        expected_revision_id=revision_id,
        expected_candidate=cand,
        attempt=attempt,
        manifest=manifest,
        launch=launch,
        verification_receipt=verif_r,
        audit_receipt=audit_r,
        push_receipt=push_r,
        expected_repository="theturtlecsz/oh-my-pi",
    )
    assert any("push receipt repository mismatch" in x.detail for x in b)


def test_bundle_approval_and_tamper_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_root = tmp_path / "omp-work"
    shutil.copytree(Path(__file__).parents[1], package_root)
    contract_dir = package_root / "src/omp_work/contracts/v1"
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: contract_dir)
    (contract_dir / "approval.json").unlink()
    with pytest.raises(ValueError, match="owner approval"):
        omp_work.validate_bundle(require_approval=True)
    approval = {
        "contract_version": omp_work.CONTRACT_VERSION,
        "contract_sha256": omp_work.contract_sha256(),
        "approved_by": "owner",
        "approved_at": "2026-08-15T00:00:00Z",
        "issue": "HOME-142",
    }
    (contract_dir / "approval.json").write_text(json.dumps(approval))
    omp_work.validate_bundle(require_approval=True)
    contract_path = contract_dir / "contract.json"
    contract_path.write_text(
        contract_path.read_text().replace(
            "HOME team worlds/initiatives", "HOME team workflow worlds/initiatives"
        )
    )
    with pytest.raises(ValueError, match="approval hash mismatch"):
        omp_work.validate_bundle(require_approval=True)


def _setup_contract_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package_root = tmp_path / "omp-work"
    shutil.copytree(Path(__file__).parents[1], package_root)
    contract_dir = package_root / "src/omp_work/contracts/v1"
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: contract_dir)
    monkeypatch.setattr(omp_work.__main__, "_contract_dir", lambda: contract_dir)
    return contract_dir


def test_approve_refuses_non_tty_and_preserves_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    sentinel = b'{"sentinel": true}\n'
    approval_path.write_bytes(sentinel)
    fake_stdin = io.StringIO("any\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "owner approval requires an interactive terminal"
    assert approval_path.read_bytes() == sentinel


def test_approve_refuses_disallowed_issue_and_preserves_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    sentinel = b'{"sentinel": true}\n'
    approval_path.write_bytes(sentinel)
    fake_stdin = io.StringIO("any\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "INVALID-999"])
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "approval issue is not allowed by the current contract"
    assert approval_path.read_bytes() == sentinel


def test_approve_refuses_digest_mismatch_and_preserves_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    sentinel = b'{"sentinel": true}\n'
    approval_path.write_bytes(sentinel)
    fake_stdin = io.StringIO("wrong-hash\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "approval digest mismatch"
    assert approval_path.read_bytes() == sentinel


def test_approve_refuses_contract_changed_and_preserves_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    sentinel = b'{"sentinel": true}\n'
    approval_path.write_bytes(sentinel)
    digest = omp_work.contract_sha256()
    fake_stdin = io.StringIO(f"{digest}\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])
    call_count = 0
    orig_hash = omp_work.contract_sha256

    def flaky_hash() -> str:
        nonlocal call_count
        call_count += 1
        return orig_hash() if call_count == 1 else "f" * 64

    monkeypatch.setattr(omp_work.__main__, "contract_sha256", flaky_hash)
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "contract changed during approval"
    assert approval_path.read_bytes() == sentinel


def test_approve_restores_prior_bytes_on_validation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    sentinel = b'{"sentinel": true}\n'
    approval_path.write_bytes(sentinel)
    digest = omp_work.contract_sha256()
    fake_stdin = io.StringIO(f"{digest}\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])

    def failing_validate(*, require_approval: bool = True) -> None:
        raise ValueError("simulated validation failure")

    monkeypatch.setattr(omp_work.__main__, "validate_bundle", failing_validate)
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "simulated validation failure"
    assert approval_path.read_bytes() == sentinel


def test_approve_removes_new_file_on_validation_failure_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    if approval_path.exists():
        approval_path.unlink()
    digest = omp_work.contract_sha256()
    fake_stdin = io.StringIO(f"{digest}\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])

    def failing_validate(*, require_approval: bool = True) -> None:
        raise ValueError("simulated validation failure")

    monkeypatch.setattr(omp_work.__main__, "validate_bundle", failing_validate)
    with pytest.raises(SystemExit) as exc:
        omp_work.__main__.main()
    assert str(exc.value) == "simulated validation failure"
    assert not approval_path.exists()


def test_approve_succeeds_interactively_and_validates_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    contract_dir = _setup_contract_env(tmp_path, monkeypatch)
    approval_path = contract_dir / "approval.json"
    if approval_path.exists():
        approval_path.unlink()
    digest = omp_work.contract_sha256()
    fake_stdin = io.StringIO(f"{digest}\n")
    monkeypatch.setattr(fake_stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "argv", ["omp-work", "approve", "--issue", "HOME-142"])
    omp_work.__main__.main()
    captured = capsys.readouterr()
    assert digest in captured.out
    assert f'"issue": "HOME-142"' in captured.out
    assert "Type the full contract SHA-256 to approve: " in captured.out
    assert f"approved {digest} for HOME-142" in captured.out
    assert approval_path.is_file()
    approval_text = approval_path.read_text()
    assert approval_text.endswith("\n")
    approval_data = json.loads(approval_text)
    assert approval_data["contract_version"] == omp_work.CONTRACT_VERSION
    assert approval_data["contract_sha256"] == digest
    assert approval_data["approved_by"] == "owner"
    assert approval_data["issue"] == "HOME-142"
    # Verify parseable with Approval model
    Approval.model_validate_json(approval_text)
    # Verify passes validate_bundle
    omp_work.validate_bundle(require_approval=True)


def test_planned_candidate_and_missing_closeout_evidence_block_completion() -> None:
    receipts = (
        receipt(EvidenceKind.PLAN),
        receipt(EvidenceKind.VERIFICATION),
        receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"),
        receipt(EvidenceKind.PUSH, remote_commit="c" * 40),
    )
    planned = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(kind="planned", commit_sha=None),
        receipts=receipts,
        closeout_requested=True,
    )
    assert {
        blocker.code
        for blocker in completion_blockers(planned, attempt=close_attempt())
    } >= {"candidate_not_final", "push_unverified"}
    no_closeout = CompletionInput(
        work_id=WORK,
        current_revision_id=REVISION,
        candidate=candidate(),
        receipts=receipts,
        closeout_requested=True,
    )
    assert {
        blocker.code
        for blocker in completion_blockers(no_closeout, attempt=close_attempt())
    } == {"closeout_missing"}


def test_evidence_payload_body_is_bounded() -> None:
    with pytest.raises(ValueError, match="1 MiB"):
        receipt(EvidenceKind.HANDOFF, payload={"data": "x" * 1048576})


def test_batch_payload_validates_refs_and_relations() -> None:
    from omp_work.v1.models import CreateWorkBatchPayload

    with pytest.raises(ValueError, match="unique"):
        CreateWorkBatchPayload.model_validate(
            {
                "items": [
                    {"client_ref": "a", "title": "one"},
                    {"client_ref": "a", "title": "two"},
                ]
            }
        )
    with pytest.raises(ValueError, match="same request"):
        CreateWorkBatchPayload.model_validate(
            {
                "items": [{"client_ref": "a", "title": "one"}],
                "relations": [
                    {"source_ref": "a", "target_ref": "missing", "kind": "parent"}
                ],
            }
        )
    with pytest.raises(ValueError, match="self"):
        CreateWorkBatchPayload.model_validate(
            {
                "items": [{"client_ref": "a", "title": "one"}],
                "relations": [{"source_ref": "a", "target_ref": "a", "kind": "blocks"}],
            }
        )
    with pytest.raises(ValueError, match="DONE"):
        CreateWorkBatchPayload.model_validate(
            {"items": [{"client_ref": "a", "title": "one", "state": "DONE"}]}
        )
    payload = CreateWorkBatchPayload.model_validate(
        {"items": [{"client_ref": "a", "title": "one"}], "relations": []}
    )
    assert payload.items[0].state == "BACKLOG"

def test_candidate_hash_matches_golden_vectors() -> None:
    from omp_work.v1.canonical import CANDIDATE_HASH_ALGORITHM, candidate_sha256

    fixture = json.loads(
        (Path(omp_work._contract_dir()) / "candidate-hash.json").read_text(
            encoding="utf-8"
        )
    )
    assert fixture["algorithm"] == CANDIDATE_HASH_ALGORITHM
    for vector in fixture["vectors"]:
        # Sorting and validation live inside the helper: input order and stored bytes are pinned.
        basis = vector.get("path_basis", "commit-diff")
        assert (
            candidate_sha256(vector["commit_sha"], vector["paths"], path_basis=basis)
            == vector["candidate_sha256"]
        ), vector["name"]
        assert (
            sorted(vector["paths"], key=lambda path: path.encode("utf-8"))
            == vector["paths_sorted"]
        ), vector["name"]


def test_candidate_hash_rejects_noncanonical_inputs() -> None:
    from omp_work.v1.canonical import candidate_sha256

    with pytest.raises(ValueError):
        candidate_sha256("0123456789abcdef0123456789abcdef01234567", [])
    with pytest.raises(ValueError):
        candidate_sha256("0123456789abcdef0123456789abcdef01234567", ["a.ts", "a.ts"])
    with pytest.raises(ValueError):
        candidate_sha256("0123456789abcdef0123456789abcdef01234567", ["dir/"])
    with pytest.raises(ValueError):
        candidate_sha256(
            "abc123", ["a.ts"]
        )  # abbreviated object ids never bind a candidate
    with pytest.raises(ValueError, match="unknown candidate path_basis"):
        candidate_sha256(
            "0123456789abcdef0123456789abcdef01234567", ["a.ts"], path_basis="unknown"  # type: ignore[arg-type]
        )

def test_client_config_lands_on_the_shared_ts_client_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cross-language contract: session-system/extensions/workflow/config.ts reads
    # exactly XDG_CONFIG_HOME/omp-work/client.json; the CLI must write it there.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from omp_work.operations.capabilities import write_client_config
    from omp_work.operations.config import OperationsConfig

    config = OperationsConfig(
        config_dir=tmp_path / "svc",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        port=54322,
    )
    bearer = tmp_path / "owner.json"
    bearer.write_text("{}")
    bearer.chmod(0o600)
    path = write_client_config(
        config,
        workspace_id=WORK,
        owner_id=REVISION,
        base_url="http://127.0.0.1:54322",
        bearer_file=bearer,
    )
    assert path == tmp_path / "omp-work" / "client.json"
    assert path.stat().st_mode & 0o777 == 0o600
    data = json.loads(path.read_text())
    assert data["workspace_id"] == str(WORK)
    assert data["bearer_file"] == str(bearer)
    with pytest.raises(ValueError):
        write_client_config(
            config,
            workspace_id=WORK,
            owner_id=REVISION,
            base_url="https://work.example.com",
            bearer_file=bearer,
        )


def test_credentials_init_is_idempotent(tmp_path: Path) -> None:
    from omp_work.operations.cli import credentials_init
    from omp_work.operations.config import OperationsConfig

    config = OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )
    first = credentials_init(config)
    assert credentials_init(config) == first
    workspace_id, actor_id = first
    assert config.workspace_id() == workspace_id and config.actor_id() == actor_id


def test_ts_contract_constant_matches_owner_approval() -> None:
    """OMP-136/OMP-147: the generated TS constant, the live contract digest, and
    the owner-approved approval.json must agree — a drifted constant ships a
    host that the service will (correctly) refuse."""
    import re

    ts_path = (
        Path(__file__).parents[3] / "packages" / "work-client" / "src" / "contract.ts"
    )
    match = re.search(r'WORK_CONTRACT_SHA256 = "([0-9a-f]{64})"', ts_path.read_text())
    assert match, "contract.ts must export a 64-hex WORK_CONTRACT_SHA256 literal"
    literal = match.group(1)
    assert literal == omp_work.contract_sha256(), (
        "contract.ts literal must equal the live contract digest"
    )
    approval = json.loads(
        (Path(omp_work._contract_dir()) / "approval.json").read_text()
    )
    assert literal == approval["contract_sha256"], (
        "contract.ts literal must equal the owner-approved digest"
    )
