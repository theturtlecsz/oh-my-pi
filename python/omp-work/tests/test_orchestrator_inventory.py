"""OMP-417-s05: the orchestrator control-plane inventory stays a real index.

The authorities table names each authority once, and every module it cites
exists and is one of the control-plane modules that decide admission,
scheduling, leases, routing, recovery, cancellation, budgets, or progress.
The feature map lists the six parallel_streams features. The consumer table
lists the parallel-admit CLI, its tests, and the docs that still mention it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INVENTORY = ROOT / "docs" / "orchestrator-control-plane.md"
PY_PATH = re.compile(r"python/omp-work/src/omp_work/[A-Za-z0-9_./-]+\.py")
AUTHORITIES = (
    "fleet admission",
    "scheduling",
    "leases",
    "routing",
    "retry and recovery",
    "cancellation",
    "budgets",
    "progress events",
)
FEATURES = (
    "in_flight_max lock",
    "heartbeat",
    "claim",
    "path leases",
    "budget reserve",
    "provider partitions",
)
_PREFIX = "python/omp-work/src/omp_work/"
_EXACT = {
    "orchestrator/stages.py",
    "orchestrator/step_log.py",
    "routing/policy.py",
    "control_actions.py",
    "project_store.py",
}


def _sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## ", text, flags=re.M)
    found: dict[str, str] = {}
    for part in parts[1:]:
        title, _, body = part.partition("\n")
        found[title.strip()] = body
    return found


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


def _allowed(path: str) -> bool:
    if not path.startswith(_PREFIX) or not path.endswith(".py"):
        return False
    rel = path[len(_PREFIX) :]
    if rel.startswith("jobs/") or rel.startswith("control_plane/"):
        return True
    return rel in _EXACT


def test_authorities_are_unique_and_name_existing_modules() -> None:
    sections = _sections(INVENTORY.read_text())
    rows = _table(sections["Authorities"])
    assert rows[0] == ("Authority", "Modules")
    seen: dict[str, tuple[str, ...]] = {}
    for row in rows[1:]:
        assert len(row) == 2, row
        assert row[0] not in seen, f"duplicate authority {row[0]}"
        seen[row[0]] = row
    assert tuple(seen) == AUTHORITIES

    cited: set[str] = set()
    for row in seen.values():
        paths = PY_PATH.findall(row[1])
        assert paths, f"{row[0]} names no module"
        for path in paths:
            assert _allowed(path), f"{row[0]} names {path}, which is not a control-plane module"
            assert (ROOT / path).is_file(), path
            cited.add(path)
    assert cited


def test_parallel_streams_features_and_consumers_are_present() -> None:
    sections = _sections(INVENTORY.read_text())
    features = _table(sections["Parallel streams features"])
    assert features[0] == ("Feature", "Component")
    by_feature: dict[str, str] = {}
    for row in features[1:]:
        assert len(row) == 2, row
        assert row[0] not in by_feature, f"duplicate feature {row[0]}"
        by_feature[row[0]] = row[1]
    assert tuple(by_feature) == FEATURES
    for name, component in by_feature.items():
        if component == "none":
            continue
        paths = PY_PATH.findall(component)
        assert paths, f"{name} component is neither none nor a module path"
        for path in paths:
            assert (ROOT / path).is_file(), path

    consumers = _table(sections["Parallel streams consumers"])
    assert consumers[0] == ("Consumer", "Path", "Migrated")
    paths = [row[1] for row in consumers[1:]]
    migrated = [row[2] for row in consumers[1:]]
    assert any("__main__.py" in cell for cell in paths)
    assert any("test_parallel_streams" in cell for cell in paths)
    assert any("docs/" in cell for cell in paths)
    assert migrated
    assert set(migrated) <= {"yes", "no"}
