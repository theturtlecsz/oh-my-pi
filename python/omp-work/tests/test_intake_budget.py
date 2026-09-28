"""OMP-404-s01: Intake budget validation and publication gate.

Pins the observable contract:
- ItemBudget rejects missing, zero, and negative fields (usd > 0, tokens > 0, wall_clock_seconds > 0, max_subagents >= 0).
- bounded_intake_semantic_sha256 omits budget when None (hash is unchanged for budget-less drafts).
- Adding a budget changes the semantic hash.
- With OMP_WORK_POSTGRES_INTEGRATION=1:
  - Publishing without a budget is refused 409 intake_not_ready ("budget:missing",) before any write;
    zero items, relations, candidates, or receipts are created.
  - Publishing with a budget succeeds and stores receipt payload.draft.budget equal to input.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from pydantic import ValidationError

from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256
from omp_work.v1.models import BoundedIntakeDraft, ItemBudget
from omp_work.v1.semantics import bounded_intake_semantic_sha256
from omp_work.v1.service import Principal, WorkError, WorkService
from omp_work.v1.store import PostgresWorkStore
from pg_native import native_postgres

# Ensure sibling test helpers are importable regardless of test invocation cwd/pythonpath
_TESTS_DIR = str(Path(__file__).parent.resolve())
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_bounded_intake_publish import (
    OWNER,
    OWNER_SCOPES,
    _config,
    _draft,
    _envelope,
    _row_counts,
    _seed_omp249,
)


@pytest.mark.parametrize(
    "missing_field",
    ["usd", "tokens", "wall_clock_seconds", "max_subagents"],
)
def test_item_budget_missing_fields_rejected(missing_field: str) -> None:
    data = {
        "usd": "50.00",
        "tokens": 500_000,
        "wall_clock_seconds": 3600,
        "max_subagents": 2,
    }
    del data[missing_field]
    with pytest.raises(ValidationError):
        ItemBudget.model_validate(data)


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"usd": "0"},
        {"usd": "0.0"},
        {"usd": "0.00"},
        {"tokens": 0},
        {"wall_clock_seconds": 0},
    ],
)
def test_item_budget_zero_fields_rejected(bad_kwargs: dict[str, object]) -> None:
    kwargs = {
        "usd": "50.00",
        "tokens": 500_000,
        "wall_clock_seconds": 3600,
        "max_subagents": 2,
        **bad_kwargs,
    }
    with pytest.raises(ValidationError):
        ItemBudget.model_validate(kwargs)


def test_item_budget_zero_max_subagents_allowed() -> None:
    budget = ItemBudget(
        usd="50.00",
        tokens=500_000,
        wall_clock_seconds=3600,
        max_subagents=0,
    )
    assert budget.max_subagents == 0


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"usd": "-1.00"},
        {"tokens": -1},
        {"wall_clock_seconds": -1},
        {"max_subagents": -1},
    ],
)
def test_item_budget_negative_fields_rejected(bad_kwargs: dict[str, object]) -> None:
    kwargs = {
        "usd": "50.00",
        "tokens": 500_000,
        "wall_clock_seconds": 3600,
        "max_subagents": 2,
        **bad_kwargs,
    }
    with pytest.raises(ValidationError):
        ItemBudget.model_validate(kwargs)


@pytest.mark.parametrize(
    "bad_kwargs",
    [
        {"usd": "not-a-number"},
        {"usd": "NaN"},
        {"usd": "inf"},
        {"usd": "-inf"},
        {"usd": True},
        {"tokens": True},
        {"tokens": "500000"},
        {"wall_clock_seconds": True},
        {"max_subagents": True},
    ],
)
def test_item_budget_malformed_values_rejected(bad_kwargs: dict[str, object]) -> None:
    kwargs = {
        "usd": "50.00",
        "tokens": 500_000,
        "wall_clock_seconds": 3600,
        "max_subagents": 2,
        **bad_kwargs,
    }
    with pytest.raises(ValidationError):
        ItemBudget.model_validate(kwargs)


def test_budget_less_semantic_hash_unchanged() -> None:
    draft_no_budget = _draft().model_copy(update={"budget": None})
    raw = draft_no_budget.model_dump(mode="json")
    assert "budget" in raw and raw["budget"] is None

    computed_hash = bounded_intake_semantic_sha256(draft_no_budget)

    # Pre-OMP-404 draft JSON had no "budget" key at all
    del raw["budget"]
    expected_hash = sha256(
        {
            "contract": "work.omp.dev/v1/bounded-intake",
            "draft": raw,
        }
    )
    assert computed_hash == expected_hash


def test_budget_changes_semantic_hash() -> None:
    draft_with_budget = _draft()
    assert draft_with_budget.budget is not None
    draft_no_budget = draft_with_budget.model_copy(update={"budget": None})

    assert bounded_intake_semantic_sha256(
        draft_with_budget
    ) != bounded_intake_semantic_sha256(draft_no_budget)


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_publish_without_budget_refused_no_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)

        draft_no_budget = _draft().model_copy(update={"budget": None})
        assert draft_no_budget.budget is None

        seeded = _seed_omp249(config, uuid4(), draft_no_budget)
        store = PostgresWorkStore(config)
        service = WorkService(store)
        owner = Principal(
            actor_id=OWNER,
            actor_kind="owner",
            workspaces=frozenset({seeded.workspace_id}),
            scopes=OWNER_SCOPES,
        )

        def execute(
            command: dict[str, object],
            *,
            operation_id: UUID | None = None,
        ) -> tuple[object, dict[str, object]]:
            return service.execute(
                owner,
                _envelope(seeded.workspace_id, command, operation_id=operation_id),
            )

        # Assess draft without budget (assess does not require a budget)
        assessment_operation_id = uuid4()
        _, assessment = execute(
            {
                "type": "assess_bounded_intake",
                "payload": {"draft": draft_no_budget.model_dump(mode="json")},
            },
            operation_id=assessment_operation_id,
        )
        assert assessment["ready_for_ratification"] is True

        # Native admission receipt
        _, admission = execute(
            {"type": "attest_intake_admission", "payload": seeded.attest_payload()},
        )
        admission_receipt_id = admission["receipt"]["receipt_id"]

        # Record rows before publish attempt
        before_counts = _row_counts(config, seeded.workspace_id)

        # Publish command without budget
        publish_command = {
            "type": "publish_bounded_intake",
            "payload": {
                "draft": draft_no_budget.model_dump(mode="json"),
                "assessment_operation_id": str(assessment_operation_id),
                "ratified_semantic_sha256": assessment["semantic_sha256"],
                "admission_work_id": str(seeded.work_id),
                "admission_revision_id": str(seeded.revision_id),
                "admission_receipt_id": admission_receipt_id,
            },
        }

        with pytest.raises(WorkError) as exc_info:
            execute(publish_command)

        assert exc_info.value.status == 409
        assert exc_info.value.code == "intake_not_ready"
        assert exc_info.value.diagnostics == ("budget:missing",)

        # Assert no item, relation, candidate, or publication receipt was created
        after_counts = _row_counts(config, seeded.workspace_id)
        assert after_counts == before_counts


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)
def test_publish_with_budget_succeeds_and_persists_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    expected_budget = ItemBudget(
        usd="75.50",
        tokens=250_000,
        wall_clock_seconds=1800,
        max_subagents=3,
    )
    draft = _draft().model_copy(update={"budget": expected_budget})
    assert draft.budget == expected_budget

    with native_postgres(tmp_path, config.port):
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        bootstrap(config)
        seeded = _seed_omp249(config, uuid4(), draft)
        store = PostgresWorkStore(config)
        service = WorkService(store)
        owner = Principal(
            actor_id=OWNER,
            actor_kind="owner",
            workspaces=frozenset({seeded.workspace_id}),
            scopes=OWNER_SCOPES,
        )

        def execute(
            command: dict[str, object],
            *,
            operation_id: UUID | None = None,
        ) -> tuple[object, dict[str, object]]:
            return service.execute(
                owner,
                _envelope(seeded.workspace_id, command, operation_id=operation_id),
            )

        assessment_operation_id = uuid4()
        _, assessment = execute(
            {
                "type": "assess_bounded_intake",
                "payload": {"draft": draft.model_dump(mode="json")},
            },
            operation_id=assessment_operation_id,
        )
        assert assessment["ready_for_ratification"] is True

        _, admission = execute(
            {"type": "attest_intake_admission", "payload": seeded.attest_payload()},
        )
        admission_receipt_id = admission["receipt"]["receipt_id"]

        publish_command = {
            "type": "publish_bounded_intake",
            "payload": {
                "draft": draft.model_dump(mode="json"),
                "assessment_operation_id": str(assessment_operation_id),
                "ratified_semantic_sha256": assessment["semantic_sha256"],
                "admission_work_id": str(seeded.work_id),
                "admission_revision_id": str(seeded.revision_id),
                "admission_receipt_id": admission_receipt_id,
            },
        }

        receipt, result = execute(publish_command)
        assert receipt.state.value == "applied"
        assert result["type"] == "publish_bounded_intake"

        minted_receipt = result["receipt"]
        assert minted_receipt["kind"] == "intake_publication"
        assert (
            minted_receipt["payload"]["draft"]["budget"]
            == expected_budget.model_dump(mode="json")
        )

        # Verify persisted receipt in Postgres
        with (
            psycopg.connect(
                **config.connection_kwargs("postgres"), row_factory=dict_row
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute(
                "SELECT payload FROM omp_evidence.receipts WHERE receipt_id=%s",
                (UUID(minted_receipt["receipt_id"]),),
            )
            row = cur.fetchone()
            assert row is not None
            assert (
                row["payload"]["draft"]["budget"]
                == expected_budget.model_dump(mode="json")
            )
