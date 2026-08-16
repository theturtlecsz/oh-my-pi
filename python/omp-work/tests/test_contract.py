from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import omp_work
from omp_work.v1.models import Anomaly, Candidate, CommandEnvelope, CompletionInput, CutoverManifest, EvidenceKind, EvidenceReceipt, RelationEdge, RelationKind, WorkAlias
from omp_work.v1.canonical import command_sha256
from omp_work.v1.semantics import completion_blockers, replay_decision, revision_decision, validate_cutover_manifest, would_create_cycle


NOW = datetime(2026, 8, 15, tzinfo=UTC)
WORK = UUID("00000000-0000-7000-8000-000000000001")
REVISION = UUID("00000000-0000-7000-8000-000000000002")
CANDIDATE = UUID("00000000-0000-7000-8000-000000000003")


def receipt(kind: EvidenceKind, **updates: object) -> EvidenceReceipt:
    data: dict[str, object] = {
        "receipt_id": UUID(f"00000000-0000-7000-8000-00000000000{len(updates) + 4}"),
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_id": CANDIDATE,
        "kind": kind,
        "payload_sha256": "a" * 64,
        "issuer": "owner",
        "issued_at": NOW,
        "candidate_sha256": "b" * 64,
        "candidate_commit": "abcdef123",
    }
    data.update(updates)
    return EvidenceReceipt.model_validate(data)


def candidate(**updates: object) -> Candidate:
    data: dict[str, object] = {
        "candidate_id": CANDIDATE,
        "work_id": WORK,
        "revision_id": REVISION,
        "candidate_sha256": "b" * 64,
        "commit_sha": "abcdef123",
        "allocated_at": NOW,
    }
    data.update(updates)
    return Candidate.model_validate(data)



def test_work_alias_binds_origin_to_immutable_key_namespace() -> None:
    assert WorkAlias(work_id=WORK, key="HOME-142", primary=True, origin="imported").key == "HOME-142"
    assert WorkAlias(work_id=WORK, key="OMP-1", primary=True, origin="local").key == "OMP-1"
    with pytest.raises(ValueError, match="imported aliases"):
        WorkAlias(work_id=WORK, key="OMP-1", primary=True, origin="imported")

def test_immutable_revision_content_requires_append() -> None:
    assert revision_decision("a" * 64, "a" * 64) == "noop"
    assert revision_decision("a" * 64, "b" * 64) == "append"


def test_relation_cycle_rejects_back_edge_and_allows_related_triangle() -> None:
    a, b, c = (UUID(f"00000000-0000-7000-8000-0000000000{n:02d}") for n in range(10, 13))
    edges = (
        RelationEdge(workspace_id=WORK, source_work_id=a, target_work_id=b, kind=RelationKind.BLOCKS),
        RelationEdge(workspace_id=WORK, source_work_id=b, target_work_id=c, kind=RelationKind.BLOCKS),
    )
    assert would_create_cycle(edges, RelationEdge(workspace_id=WORK, source_work_id=c, target_work_id=a, kind=RelationKind.BLOCKS))
    assert not would_create_cycle((), RelationEdge(workspace_id=WORK, source_work_id=a, target_work_id=b, kind=RelationKind.RELATED))


def test_command_envelope_rejects_unknown_nested_payload_fields() -> None:
    envelope = {
        "api_version": "work.omp.dev/v1",
        "workspace_id": str(WORK),
        "operation_id": "00000000-0000-7000-8000-000000000010",
        "request_id": "00000000-0000-7000-8000-000000000011",
        "correlation_id": "00000000-0000-7000-8000-000000000012",
        "command": {"type": "create_work_batch", "payload": {"work_items": ["one"], "unknown": True}},
    }
    with pytest.raises(ValueError, match="unknown"):
        CommandEnvelope.model_validate(envelope)


