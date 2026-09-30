"""OMP-426-s01: the NSI component inventory document must stay a real index.

Failure mode defended: `docs/nsi-component-inventory.md` rots silently when an
owner row is dropped, when an id's What/Test-status cell is emptied, or when a
cited test/code path is renamed or deleted. A consumer trusting the inventory
would then chase a path that no longer exists or a component with no status.
This parses the markdown table and asserts the six NSI ids are present, each
with non-empty What and Test status cells, that every cited .py path exists on
disk, and that the closing gap list is present.
"""

from __future__ import annotations

import re
from pathlib import Path

INVENTORY = Path(__file__).resolve().parents[3] / "docs" / "nsi-component-inventory.md"
HEADER = ("Component", "What it does", "Code paths", "Test files", "Test status")
NSI_IDS = ("OMP-266", "OMP-267", "OMP-268", "OMP-269", "OMP-270", "OMP-328")
PY_PATH = re.compile(r"(?<![\w/.-])(?:python|packages|docs)/[A-Za-z0-9_./-]+\.py")


def _table(text: str) -> list[tuple[str, ...]]:
    rows: list[tuple[str, ...]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = tuple(cell.strip() for cell in stripped.strip("|").split("|"))
        if cells and all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        rows.append(cells)
    return rows


def test_inventory_covers_every_nsi_id_with_content() -> None:
    rows = _table(INVENTORY.read_text())
    assert rows, "inventory table not found"
    assert rows[0] == HEADER, f"unexpected header row: {rows[0]}"

    by_id: dict[str, tuple[str, ...]] = {}
    for row in rows[1:]:
        assert len(row) == len(HEADER), f"row {row} has {len(row)} cells, expected {len(HEADER)}"
        assert row[0] not in by_id, f"duplicate inventory row for {row[0]}"
        by_id[row[0]] = row

    for nsi_id in NSI_IDS:
        assert nsi_id in by_id, f"missing inventory row for {nsi_id}"
        what, test_status = by_id[nsi_id][1], by_id[nsi_id][4]
        assert what, f"{nsi_id} has an empty What cell"
        assert test_status, f"{nsi_id} has an empty Test status cell"


def test_inventory_cited_python_paths_exist() -> None:
    cited: set[str] = set()
    for row in _table(INVENTORY.read_text()):
        for cell in row:
            cited.update(PY_PATH.findall(cell))
    assert cited, "inventory cites no python paths"
    missing = sorted(path for path in cited if not (INVENTORY.parents[1] / path).is_file())
    assert not missing, f"inventory cites non-existent paths: {missing}"


def test_inventory_names_the_omp_426_gaps() -> None:
    assert "Gaps for OMP-426" in INVENTORY.read_text()
