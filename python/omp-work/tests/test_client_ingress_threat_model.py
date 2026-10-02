"""OMP-490-s01: client ingress threat model table tests against client_contract.

Failure mode defended:
`docs/runbooks/client-ingress-threat-model.md` defines the threat model and edge
exposure boundaries for the external client ingress. If the runbook's operation
table falls out of sync with contract.json (operations added, removed, or changed
in method, path, or scope), or if the edge exposure allowlist is modified without
updating the threat model, deployments or audits will rely on an inaccurate or
stale threat model. This test enforces that the runbook's table matches exactly
the 17 contract operations in method, path, and scope, that exactly the 8 specified
operations are allowed at the edge, and that any omitted contract operation causes
the test to fail.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from omp_work import load_contract
from omp_work.v1.models import ClientOperation

THREAT_MODEL_PATH = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "runbooks"
    / "client-ingress-threat-model.md"
)

EXPECTED_HEADER = ("Name", "Method", "Path", "Scope", "Principal", "Edge")

EXPECTED_ALLOW = frozenset(
    {
        "project.list",
        "project.context",
        "project.status",
        "project.decisions",
        "mission.status",
        "stop.status",
        "mission.pause",
        "stop.engage",
    }
)


def parse_threat_model_table(markdown_text: str) -> list[dict[str, str]]:
    """Parse the operation markdown table from the threat model runbook."""
    rows: list[list[str]] = []
    for line in markdown_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)

    if not rows:
        raise AssertionError("No markdown table found in threat model document")

    header = tuple(rows[0])
    if header != EXPECTED_HEADER:
        raise AssertionError(
            f"Unexpected table header: {header}, expected: {EXPECTED_HEADER}"
        )

    table: list[dict[str, str]] = []
    for row in rows[1:]:
        if len(row) != len(header):
            raise AssertionError(f"Row cell count mismatch: {row}")
        table.append(dict(zip(header, row)))
    return table


def verify_threat_model_operations(
    table_rows: list[dict[str, str]],
    contract_operations: Sequence[ClientOperation],
) -> None:
    """Verify that table rows match contract operations and expected edge rules."""
    by_name = {row["Name"]: row for row in table_rows}
    if len(by_name) != len(table_rows):
        raise AssertionError("Duplicate operation name found in threat model table")

    for op in contract_operations:
        if op.name not in by_name:
            raise AssertionError(
                f"missing operation from threat model table: {op.name}"
            )
        row = by_name[op.name]

        if row["Method"] != op.method:
            raise AssertionError(
                f"Method mismatch for {op.name}: table={row['Method']}, contract={op.method}"
            )
        if row["Path"] != op.path:
            raise AssertionError(
                f"Path mismatch for {op.name}: table={row['Path']}, contract={op.path}"
            )

        row_scopes = tuple(s.strip() for s in row["Scope"].split(","))
        if row_scopes != tuple(op.scope):
            raise AssertionError(
                f"Scope mismatch for {op.name}: table={row_scopes}, contract={tuple(op.scope)}"
            )

        expected_edge = "allow" if op.name in EXPECTED_ALLOW else "refuse"
        if row["Edge"] != expected_edge:
            raise AssertionError(
                f"Edge mismatch for {op.name}: table={row['Edge']}, expected={expected_edge}"
            )

    if len(table_rows) != len(contract_operations):
        extra = set(by_name) - {op.name for op in contract_operations}
        raise AssertionError(
            f"Expected {len(contract_operations)} table rows, got {len(table_rows)}. Extra operations: {extra}"
        )


def test_threat_model_file_exists() -> None:
    assert THREAT_MODEL_PATH.is_file(), (
        f"Missing threat model file at {THREAT_MODEL_PATH}"
    )


def test_threat_model_table_matches_contract_operations() -> None:
    content = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    table_rows = parse_threat_model_table(content)

    contract = load_contract()
    contract_ops = contract.client_contract.operations

    assert len(contract_ops) == 17
    assert len(table_rows) == 17

    verify_threat_model_operations(table_rows, contract_ops)

    allow_rows = [row["Name"] for row in table_rows if row["Edge"] == "allow"]
    assert set(allow_rows) == EXPECTED_ALLOW
    assert len(allow_rows) == 8

    refuse_rows = [row["Name"] for row in table_rows if row["Edge"] == "refuse"]
    assert len(refuse_rows) == 9


def test_threat_model_fails_if_operation_missing() -> None:
    content = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    table_rows = parse_threat_model_table(content)

    contract = load_contract()
    contract_ops = contract.client_contract.operations

    for op in contract_ops:
        incomplete_rows = [row for row in table_rows if row["Name"] != op.name]
        with pytest.raises(
            AssertionError,
            match=f"missing operation from threat model table: {op.name}",
        ):
            verify_threat_model_operations(incomplete_rows, contract_ops)


def test_threat_model_fails_if_edge_status_inverted() -> None:
    content = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    table_rows = parse_threat_model_table(content)

    contract = load_contract()
    contract_ops = contract.client_contract.operations

    tampered_allow = [
        dict(row, Edge="refuse" if row["Name"] == "project.list" else row["Edge"])
        for row in table_rows
    ]
    with pytest.raises(AssertionError, match="Edge mismatch for project.list"):
        verify_threat_model_operations(tampered_allow, contract_ops)

    tampered_refuse = [
        dict(row, Edge="allow" if row["Name"] == "mission.submit" else row["Edge"])
        for row in table_rows
    ]
    with pytest.raises(AssertionError, match="Edge mismatch for mission.submit"):
        verify_threat_model_operations(tampered_refuse, contract_ops)


def test_threat_model_fails_if_contract_attributes_differ() -> None:
    content = THREAT_MODEL_PATH.read_text(encoding="utf-8")
    table_rows = parse_threat_model_table(content)

    contract = load_contract()
    contract_ops = contract.client_contract.operations

    tampered_method = [
        dict(row, Method="POST" if row["Name"] == "project.list" else row["Method"])
        for row in table_rows
    ]
    with pytest.raises(AssertionError, match="Method mismatch for project.list"):
        verify_threat_model_operations(tampered_method, contract_ops)

    tampered_scope = [
        dict(row, Scope="work.stop" if row["Name"] == "project.list" else row["Scope"])
        for row in table_rows
    ]
    with pytest.raises(AssertionError, match="Scope mismatch for project.list"):
        verify_threat_model_operations(tampered_scope, contract_ops)