def test_cutover_rejects_blocking_anomaly_or_parity_difference() -> None:
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest((Anomaly(code="relation_cycle", disposition="blocking"),), ())
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest((Anomaly(code="attachment_content_unavailable", disposition="blocking"),), ())
    with pytest.raises(ValueError, match="cutover invariant"):
        validate_cutover_manifest((), ("unexplained work count",))
    validate_cutover_manifest((Anomaly(code="attachment_content_unavailable", disposition="quarantined"),), ())



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
    command = {"type": "create_work_batch", "payload": {"work_items": ["one"]}}
    first = CommandEnvelope.model_validate({"api_version": "work.omp.dev/v1", "workspace_id": str(WORK), "operation_id": str(uuid4()), "request_id": str(uuid4()), "correlation_id": str(uuid4()), "command": command})
    retry = first.model_copy(update={"request_id": uuid4(), "correlation_id": uuid4()})
    assert command_sha256(first) == command_sha256(retry)


def test_stale_evidence_blocks_completion_after_revision_changes() -> None:
    stale = receipt(EvidenceKind.VERIFICATION, revision_id=UUID("00000000-0000-7000-8000-000000000099"))
    result = completion_blockers(CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(), receipts=(receipt(EvidenceKind.PLAN), stale, receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"), receipt(EvidenceKind.PUSH, remote_commit="abcdef123")), closeout_requested=True))
    assert {blocker.code for blocker in result} >= {"verification_missing", "stale_evidence"}


def test_pushed_branch_requires_remote_candidate_and_preserves_closeout() -> None:
    receipts = (receipt(EvidenceKind.PLAN), receipt(EvidenceKind.VERIFICATION), receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"), receipt(EvidenceKind.PUSH, remote_commit="deadbeef"))
    blocked = completion_blockers(CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(), receipts=receipts, closeout_requested=True))
    assert {blocker.code for blocker in blocked} == {"push_unverified"}
    fresh = (*receipts[:-1], receipt(EvidenceKind.PUSH, remote_commit="abcdef123"))
    assert completion_blockers(CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(), receipts=fresh, closeout_requested=True)) == ()


def test_completion_rejects_work_and_revision_binding_mismatches() -> None:
    receipts = (receipt(EvidenceKind.PLAN), receipt(EvidenceKind.VERIFICATION), receipt(EvidenceKind.AUDIT, independent=True, verdict="PASS"), receipt(EvidenceKind.PUSH, remote_commit="abcdef123"))
    wrong_work = CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(work_id=UUID("00000000-0000-7000-8000-000000000098")), receipts=receipts, closeout_requested=True)
    wrong_revision = CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(revision_id=UUID("00000000-0000-7000-8000-000000000097")), receipts=receipts, closeout_requested=True)
    wrong_receipt = CompletionInput(work_id=WORK, current_revision_id=REVISION, candidate=candidate(), receipts=(*receipts[:-1], receipt(EvidenceKind.PUSH, work_id=UUID("00000000-0000-7000-8000-000000000096"), remote_commit="abcdef123")), closeout_requested=True)
    for input in (wrong_work, wrong_revision, wrong_receipt):
        assert "stale_evidence" in {blocker.code for blocker in completion_blockers(input)}


def test_bundle_approval_and_tamper_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package_root = tmp_path / "omp-work"
    shutil.copytree(Path(__file__).parents[1], package_root)
    contract_dir = package_root / "src/omp_work/contracts/v1"
    monkeypatch.setattr(omp_work, "_contract_dir", lambda: contract_dir)
    (contract_dir / "approval.json").unlink()
    with pytest.raises(ValueError, match="owner approval"):
        omp_work.validate_bundle(require_approval=True)
    approval = {"contract_version": omp_work.CONTRACT_VERSION, "contract_sha256": omp_work.contract_sha256(), "approved_by": "owner", "approved_at": "2026-08-15T00:00:00Z", "issue": "HOME-142"}
    (contract_dir / "approval.json").write_text(json.dumps(approval))
    omp_work.validate_bundle(require_approval=True)
    contract_path = contract_dir / "contract.json"
    contract_path.write_text(contract_path.read_text().replace("HOME team worlds/initiatives", "HOME team workflow worlds/initiatives"))
    with pytest.raises(ValueError, match="approval hash mismatch"):
        omp_work.validate_bundle(require_approval=True)
