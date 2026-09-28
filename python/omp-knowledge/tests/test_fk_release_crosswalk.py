"""Contract test for the Fleet Knowledge release criteria crosswalk (OMP-278-s05).

Validates that docs/fleet-knowledge-release.md contains the 12 release criteria
titles verbatim in order, references real AST function definitions for each required
file, and specifies exact owner IDs.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

DOC_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "fleet-knowledge-release.md"
)
REPO_ROOT = Path(__file__).resolve().parents[3]

EXPECTED_TITLES: list[str] = [
    "Exact task capture and retained source",
    "Attributed proposal and native acceptance",
    "Fresh-task reuse with recorded outcome",
    "Counter-evidence, narrowing, and retraction",
    "Two actual repositories verified",
    "Candidate A/B real Cognee isolation",
    "Repository identity and scope stability",
    "Context budget and dispatch revalidation",
    "Service resilience and conflict handling",
    "Backup, rebuild, and rollback demonstrated",
    "Qualified RTX 5090 roles and explicit routes",
    "Native audit and delivery gates",
]

REQUIRED_FILES_PER_ROW: list[list[str]] = [
    [
        "python/omp-knowledge/tests/test_learning_record.py",
        "python/omp-knowledge/tests/test_learning_capture_execute.py",
        "python/omp-knowledge/tests/test_source_import.py",
    ],
    [
        "python/omp-knowledge/tests/test_learning_proposals.py",
        "python/omp-knowledge/tests/test_learning_capture_execute.py",
    ],
    [
        "python/omp-knowledge/tests/test_learning_uses.py",
    ],
    [
        "python/omp-knowledge/tests/test_learning_corrections.py",
        "python/omp-knowledge/tests/test_fk7_failure_modes.py",
    ],
    [
        "python/omp-knowledge/tests/test_fk7_journey.py",
    ],
    [
        "python/omp-knowledge/tests/test_snapshot_isolation.py",
        "python/omp-knowledge/tests/test_vector_projection.py",
    ],
    [
        "python/omp-work/tests/test_knowledge_source.py",
        "python/omp-knowledge/tests/test_context_sources.py",
    ],
    [
        "python/omp-knowledge/tests/test_context_compiler.py",
        "python/omp-knowledge/tests/test_context_sources.py",
        "python/omp-knowledge/tests/test_context_routes.py",
    ],
    [
        "python/omp-knowledge/tests/test_error_taxonomy.py",
        "python/omp-knowledge/tests/test_fk7_failure_modes.py",
        "python/omp-knowledge/tests/test_inference_routes.py",
    ],
    [
        "python/omp-knowledge/tests/test_maintenance_backup.py",
        "python/omp-knowledge/tests/test_vector_projection.py",
        "python/omp-knowledge/tests/test_context_routes.py",
    ],
    [
        "python/omp-knowledge/tests/test_inference_routes.py",
        "python/omp-knowledge/tests/test_inference_embedding.py",
    ],
    [],  # 12: `none`
]

EXPECTED_OWNER_IDS: list[str] = [
    "OMP-278-s07",
    "-",
    "OMP-278-s07",
    "OMP-278-s07",
    "OMP-312-s08, OMP-278-s06, OMP-278-s07",
    "-",
    "-",
    "OMP-278-s06",
    "OMP-278-s06",
    "OMP-312-s08, OMP-278-s06",
    "OMP-278-s06",
    "OMP-278-s08",
]

EXPECTED_OWNER_IDS_SHORTHAND: list[str] = [
    "s07",
    "-",
    "s07",
    "s07",
    "OMP-312-s08, s06, s07",
    "-",
    "-",
    "s06",
    "s06",
    "OMP-312-s08, s06",
    "s06",
    "s08",
]


class CrosswalkRow(NamedTuple):
    criterion: str
    automated_raw: str
    owner_ids: str
    test_refs: list[tuple[str, str]]


def _parse_crosswalk_table() -> list[CrosswalkRow]:
    assert DOC_PATH.is_file(), f"Crosswalk document missing at {DOC_PATH}"
    content = DOC_PATH.read_text(encoding="utf-8")

    lines = [line.strip() for line in content.splitlines()]
    table_lines: list[str] = []
    for line in lines:
        if line.startswith("|") and line.endswith("|"):
            table_lines.append(line)
        elif table_lines:
            break

    assert len(table_lines) >= 14, f"Expected header, separator, and 12 rows; got {len(table_lines)}"
    test_ref_pattern = re.compile(r"([a-zA-Z0-9_/.-]+\.py)::([a-zA-Z0-9_]+)")

    rows: list[CrosswalkRow] = []
    for line in table_lines[2:]:
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) < 3:
            continue
        criterion, automated, owner_ids = cells[0], cells[1], cells[2]
        test_refs = test_ref_pattern.findall(automated)
        rows.append(CrosswalkRow(criterion, automated, owner_ids, test_refs))

    return rows


def test_titles_verbatim_and_in_order() -> None:
    """All 12 criteria titles must match the required specification verbatim in order."""
    rows = _parse_crosswalk_table()
    assert len(rows) == 12, f"Expected 12 criteria rows, found {len(rows)}"

    for idx, (row, expected_title) in enumerate(zip(rows, EXPECTED_TITLES)):
        assert row.criterion == expected_title, (
            f"Row {idx + 1} title mismatch:\n"
            f"Expected: {expected_title!r}\n"
            f"Actual:   {row.criterion!r}"
        )


def test_each_path_test_is_a_def_via_ast() -> None:
    """Every path::name test cited in the table must be a function definition in AST."""
    rows = _parse_crosswalk_table()
    ast_cache: dict[Path, set[str]] = {}

    total_citations = 0
    for idx, row in enumerate(rows):
        for path_str, fn_name in row.test_refs:
            total_citations += 1
            file_path = REPO_ROOT / path_str
            assert file_path.is_file(), (
                f"Row {idx + 1} ({row.criterion}) cites nonexistent test file: {path_str}"
            )

            if file_path not in ast_cache:
                tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
                defs = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                ast_cache[file_path] = defs

            assert fn_name in ast_cache[file_path], (
                f"Row {idx + 1} ({row.criterion}) cites test {fn_name!r} which is not a def in {path_str}"
            )

    assert total_citations >= 20, f"Expected at least 20 test citations across rows, found {total_citations}"


def test_each_required_file_cited() -> None:
    """Every required file for each criterion row must be cited in its automated column."""
    rows = _parse_crosswalk_table()
    assert len(rows) == len(REQUIRED_FILES_PER_ROW)

    for idx, (row, required_files) in enumerate(zip(rows, REQUIRED_FILES_PER_ROW)):
        if not required_files:
            # Row 12 (native audit and delivery gates) requires none
            assert row.automated_raw.strip(" `").lower() == "none", (
                f"Row {idx + 1} ({row.criterion}) expected 'none' for automated tests, got: {row.automated_raw!r}"
            )
            assert len(row.test_refs) == 0, f"Row {idx + 1} expected no test citations, found {row.test_refs}"
            continue

        cited_files = {path for path, _ in row.test_refs}
        for req_file in required_files:
            assert req_file in cited_files, (
                f"Row {idx + 1} ({row.criterion}) missing citation for required file {req_file!r}. "
                f"Cited files: {sorted(cited_files)}"
            )


def test_owner_ids_exact() -> None:
    """Owner IDs for all criteria rows must match the expected specification."""
    rows = _parse_crosswalk_table()
    assert len(rows) == len(EXPECTED_OWNER_IDS)

    for idx, (row, expected_owner, shorthand_owner) in enumerate(
        zip(rows, EXPECTED_OWNER_IDS, EXPECTED_OWNER_IDS_SHORTHAND)
    ):
        assert row.owner_ids in (expected_owner, shorthand_owner), (
            f"Row {idx + 1} ({row.criterion}) owner IDs mismatch:\n"
            f"Expected: {expected_owner!r} (or {shorthand_owner!r})\n"
            f"Actual:   {row.owner_ids!r}"
        )
