"""Pending ownership records must not be mistaken for E2/E3 runtime admission."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OWNERSHIP = ROOT / "docs/programme/NSI-4-5-OWNERSHIP.md"
INVENTORY = ROOT / "docs/nsi-component-inventory.md"
DISPOSITION = "NSI-DISPOSITION: NSI-4=pending NSI-5=pending"
PROOF = (
    "Naming an owner or recording a deferral is not qualified runtime proof "
    "and does not admit E2 or E3."
)


def test_prepared_ownership_leaves_both_decisions_pending() -> None:
    text = OWNERSHIP.read_text()
    record = json.loads(text.split("```json\n", 1)[1].split("```", 1)[0])
    assert record["phase"] == "prepared"
    assert DISPOSITION in text
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
        assert decision["row"] == row
        assert decision["title"] == title
        assert decision["status"] == "pending"
        for field in ("owner", "deferral", "date", "wording", "evidence"):
            assert decision[field] is None, f"{component} has an unapproved {field}"


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
