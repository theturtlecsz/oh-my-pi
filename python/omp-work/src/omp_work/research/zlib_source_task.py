"""Self-contained Python/zlib evaluation tasks over production source files (OMP-605-s07)."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "POOL_NODES",
    "SAMPLE_SIZE",
    "ZlibNode",
    "build_corpus",
    "create_harness_script",
    "create_seed_task",
    "production_files",
    "sample_production_files",
]

SAMPLE_SIZE = 40


@dataclass(frozen=True, slots=True)
class ZlibNode:
    """One candidate parameterization in the unit-cost zlib search pool."""

    id: str
    parent: str | None
    level: int
    mem_level: int
    strategy: str  # "default" or "filtered"
    cost: int = 1


# Unit-cost pool order:
# roots a(level1), b(level6), c(level9);
# a children a1(level6), a2(level9 filtered);
# a1 children a1x(level9), a1y(level9 filtered memLevel9).
# Others memLevel8/default strategy.
POOL_NODES: tuple[ZlibNode, ...] = (
    ZlibNode(id="a", parent=None, level=1, mem_level=8, strategy="default", cost=1),
    ZlibNode(id="b", parent=None, level=6, mem_level=8, strategy="default", cost=1),
    ZlibNode(id="c", parent=None, level=9, mem_level=8, strategy="default", cost=1),
    ZlibNode(id="a1", parent="a", level=6, mem_level=8, strategy="default", cost=1),
    ZlibNode(id="a2", parent="a", level=9, mem_level=8, strategy="filtered", cost=1),
    ZlibNode(id="a1x", parent="a1", level=9, mem_level=8, strategy="default", cost=1),
    ZlibNode(id="a1y", parent="a1", level=9, mem_level=9, strategy="filtered", cost=1),
)


def production_files(repo_root: Path | str) -> list[Path]:
    """Sorted production *.py under python/omp-work/src/omp_work."""
    root = Path(repo_root)
    if (root / "python" / "omp-work" / "src" / "omp_work").is_dir():
        base = root / "python" / "omp-work" / "src" / "omp_work"
    elif (root / "src" / "omp_work").is_dir():
        base = root / "src" / "omp_work"
    elif (root / "omp_work").is_dir():
        base = root / "omp_work"
    else:
        base = root
    return sorted(
        p
        for p in base.rglob("*.py")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.name.endswith(".pyc")
        and "egg-info" not in str(p)
    )


def sample_production_files(
    repo_root: Path | str,
    seed: int,
    count: int = SAMPLE_SIZE,
) -> list[Path]:
    """Take a seeded sample of 40 sorted production *.py files."""
    files = production_files(repo_root)
    k = min(len(files), count)
    rng = random.Random(seed)  # nosec B311 - reproducible sampling, not security
    return sorted(rng.sample(files, k))


def build_corpus(
    repo_root: Path | str,
    seed: int,
    count: int = SAMPLE_SIZE,
) -> tuple[bytes, dict[str, str], str]:
    """Sample files, read bytes, and compute file hashes and tree hash.

    Returns (corpus_bytes, file_hashes, tree_sha256).
    """
    sampled = sample_production_files(repo_root, seed, count)
    root = Path(repo_root)
    hashes: dict[str, str] = {}
    chunks: list[bytes] = []
    tree_hasher = hashlib.sha256()

    for path in sampled:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        data = path.read_bytes()
        file_hash = hashlib.sha256(data).hexdigest()
        hashes[rel] = file_hash
        chunks.append(data)

        tree_hasher.update(rel.encode("utf-8"))
        tree_hasher.update(b"\0")
        tree_hasher.update(len(data).to_bytes(8, "big"))
        tree_hasher.update(data)

    corpus_bytes = b"".join(chunks)
    tree_sha256 = tree_hasher.hexdigest()
    return corpus_bytes, hashes, tree_sha256


def create_harness_script(
    node: ZlibNode,
    raw_bytes: bytes,
) -> str:
    """Generate self-contained Python/zlib bash harness with embedded frozen bytes."""
    b64_str = base64.b64encode(raw_bytes).decode("ascii")
    wrapped = "\n".join(b64_str[i : i + 76] for i in range(0, len(b64_str), 76))
    strat = "zlib.Z_FILTERED" if node.strategy == "filtered" else "zlib.Z_DEFAULT_STRATEGY"
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "python3 - <<'EOF'\n"
        "import base64\n"
        "import zlib\n"
        "\n"
        "RAW = base64.b64decode('''\n"
        + wrapped
        + "\n''')\n"
        + f"comp = zlib.compressobj({node.level}, zlib.DEFLATED, 15, {node.mem_level}, {strat})\n"
        + "out = comp.compress(RAW) + comp.flush()\n"
        + "ratio = len(RAW) / len(out)\n"
        + "print(f'METRIC ratio={ratio}')\n"
        + "EOF\n"
    )


def create_seed_task(
    repo_root: Path | str,
    seed: int,
    out_dir: Path | str,
    nodes: Sequence[ZlibNode] = POOL_NODES,
) -> tuple[Path, Path, dict[str, str], str]:
    """Write harnesses and definitions for seed into out_dir.

    Returns (greedy_def_path, fixed_def_path, corpus_hashes, corpus_tree_sha256).
    """
    task_dir = Path(out_dir) / f"seed_{seed}"
    harness_dir = task_dir / "harnesses"
    harness_dir.mkdir(parents=True, exist_ok=True)

    corpus_bytes, hashes, tree_sha256 = build_corpus(repo_root, seed)

    trials: list[dict[str, Any]] = []
    for node in nodes:
        script_path = harness_dir / f"{node.id}.sh"
        script_path.write_text(create_harness_script(node, corpus_bytes), encoding="utf-8")
        script_path.chmod(0o755)

        trial_dict: dict[str, Any] = {
            "id": node.id,
            "cost": node.cost,
            "harness": str(script_path.relative_to(task_dir).as_posix()),
        }
        if node.parent is not None:
            trial_dict["parent"] = node.parent
        trials.append(trial_dict)

    base_def = {
        "objective": "Compare adaptive and fixed search on zlib source compression",
        "domain": "engineering",
        "timeout_seconds": 60,
        "criteria": [{"metric": "ratio", "op": "gte", "threshold": 1.0}],
        "trials": trials,
    }

    greedy_def = {
        **base_def,
        "search": {
            "policy": "greedy",
            "budget": 5,
            "width": 1,
            "seed": seed,
            "metric": "ratio",
        },
    }
    fixed_def = {
        **base_def,
        "search": {
            "policy": "fixed",
            "budget": 5,
            "width": 1,
            "seed": seed,
            "metric": "ratio",
        },
    }

    greedy_path = task_dir / "greedy_definition.json"
    fixed_path = task_dir / "fixed_definition.json"
    greedy_path.write_text(json.dumps(greedy_def, indent=2), encoding="utf-8")
    fixed_path.write_text(json.dumps(fixed_def, indent=2), encoding="utf-8")

    return greedy_path, fixed_path, hashes, tree_sha256
