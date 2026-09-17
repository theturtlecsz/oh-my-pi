from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from omp_work.knowledge_contracts import (
    ExtractionCoverage,
    ExtractorInfo,
    JobState,
    ProposalState,
    RepositoryAlias,
    RepositoryAliasKind,
    RepositoryBinding,
    RepositoryBindingState,
    SnapshotRef,
    SourceObservation,
    SourceRef,
    compute_ingestion_request_sha256,
    compute_proposal_sha256,
    compute_snapshot_sha256,
    generate_knowledge_schema,
)


def test_knowledge_schema_generation() -> None:
    schema = generate_knowledge_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["contract_version"] == "knowledge.omp.dev/v1"
    models = schema["models"]
    assert "SnapshotRef" in models
    assert "SourceRef" in models
    assert "ProcedureProposal" in models
    assert "RepositoryBinding" in models
    assert "IngestionJob" in models


def test_repository_binding_validation() -> None:
    repo_id = uuid4()
    native_id = uuid4()

    # Valid bound
    binding = RepositoryBinding(
        repository_id=repo_id,
        native_repository_id=native_id,
        state=RepositoryBindingState.BOUND,
    )
    assert binding.state == RepositoryBindingState.BOUND
    assert binding.native_repository_id == native_id

    # Bound without native_repository_id must fail
    with pytest.raises(ValidationError, match="bound repositories must carry native_repository_id"):
        RepositoryBinding(
            repository_id=repo_id,
            native_repository_id=None,
            state=RepositoryBindingState.BOUND,
        )

    # Unbound with native_repository_id must fail
    with pytest.raises(ValidationError, match="unbound repositories must not carry native_repository_id"):
        RepositoryBinding(
            repository_id=repo_id,
            native_repository_id=native_id,
            state=RepositoryBindingState.UNBOUND,
        )


def test_strict_model_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ExtractorInfo(
            name="enola",
            version="1.0.0",
            unexpected_field="disallowed",  # type: ignore[call-arg]
        )


def test_snapshot_sha256_canonical_determinism() -> None:
    repo_id = UUID("00000000-0000-7000-8000-000000000001")
    extractor = ExtractorInfo(name="enola", version="v0.4.18")
    hash1 = compute_snapshot_sha256(
        repository_id=repo_id,
        base_commit="a" * 40,
        tree_sha="b" * 40,
        included_untracked=["untracked/b.py", "untracked/a.py"],
        excluded=[{"path": "x.log", "reason": "log"}],
        extractor=extractor,
    )
    # Order of untracked inputs reversed in call; result must be identical
    hash2 = compute_snapshot_sha256(
        repository_id=repo_id,
        base_commit="a" * 40,
        tree_sha="b" * 40,
        included_untracked=["untracked/a.py", "untracked/b.py"],
        excluded=[{"path": "x.log", "reason": "log"}],
        extractor=extractor,
    )
    assert hash1 == hash2
    assert len(hash1) == 64


def test_proposal_sha256_ignores_ephemeral_fields() -> None:
    base = {
        "title": "Fix memory leak in Ladybug connection pool",
        "preconditions": ["pool_size > 10"],
        "steps": ["close connections after transaction", "reset ladybug context"],
        "applicability_scope": "omp_knowledge.engine",
        "workspace_id": "00000000-0000-7000-8000-000000000001",
        "repository_id": "00000000-0000-7000-8000-000000000002",
    }
    p1 = {**base, "derived_at": "2026-09-12T10:00:00Z", "proposal_id": str(uuid4())}
    p2 = {**base, "derived_at": "2026-09-12T12:00:00Z", "proposal_id": str(uuid4())}

    assert compute_proposal_sha256(p1) == compute_proposal_sha256(p2)


def test_job_state_includes_interrupted() -> None:
    assert JobState.INTERRUPTED.value == "interrupted"


def test_ingest_request_discriminated_shape_and_validation() -> None:
    from pydantic import TypeAdapter
    from omp_knowledge.server import (
        CodeSnapshotIngestRequest,
        IngestRequest,
        NativeRecordIngestRequest,
    )
    from support.fixtures import make_synthetic_enola_snapshot_ref

    repo_id = uuid4()
    ws_id = uuid4()
    snap_ref = make_synthetic_enola_snapshot_ref(repository_id=repo_id, fixture_name="A")

    # Valid CodeSnapshotIngestRequest
    code_req = CodeSnapshotIngestRequest(
        operation_id=uuid4(),
        workspace_id=ws_id,
        repository_id=repo_id,
        snapshot_id=snap_ref.snapshot_id,
        snapshot_ref=snap_ref,
        facts_jsonl="{}",
        receipt_json="{}",
    )
    assert code_req.kind == "code_snapshot"

    # Mismatched snapshot_id between body and snapshot_ref is unrepresentable
    with pytest.raises(ValidationError, match="must match snapshot_ref.snapshot_id"):
        CodeSnapshotIngestRequest(
            operation_id=uuid4(),
            workspace_id=ws_id,
            repository_id=repo_id,
            snapshot_id="sha256:" + "f" * 64,
            snapshot_ref=snap_ref,
            facts_jsonl="{}",
            receipt_json="{}",
        )

    # Missing snapshot_ref is unrepresentable
    with pytest.raises(ValidationError, match="snapshot_ref"):
        TypeAdapter(IngestRequest).validate_python(
            {
                "kind": "code_snapshot",
                "operation_id": str(uuid4()),
                "workspace_id": str(ws_id),
                "repository_id": str(repo_id),
                "snapshot_id": snap_ref.snapshot_id,
                "facts_jsonl": "{}",
                "receipt_json": "{}",
            }
        )
