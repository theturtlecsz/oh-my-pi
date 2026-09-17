from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_OID, UUID, uuid5

from omp_work.knowledge_contracts import (
    ObservationKind,
    SourceObservation,
    SourceRef,
)
from omp_work.v1.canonical import sha256
from omp_work.v1.client import WorkClient


def deterministic_work_item_observation_id(
    workspace_id: UUID,
    work_id: UUID,
    revision_id: UUID | None = None,
    candidate_id: UUID | None = None,
) -> UUID:
    """Deterministic work-item observation identity using existing valid UUID namespace
    helper (NAMESPACE_OID pattern) and stable source identity (workspace + work_id +
    current revision + current candidate, with explicit empty markers).
    """
    rev_marker = str(revision_id) if revision_id is not None else ""
    cand_marker = str(candidate_id) if candidate_id is not None else ""
    key = f"omp-work-item:{workspace_id}:{work_id}:{rev_marker}:{cand_marker}"
    return uuid5(NAMESPACE_OID, key)


def deterministic_receipt_observation_id(workspace_id: UUID, receipt_id: UUID) -> UUID:
    key = f"omp-receipt:{workspace_id}:{receipt_id}"
    return uuid5(NAMESPACE_OID, key)


class NativeExporter:
    """Exports workflow evidence and items from native WorkService into knowledge observations.
    Runs in exact-key mode via existing WorkClient.workflow(key) preserving explicit read caps.
    Never compares captured current workflow to a complete historical export.
    """

    def __init__(self, client: WorkClient, workspace_id: UUID, repository_id: UUID) -> None:
        self.client = client
        self.workspace_id = workspace_id
        self.repository_id = repository_id

    def export_key(self, key: str) -> list[SourceObservation]:
        workflow_view = self.client.workflow(key)
        now = datetime.now(timezone.utc)
        observations: list[SourceObservation] = []

        item = workflow_view.item
        item_payload = item.model_dump(mode="json")
        revision_id = item.revision.revision_id
        candidate_id = item.candidate.candidate_id if item.candidate is not None else None
        item_source = SourceRef(
            workspace_id=self.workspace_id,
            repository_id=self.repository_id,
            work_id=item.work_id,
            revision_id=revision_id,
            candidate_id=candidate_id,
            producer="native_work_exporter:exact_key",
            observed_at=now,
        )
        stored_item_payload = {"type": "work_item", "data": item_payload}
        observations.append(
            SourceObservation(
                observation_id=deterministic_work_item_observation_id(
                    self.workspace_id,
                    item.work_id,
                    revision_id,
                    candidate_id,
                ),
                source=item_source,
                kind=ObservationKind.EXECUTION_TRACE,
                payload=stored_item_payload,
                payload_sha256=sha256(stored_item_payload),
                relevance_tags=(f"work:{item.work_id}", f"key:{key}"),
                observed_at=now,
            )
        )

        # Export receipts as observations
        for receipt in workflow_view.receipts:
            rec_payload = receipt.model_dump(mode="json")
            rec_source = SourceRef(
                workspace_id=self.workspace_id,
                repository_id=self.repository_id,
                work_id=receipt.work_id,
                revision_id=receipt.revision_id,
                candidate_id=receipt.candidate_id,
                candidate_sha256=receipt.candidate_sha256,
                source_revision=receipt.candidate_commit,
                producer=f"native_work_exporter:{receipt.issuer}",
                observed_at=receipt.issued_at,
                native_validity_ref=str(receipt.receipt_id),
            )
            stored_rec_payload = {"type": "receipt", "data": rec_payload}
            observations.append(
                SourceObservation(
                    observation_id=deterministic_receipt_observation_id(self.workspace_id, receipt.receipt_id),
                    source=rec_source,
                    kind=ObservationKind.REVIEW_FINDING if receipt.kind == "audit" else ObservationKind.TOOL_OUTPUT,
                    payload=stored_rec_payload,
                    payload_sha256=sha256(stored_rec_payload),
                    native_payload_sha256=receipt.payload_sha256,
                    relevance_tags=(f"work:{receipt.work_id}", f"receipt:{receipt.receipt_id}", f"kind:{receipt.kind}"),
                    observed_at=receipt.issued_at,
                )
            )

        return observations
