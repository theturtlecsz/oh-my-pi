from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5

import pytest

from omp_knowledge.native.exporter import (
    NativeExporter,
    deterministic_receipt_observation_id,
    deterministic_work_item_observation_id,
)
from omp_work.knowledge_contracts import ObservationKind, SourceObservation, SourceRef
from omp_work.v1.api_models import WorkItemView, WorkflowView
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    Candidate,
    EvidenceKind,
    EvidenceReceipt,
    WorkAlias,
    WorkRevision,
)


class FakeWorkClient:
    """Typed test double providing WorkClient.workflow() interface."""

    def __init__(self, workflow_view: WorkflowView) -> None:
        self._workflow_view = workflow_view
        self.requested_keys: list[str] = []

    def workflow(self, key: str) -> WorkflowView:
        self.requested_keys.append(key)
        return self._workflow_view


def _make_work_revision(
    work_id: UUID,
    revision_id: UUID | None = None,
    revision_number: int = 1,
) -> WorkRevision:
    rev_id = revision_id or uuid4()
    return WorkRevision(
        revision_id=rev_id,
        work_id=work_id,
        revision_number=revision_number,
        title="Repair native exporter",
        description="Verify exporter behavior with typed fakes",
        scope="python/omp-knowledge",
        acceptance_criteria=("Replay produces stable identity",),
        content_sha256="1" * 64,
        created_by="agent",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def _make_candidate(
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID | None = None,
) -> Candidate:
    cand_id = candidate_id or uuid4()
    return Candidate(
        candidate_id=cand_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_sha256="2" * 64,
        commit_sha=None,
        kind="planned",
        allocated_at=datetime(2026, 1, 1, 12, 10, 0, tzinfo=timezone.utc),
    )


def _make_evidence_receipt(
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID,
    receipt_id: UUID | None = None,
    kind: EvidenceKind = EvidenceKind.AUDIT,
    payload: dict[str, Any] | None = None,
) -> EvidenceReceipt:
    rec_id = receipt_id or uuid4()
    rec_payload = payload if payload is not None else {"status": "passed", "checks": 3}
    return EvidenceReceipt(
        receipt_id=rec_id,
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        kind=kind,
        payload=rec_payload,
        payload_sha256=sha256(rec_payload),
        issuer="auditor-1",
        issued_at=datetime(2026, 1, 1, 12, 20, 0, tzinfo=timezone.utc),
        candidate_sha256="2" * 64,
        candidate_commit="a" * 40,
        verdict="PASS" if kind == EvidenceKind.AUDIT else None,
    )


def test_exporter_replay_stable_observation_ids() -> None:
    """Exact-key replay identity is stable across repeated invocations."""
    ws_id = uuid4()
    repo_id = uuid4()
    work_id = uuid4()

    revision = _make_work_revision(work_id=work_id)
    candidate = _make_candidate(work_id=work_id, revision_id=revision.revision_id)
    alias = WorkAlias(work_id=work_id, key="OMP-101", primary=True, origin="local")

    item = WorkItemView(
        work_id=work_id,
        workspace_id=ws_id,
        alias=alias,
        state="in_progress",
        revision=revision,
        candidate=candidate,
    )
    receipt1 = _make_evidence_receipt(
        work_id=work_id,
        revision_id=revision.revision_id,
        candidate_id=candidate.candidate_id,
        kind=EvidenceKind.AUDIT,
        payload={"audit": "clean", "passed": True},
    )
    receipt2 = _make_evidence_receipt(
        work_id=work_id,
        revision_id=revision.revision_id,
        candidate_id=candidate.candidate_id,
        kind=EvidenceKind.VERIFICATION,
        payload={"verification": "green"},
    )

    wf_view = WorkflowView(item=item, receipts=(receipt1, receipt2))
    client = FakeWorkClient(wf_view)
    exporter = NativeExporter(client=client, workspace_id=ws_id, repository_id=repo_id)  # type: ignore[arg-type]

    # First export run
    observations_1 = exporter.export_key("OMP-101")
    assert len(observations_1) == 3

    # Replay export run
    observations_2 = exporter.export_key("OMP-101")
    assert len(observations_2) == 3

    # Every observation ID and payload hash must be stable and identical across replays
    for obs1, obs2 in zip(observations_1, observations_2, strict=True):
        assert obs1.observation_id == obs2.observation_id
        assert obs1.payload_sha256 == obs2.payload_sha256
        assert obs1.native_payload_sha256 == obs2.native_payload_sha256
        assert obs1.kind == obs2.kind
        assert obs1.relevance_tags == obs2.relevance_tags


def test_exporter_absent_candidate_marker() -> None:
    """When candidate is absent, observation ID uses the explicit empty marker and candidate_id is None."""
    ws_id = uuid4()
    repo_id = uuid4()
    work_id = uuid4()

    revision = _make_work_revision(work_id=work_id)
    alias = WorkAlias(work_id=work_id, key="OMP-102", primary=True, origin="local")

    # Item without candidate
    item_no_cand = WorkItemView(
        work_id=work_id,
        workspace_id=ws_id,
        alias=alias,
        state="open",
        revision=revision,
        candidate=None,
    )
    wf_view_no_cand = WorkflowView(item=item_no_cand)
    client_no_cand = FakeWorkClient(wf_view_no_cand)
    exporter_no_cand = NativeExporter(client=client_no_cand, workspace_id=ws_id, repository_id=repo_id)  # type: ignore[arg-type]

    observations = exporter_no_cand.export_key("OMP-102")
    assert len(observations) == 1
    obs = observations[0]

    # Source candidate_id must be None
    assert obs.source.candidate_id is None
    assert obs.source.revision_id == revision.revision_id
    assert obs.source.work_id == work_id

    # Observation ID must equal deterministic helper with None candidate
    expected_deterministic_id = deterministic_work_item_observation_id(
        ws_id,
        work_id,
        revision.revision_id,
        None,
    )
    assert obs.observation_id == expected_deterministic_id

    # Explicit empty marker in UUID5 namespace key: cand_marker is ""
    empty_marker_key = f"omp-work-item:{ws_id}:{work_id}:{revision.revision_id}:"
    expected_empty_marker_id = uuid5(NAMESPACE_OID, empty_marker_key)
    assert obs.observation_id == expected_empty_marker_id

    # Contrast with item that has candidate present
    candidate = _make_candidate(work_id=work_id, revision_id=revision.revision_id)
    item_with_cand = WorkItemView(
        work_id=work_id,
        workspace_id=ws_id,
        alias=alias,
        state="in_progress",
        revision=revision,
        candidate=candidate,
    )
    wf_view_with_cand = WorkflowView(item=item_with_cand)
    client_with_cand = FakeWorkClient(wf_view_with_cand)
    exporter_with_cand = NativeExporter(client=client_with_cand, workspace_id=ws_id, repository_id=repo_id)  # type: ignore[arg-type]

    observations_with_cand = exporter_with_cand.export_key("OMP-102")
    obs_with_cand = observations_with_cand[0]

    assert obs_with_cand.source.candidate_id == candidate.candidate_id
    assert obs_with_cand.observation_id != obs.observation_id

    cand_key = f"omp-work-item:{ws_id}:{work_id}:{revision.revision_id}:{candidate.candidate_id}"
    assert obs_with_cand.observation_id == uuid5(NAMESPACE_OID, cand_key)
    assert obs_with_cand.observation_id == deterministic_work_item_observation_id(
        ws_id,
        work_id,
        revision.revision_id,
        candidate.candidate_id,
    )


def test_exporter_receipt_hash_separation() -> None:
    """Receipt observations have distinct canonical payload_sha256 and native_payload_sha256."""
    ws_id = uuid4()
    repo_id = uuid4()
    work_id = uuid4()

    revision = _make_work_revision(work_id=work_id)
    candidate = _make_candidate(work_id=work_id, revision_id=revision.revision_id)
    alias = WorkAlias(work_id=work_id, key="OMP-103", primary=True, origin="local")

    item = WorkItemView(
        work_id=work_id,
        workspace_id=ws_id,
        alias=alias,
        state="in_progress",
        revision=revision,
        candidate=candidate,
    )

    audit_payload = {"audit_type": "security", "passed": True, "vulnerabilities": 0}
    audit_receipt = _make_evidence_receipt(
        work_id=work_id,
        revision_id=revision.revision_id,
        candidate_id=candidate.candidate_id,
        kind=EvidenceKind.AUDIT,
        payload=audit_payload,
    )
    verification_payload = {"test_suite": "unit", "exit_code": 0}
    verification_receipt = _make_evidence_receipt(
        work_id=work_id,
        revision_id=revision.revision_id,
        candidate_id=candidate.candidate_id,
        kind=EvidenceKind.VERIFICATION,
        payload=verification_payload,
    )

    wf_view = WorkflowView(item=item, receipts=(audit_receipt, verification_receipt))
    client = FakeWorkClient(wf_view)
    exporter = NativeExporter(client=client, workspace_id=ws_id, repository_id=repo_id)  # type: ignore[arg-type]

    observations = exporter.export_key("OMP-103")
    assert len(observations) == 3

    receipt_observations = observations[1:]
    receipts = [audit_receipt, verification_receipt]

    for obs, rec in zip(receipt_observations, receipts, strict=True):
        # Native payload sha256 must match receipt.payload_sha256
        assert obs.native_payload_sha256 == rec.payload_sha256
        # Stored payload must be {type: 'receipt', data: rec_payload}
        expected_stored_payload = {"type": "receipt", "data": rec.model_dump(mode="json")}
        assert obs.payload == expected_stored_payload
        # Canonical hash must be sha256 of the stored observation payload
        assert obs.payload_sha256 == sha256(expected_stored_payload)
        # Receipt hash separation: canonical observation hash is distinct from native hash
        assert obs.payload_sha256 != obs.native_payload_sha256
        assert len(obs.payload_sha256) == 64
        assert len(obs.native_payload_sha256) == 64

        # Deterministic observation ID
        assert obs.observation_id == deterministic_receipt_observation_id(ws_id, rec.receipt_id)

        # Source identity semantics preserved
        assert obs.source.workspace_id == ws_id
        assert obs.source.repository_id == repo_id
        assert obs.source.work_id == rec.work_id
        assert obs.source.revision_id == rec.revision_id
        assert obs.source.candidate_id == rec.candidate_id
        assert obs.source.candidate_sha256 == rec.candidate_sha256
        assert obs.source.source_revision == rec.candidate_commit
        assert obs.source.native_validity_ref == str(rec.receipt_id)
        assert obs.source.observed_at == rec.issued_at
        assert obs.source.producer == f"native_work_exporter:{rec.issuer}"

    # Verify kind mapping: audit is REVIEW_FINDING, others are TOOL_OUTPUT
    assert receipt_observations[0].kind == ObservationKind.REVIEW_FINDING
    assert receipt_observations[1].kind == ObservationKind.TOOL_OUTPUT


def test_exporter_work_item_payload_no_now() -> None:
    """Work-item observation payload uses full stored payload without runtime 'now'."""
    ws_id = uuid4()
    repo_id = uuid4()
    work_id = uuid4()

    revision = _make_work_revision(work_id=work_id)
    alias = WorkAlias(work_id=work_id, key="OMP-104", primary=True, origin="local")

    item = WorkItemView(
        work_id=work_id,
        workspace_id=ws_id,
        alias=alias,
        state="in_progress",
        revision=revision,
        candidate=None,
    )
    wf_view = WorkflowView(item=item)
    client = FakeWorkClient(wf_view)
    exporter = NativeExporter(client=client, workspace_id=ws_id, repository_id=repo_id)  # type: ignore[arg-type]

    observations = exporter.export_key("OMP-104")
    assert len(observations) == 1
    obs = observations[0]

    raw_item_payload = item.model_dump(mode="json")
    expected_payload = {"type": "work_item", "data": raw_item_payload}

    # Stored payload matches expected full payload
    assert obs.payload == expected_payload
    # payload_sha256 is the hash of the full stored payload
    assert obs.payload_sha256 == sha256(expected_payload)
    # Different from the hash of just the inner item payload
    assert obs.payload_sha256 != sha256(raw_item_payload)

    # Neither the outer payload nor inner data should contain a runtime 'now'
    assert "now" not in obs.payload
    assert "now" not in obs.payload["data"]
