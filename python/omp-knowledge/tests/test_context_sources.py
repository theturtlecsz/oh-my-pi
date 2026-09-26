"""Contract tests for exact, structural and procedural context retrieval (OMP-311 / FK-6).

Each test defends an externally observable retrieval contract:

- a receipt is current only when its revision and candidate match the item, and
  an older revision or another candidate is stale with both ids named;
- a snapshot of an unpermitted repository is denied without any store query, an
  unpublished selection is missing, and a published fixture snapshot yields
  symbol-map items in a stable order;
- a withdrawn procedure renders nowhere and is excluded, a precondition
  mismatch yields nothing, and the learning store file is byte-unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from omp_knowledge.context.compiler import compile_bundle
from omp_knowledge.context.models import CompileRequest
from omp_knowledge.context.sources import (
    exact_items,
    identity_from_view,
    procedural_items,
    structural_items,
)
from omp_knowledge.learning.store import LearningStore
from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.v1.api_models import WorkflowView, WorkItemView
from omp_work.v1.canonical import sha256
from omp_work.v1.models import (
    Candidate,
    EvidenceKind,
    EvidenceReceipt,
    WorkAlias,
    WorkRevision,
)
from support.fixtures import load_staged_fixture

WS = uuid5(NAMESPACE_URL, "omp-test/fk6-workspace")
REPO = uuid5(NAMESPACE_URL, "omp-test/fk6-repository")
OTHER_REPO = uuid5(NAMESPACE_URL, "omp-test/fk6-other-repository")

_PAYLOAD_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SNAP_A = "5dc87df1316f9afab59d47c42eed60f6a3d78286d9dc852d5b9ff827b66d4aee"


class WordCounter:
    """Deterministic fake tokenizer: one token per whitespace-separated word."""

    profile = "test-words-v1"

    def count(self, texts: Sequence[str]) -> list[int]:
        return [len(text.split()) for text in texts]


def _revision(
    *,
    revision_id: UUID,
    work_id: UUID,
    title: str = "Ship the thing",
    description: str = "do the work",
    scope: str = "python/omp-knowledge",
) -> WorkRevision:
    return WorkRevision(
        revision_id=revision_id,
        work_id=work_id,
        revision_number=1,
        title=title,
        description=description,
        scope=scope,
        acceptance_criteria=("first criterion", "second criterion"),
        content_sha256="a" * 64,
        created_by="tester",
        created_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
    )


def _view(
    *,
    work_id: UUID | None = None,
    revision_id: UUID | None = None,
    candidate_id: UUID | None = None,
    with_candidate: bool = True,
    receipts: tuple[EvidenceReceipt, ...] = (),
) -> WorkflowView:
    work_id = work_id or uuid4()
    revision_id = revision_id or uuid4()
    candidate = (
        Candidate(
            candidate_id=candidate_id or uuid4(),
            work_id=work_id,
            revision_id=revision_id,
            candidate_sha256="b" * 64,
            allocated_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        )
        if with_candidate
        else None
    )
    item = WorkItemView(
        work_id=work_id,
        workspace_id=uuid4(),
        alias=WorkAlias(
            work_id=work_id, key="OMP-311", primary=True, origin="local"
        ),
        state="in_progress",
        revision=_revision(revision_id=revision_id, work_id=work_id),
        candidate=candidate,
    )
    return WorkflowView(item=item, receipts=receipts)


def _receipt(
    *,
    work_id: UUID,
    revision_id: UUID,
    candidate_id: UUID | None,
    kind: EvidenceKind = EvidenceKind.VERIFICATION,
    verdict: str | None = "PASS",
) -> EvidenceReceipt:
    return EvidenceReceipt(
        receipt_id=uuid4(),
        work_id=work_id,
        revision_id=revision_id,
        candidate_id=candidate_id,
        kind=kind,
        payload={"exit_code": 0},
        payload_sha256=_PAYLOAD_SHA,
        issuer="tester",
        issued_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        verdict=verdict,
    )


def _published_store(
    state_dir: Path, *, repository_id: UUID = REPO
) -> tuple[StructuralPublicationStore, str]:
    facts_bytes, receipt_bytes, _insights, _raw = load_staged_fixture("A")
    store = StructuralPublicationStore(state_dir)
    store.stage_enola_snapshot(
        workspace_id=WS,
        repository_id=repository_id,
        snapshot_id=_SNAP_A,
        facts_bytes=facts_bytes,
        receipt_bytes=receipt_bytes,
        manifest_files=[],
    )
    store.publish(workspace_id=WS, repository_id=repository_id, snapshot_id=_SNAP_A)
    return store, _SNAP_A


# ---------------------------------------------------------------------------
# identity_from_view
# ---------------------------------------------------------------------------


def test_identity_from_view_binds_work_key_and_candidate() -> None:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    view = _view(work_id=work_id, revision_id=revision_id, candidate_id=candidate_id)

    identity = identity_from_view(view, "implement", "attempt-1")

    assert identity.work_id == str(work_id)
    assert identity.work_key == "OMP-311"
    assert identity.revision_id == str(revision_id)
    assert identity.candidate_id == str(candidate_id)
    assert identity.stage == "implement"
    assert identity.attempt_id == "attempt-1"


def test_identity_from_view_without_candidate_is_none() -> None:
    view = _view(with_candidate=False)

    identity = identity_from_view(view, "plan", "attempt-2")

    assert identity.candidate_id == "none"


# ---------------------------------------------------------------------------
# exact_items
# ---------------------------------------------------------------------------


def test_exact_items_render_revision_and_current_receipt() -> None:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    receipt = _receipt(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id
    )
    view = _view(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id,
        receipts=(receipt,),
    )

    items = exact_items(view)

    source = next(item for item in items if item.ref == str(revision_id))
    assert source.section == "exact"
    assert source.mandatory is True
    assert source.status == "current"
    assert "Ship the thing" in source.text
    assert "do the work" in source.text
    assert "python/omp-knowledge" in source.text
    assert "1. first criterion" in source.text
    assert "2. second criterion" in source.text

    rendered = next(item for item in items if item.ref == str(receipt.receipt_id))
    assert rendered.status == "current"
    assert rendered.text == (
        f"verification PASS receipt={receipt.receipt_id} payload_sha256={_PAYLOAD_SHA}"
    )


def test_exact_items_older_revision_and_other_candidate_are_stale() -> None:
    work_id = uuid4()
    revision_id = uuid4()
    other_revision_id = uuid4()
    candidate_id = uuid4()
    other_candidate_id = uuid4()
    current = _receipt(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id
    )
    older = _receipt(
        work_id=work_id, revision_id=other_revision_id, candidate_id=candidate_id
    )
    other = _receipt(
        work_id=work_id, revision_id=revision_id, candidate_id=other_candidate_id
    )
    view = _view(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id,
        receipts=(current, older, other),
    )

    by_ref = {item.ref: item for item in exact_items(view)}

    assert by_ref[str(current.receipt_id)].status == "current"
    assert by_ref[str(older.receipt_id)].status == "stale"
    assert by_ref[str(other.receipt_id)].status == "stale"
    # The stale detail names the receipt's and the item's ids.
    detail = by_ref[str(older.receipt_id)].detail
    assert str(other_revision_id) in detail
    assert str(revision_id) in detail
    assert str(candidate_id) in detail
    detail = by_ref[str(other.receipt_id)].detail
    assert str(other_candidate_id) in detail
    assert str(candidate_id) in detail


# ---------------------------------------------------------------------------
# structural_items
# ---------------------------------------------------------------------------


def test_structural_items_deny_unpermitted_repo_without_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _snapshot = _published_store(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(store, "query", lambda **_: calls.append("query"))
    monkeypatch.setattr(
        store, "is_published", lambda **_: calls.append("is_published") or True
    )

    items = structural_items(
        store,
        selection=[(WS, OTHER_REPO, _SNAP_A)],
        permitted_repo_ids=[str(REPO)],
        limit=50,
    )

    assert calls == []
    assert len(items) == 1
    assert items[0].status == "denied"
    assert str(OTHER_REPO) in items[0].detail


def test_structural_items_missing_when_selection_not_published(
    tmp_path: Path,
) -> None:
    store = StructuralPublicationStore(tmp_path)
    unpublished = "c" * 64

    items = structural_items(
        store,
        selection=[(WS, REPO, unpublished)],
        permitted_repo_ids=[str(REPO)],
        limit=50,
    )

    assert len(items) == 1
    assert items[0].status == "missing"
    assert items[0].ref == unpublished


def test_structural_items_fixture_snapshot_symbol_map_is_stable(
    tmp_path: Path,
) -> None:
    store, snapshot = _published_store(tmp_path)
    selection = [(WS, REPO, snapshot)]
    permitted = [str(REPO)]

    first = structural_items(store, selection=selection, permitted_repo_ids=permitted, limit=100)
    second = structural_items(store, selection=selection, permitted_repo_ids=permitted, limit=100)

    assert first
    assert [(item.ref, item.text, item.status) for item in first] == [
        (item.ref, item.text, item.status) for item in second
    ]
    assert all(item.section == "structural" for item in first)
    assert all(item.status == "current" for item in first)
    # One symbol-map line per hit, ref is the structural fact id.
    assert any(item.text.endswith("symbol py/pkg/alpha.normalize") for item in first)
    assert len({item.ref for item in first}) == len(first)


def test_structural_items_query_limit_bounds_hits(tmp_path: Path) -> None:
    store, snapshot = _published_store(tmp_path)

    items = structural_items(
        store,
        selection=[(WS, REPO, snapshot)],
        permitted_repo_ids=[str(REPO)],
        limit=2,
    )

    assert len(items) == 2


# ---------------------------------------------------------------------------
# procedural_items
# ---------------------------------------------------------------------------


def _seed_procedure(
    path: Path,
    *,
    steps: tuple[str, ...],
    title: str = "Fix the flake",
    preconditions_json: str = "[]",
    status: str = "active",
    procedure_id: str = "proc-1",
    version: int = 1,
) -> None:
    store = LearningStore(path)
    with store.transaction() as conn:
        conn.execute(
            """
            INSERT INTO procedures (
                procedure_id, fingerprint, status, current_version, title, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '2026-09-26T00:00:00Z', '2026-09-26T00:00:00Z')
            """,
            (procedure_id, sha256([procedure_id]), status, version, title),
        )
        conn.execute(
            """
            INSERT INTO procedure_versions (
                procedure_id, version, title, steps_json, preconditions_json,
                model, profile, source_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'm', 'p', '{}', '2026-09-26T00:00:00Z')
            """,
            (
                procedure_id,
                version,
                title,
                json.dumps(list(steps)),
                preconditions_json,
            ),
        )
    store.close()


def test_procedural_items_active_renders_title_and_steps(tmp_path: Path) -> None:
    db = tmp_path / "learning.sqlite"
    _seed_procedure(db, steps=("run tests", "fix root cause"))

    items = procedural_items(db, {"project_id": str(uuid4()), "repository": "repo"})

    assert len(items) == 1
    assert items[0].status == "current"
    assert items[0].ref == "proc-1 @v1"
    assert items[0].text == "Fix the flake: run tests; fix root cause"


def test_procedural_items_precondition_mismatch_yields_nothing(tmp_path: Path) -> None:
    db = tmp_path / "learning.sqlite"
    _seed_procedure(
        db,
        steps=("only when repo matches",),
        preconditions_json='[{"key": "repository", "op": "eq", "value": "repo-a"}]',
    )

    mismatch = procedural_items(db, {"project_id": "p", "repository": "repo-b"})
    match = procedural_items(db, {"project_id": "p", "repository": "repo-a"})

    assert mismatch == ()
    assert len(match) == 1


def test_procedural_items_ne_precondition_holds(tmp_path: Path) -> None:
    db = tmp_path / "learning.sqlite"
    _seed_procedure(
        db,
        steps=("not on the excluded repo",),
        preconditions_json='[{"key": "repository", "op": "ne", "value": "repo-a"}]',
    )

    assert len(procedural_items(db, {"project_id": "p", "repository": "repo-b"})) == 1
    assert procedural_items(db, {"project_id": "p", "repository": "repo-a"}) == ()


def test_procedural_items_absent_file_is_missing(tmp_path: Path) -> None:
    items = procedural_items(tmp_path / "absent.sqlite", {"project_id": "p", "repository": "r"})

    assert len(items) == 1
    assert items[0].status == "missing"


def test_procedural_items_learning_store_file_is_byte_unchanged(tmp_path: Path) -> None:
    db = tmp_path / "learning.sqlite"
    _seed_procedure(db, steps=("keep me",))
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    items = procedural_items(db, {"project_id": "p", "repository": "repo"})

    assert len(items) == 1
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


# ---------------------------------------------------------------------------
# integration with compile_bundle (withdrawn procedure / stale receipt)
# ---------------------------------------------------------------------------


def test_withdrawn_procedure_is_excluded_and_absent_from_text(tmp_path: Path) -> None:
    db = tmp_path / "learning.sqlite"
    _seed_procedure(db, steps=("retired guidance",), status="withdrawn")

    procedural = procedural_items(db, {"project_id": "p", "repository": "repo"})
    assert procedural[0].status == "withdrawn"

    view = _view()
    exact = exact_items(view)
    identity = identity_from_view(view, "implement", "attempt-3")
    bundle = compile_bundle(
        CompileRequest(
            identity=identity,
            token_budget=100_000,
            encoding="utf-8",
            items=tuple(exact) + procedural,
        ),
        WordCounter(),
    )

    assert "retired guidance" not in bundle.text
    assert [exclusion.reason for exclusion in bundle.exclusions] == ["withdrawn"]


def test_stale_receipt_is_excluded_and_current_renders() -> None:
    work_id = uuid4()
    revision_id = uuid4()
    candidate_id = uuid4()
    current = _receipt(work_id=work_id, revision_id=revision_id, candidate_id=candidate_id)
    older = _receipt(work_id=work_id, revision_id=uuid4(), candidate_id=candidate_id)
    view = _view(
        work_id=work_id, revision_id=revision_id, candidate_id=candidate_id,
        receipts=(current, older),
    )

    bundle = compile_bundle(
        CompileRequest(
            identity=identity_from_view(view, "implement", "attempt-4"),
            token_budget=100_000,
            encoding="utf-8",
            items=exact_items(view),
        ),
        WordCounter(),
    )

    assert str(current.receipt_id) in bundle.text
    assert str(older.receipt_id) not in bundle.text
    assert [exclusion.reason for exclusion in bundle.exclusions] == ["stale"]
