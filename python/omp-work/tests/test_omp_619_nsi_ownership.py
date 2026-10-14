"""Pending ownership records must not be mistaken for E2/E3 runtime admission."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
OWNERSHIP = ROOT / "docs/programme/NSI-4-5-OWNERSHIP.md"
INVENTORY = ROOT / "docs/nsi-component-inventory.md"
DISPOSITION = "NSI-DISPOSITION: NSI-4=pending NSI-5=pending"
PROOF = (
    "Naming an owner or recording a deferral is not qualified runtime proof "
    "and does not admit E2 or E3."
)


def _read_record() -> tuple[dict[str, object], str]:
    text = OWNERSHIP.read_text()
    record = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    marker = next(line for line in text.splitlines() if line.startswith("NSI-DISPOSITION:"))
    return record, marker


def _assert_dispositions(record: dict[str, object], marker: str) -> None:
    assert record["phase"] in ("prepared", "recorded")
    dispositions = []
    for component in ("NSI-4", "NSI-5"):
        decision = record[component]
        assert isinstance(decision, dict)
        status = decision["status"]
        if record["phase"] == "prepared":
            assert status == "pending"
            for field in ("owner", "deferral", "date", "wording", "evidence"):
                assert decision[field] is None, f"{component} has an unapproved {field}"
            disposition = "pending"
        else:
            assert status in ("owner", "deferral")
            for field in ("date", "wording", "evidence"):
                assert isinstance(decision[field], str) and decision[field].strip()
            if status == "owner":
                assert isinstance(decision["owner"], str) and decision["owner"].strip()
                assert decision["deferral"] is None
                disposition = f"owner:{decision['owner']}"
            else:
                assert decision["owner"] is None
                assert isinstance(decision["deferral"], dict)
                for field in ("reason", "revisit"):
                    value = decision["deferral"][field]
                    assert isinstance(value, str) and value.strip()
                disposition = "deferral"
        dispositions.append(f"{component}={disposition}")
    assert marker == "NSI-DISPOSITION: " + " ".join(dispositions)


def test_ownership_phase_requires_matching_dispositions() -> None:
    text = OWNERSHIP.read_text()
    record, marker = _read_record()
    _assert_dispositions(record, marker)
    assert "pending|owner|deferral" in text
    assert "keys `reason` and `revisit`" in text
    for component, row, title in (
        ("NSI-4", "NSI4", "Gap-directed questioning and owner decision loop"),
        (
            "NSI-5",
            "NSI5",
            "Acceptance compilation, semantic ratification and readiness receipts",
        ),
    ):
        decision = record[component]
        assert isinstance(decision, dict)
        assert decision["row"] == row
        assert decision["title"] == title


def _recorded_copy(statuses: tuple[str, str]) -> tuple[dict[str, object], str]:
    record, _ = _read_record()
    record = deepcopy(record)
    record["phase"] = "recorded"
    dispositions = []
    for component, status in zip(("NSI-4", "NSI-5"), statuses, strict=True):
        decision = record[component]
        assert isinstance(decision, dict)
        decision.update(
            status=status,
            owner="Example owner" if status == "owner" else None,
            deferral={"reason": "Awaiting capacity", "revisit": "2026-11-01"}
            if status == "deferral" else None,
            date="2026-10-14",
            wording="Example decision for disposition validation",
            evidence="In-memory decision fixture",
        )
        disposition = "owner:Example owner" if status == "owner" else status
        dispositions.append(f"{component}={disposition}")
    return record, "NSI-DISPOSITION: " + " ".join(dispositions)


@pytest.mark.parametrize("statuses", [("owner", "owner"), ("deferral", "deferral"), ("owner", "deferral")])
def test_recorded_owner_and_deferral_decisions_pass(statuses: tuple[str, str]) -> None:
    record, marker = _recorded_copy(statuses)
    _assert_dispositions(record, marker)


def _prepared_copy() -> dict[str, object]:
    record, _ = _read_record()
    record = deepcopy(record)
    record["phase"] = "prepared"
    for component in ("NSI-4", "NSI-5"):
        decision = record[component]
        assert isinstance(decision, dict)
        decision.update(status="pending", owner=None, deferral=None, date=None, wording=None, evidence=None)
    return record


def test_recorded_phase_refuses_pending_decisions() -> None:
    record = _prepared_copy()
    record["phase"] = "recorded"
    with pytest.raises(AssertionError):
        _assert_dispositions(record, DISPOSITION)


@pytest.mark.parametrize("field,value", [
    ("status", "owner"),
    ("owner", "Unapproved owner"),
    ("deferral", {"reason": "Unapproved", "revisit": "2026-11-01"}),
    ("date", "2026-10-14"),
    ("wording", "Unapproved decision"),
    ("evidence", "Unapproved evidence"),
])
def test_prepared_phase_refuses_unapproved_decision_fields(field: str, value: object) -> None:
    record = _prepared_copy()
    decision = record["NSI-4"]
    assert isinstance(decision, dict)
    decision[field] = value
    with pytest.raises(AssertionError):
        _assert_dispositions(record, DISPOSITION)


@pytest.mark.parametrize("status,field,value", [
    ("owner", "owner", " "),
    ("owner", "deferral", {"reason": "Conflict", "revisit": "2026-11-01"}),
    ("deferral", "owner", "Conflicting owner"),
    ("deferral", "deferral", None),
    ("deferral", "deferral", {"reason": "", "revisit": "2026-11-01"}),
    ("deferral", "deferral", {"reason": "Awaiting capacity", "revisit": " "}),
    ("owner", "date", None),
    ("owner", "wording", ""),
    ("owner", "evidence", " "),
    ("deferral", "date", ""),
    ("deferral", "wording", None),
    ("deferral", "evidence", ""),
    ("owner", "status", "unknown"),
    ("owner", "status", "pending"),
])
def test_recorded_phase_refuses_incomplete_or_conflicting_decisions(
    status: str, field: str, value: object,
) -> None:
    record, marker = _recorded_copy((status, "owner"))
    decision = record["NSI-4"]
    assert isinstance(decision, dict)
    decision[field] = value
    with pytest.raises(AssertionError):
        _assert_dispositions(record, marker)


@pytest.mark.parametrize("statuses", [("owner", "owner"), ("deferral", "deferral")])
def test_recorded_phase_refuses_pending_marker(statuses: tuple[str, str]) -> None:
    record, _ = _recorded_copy(statuses)
    with pytest.raises(AssertionError):
        _assert_dispositions(record, DISPOSITION)


def test_pending_dispositions_preserve_runtime_admission_gates() -> None:
    text = OWNERSHIP.read_text()
    for sentence in (
        "OMP-266 bounded P12 is not NSI-4 and is not NSI-5.",
        "Missing NSI-4 owner or disposition refuses OMP-268 E2 admission.",
        "Missing NSI-5 owner or disposition leaves OMP-269 E3 admission unresolved.",
        "A deferral or a pending record is not runtime readiness and does not remove the E2/E3 admission gate.",
    ):
        assert sentence in text
    assert PROOF in text

    inventory = INVENTORY.read_text()
    assert DISPOSITION in inventory
    assert PROOF in inventory
    assert "have no qualified runtime proof recorded" in inventory
    assert "have no qualified owner or proof recorded" not in inventory
