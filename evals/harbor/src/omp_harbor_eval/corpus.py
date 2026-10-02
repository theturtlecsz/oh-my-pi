"""Harbor corpus: manifest, freeze, verify and categories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .evidence import canonical_json
from .fixtures import load_fixture

_parents = Path(__file__).resolve().parents
REPO_ROOT = _parents[4] if len(_parents) > 4 else Path("/")
EVALS_HARBOR_ROOT = _parents[2] if len(_parents) > 2 else Path("/evals/harbor")

CATEGORIES: tuple[str, ...] = (
    "localized_bug",
    "ambiguous_intake",
    "multi_file_feature",
    "migration",
    "difficult_debugging",
    "resume",
    "security_concurrency",
    "seeded_defect",
)
_CATEGORIES_SET = frozenset(CATEGORIES)
_REQUIRED_TASK_KEYS = frozenset({"task_id", "category", "fixture", "source_commit", "rationale"})


class Batch(list[dict[str, Any]]):
    """List of task entries that also supports .tasks and ['tasks']."""

    @property
    def tasks(self) -> list[dict[str, Any]]:
        return list(self)

    def __getitem__(self, item: Any) -> Any:
        if item == "tasks":
            return list(self)
        return super().__getitem__(item)

    def get(self, key: str, default: Any = None) -> Any:
        if key == "tasks":
            return list(self)
        return default


def extract_patch_paths(patch_text: str) -> list[str]:
    """Extract sorted list of unique file paths modified by a unified diff."""
    seen: set[str] = set()
    has_diff_git = any(line.startswith("diff --git ") for line in patch_text.splitlines())
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line[len("diff --git ") :].strip().split()
            if len(parts) >= 2:
                for part in (parts[1], parts[0]):
                    target = part
                    if target.startswith("a/") or target.startswith("b/"):
                        target = target[2:]
                    if target != "/dev/null" and target not in seen:
                        seen.add(target)
                        break
        elif line.startswith("+++ "):
            part = line[4:].strip().split("\t")[0]
            if part.startswith("b/") or part.startswith("a/"):
                part = part[2:]
            if part != "/dev/null":
                seen.add(part)
        elif line.startswith("--- "):
            part = line[4:].strip().split("\t")[0]
            if part.startswith("a/") or part.startswith("b/"):
                part = part[2:]
            if part != "/dev/null" and ("+++ /dev/null" in patch_text or not has_diff_git):
                seen.add(part)
    return sorted(seen)


def _resolve_fixture_dir(fixture_str: str, task_id: str, base_path: Path | None = None) -> Path:
    p = Path(fixture_str)
    if p.is_absolute() and p.is_dir():
        return p.resolve()
    candidates: list[Path] = []
    if base_path is not None:
        candidates.append(base_path / fixture_str)
    candidates.extend([
        REPO_ROOT / fixture_str,
        EVALS_HARBOR_ROOT / fixture_str,
        Path.cwd() / fixture_str,
    ])
    for c in candidates:
        if c.is_dir():
            return c.resolve()
    if p.is_absolute():
        return p
    if base_path is not None:
        return (base_path / fixture_str).resolve()
    return (REPO_ROOT / fixture_str).resolve()


def task_sha256(directory: str | Path) -> str:
    """sha256 over files in directory in sorted relpath order.

    Each file contributes: sorted relpath, NUL, 8-byte BE length, bytes.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ValueError(f"not a directory: {directory}")
    files: list[tuple[str, Path]] = []
    for dirpath, _, filenames in os.walk(dir_path, followlinks=False):
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if path.is_file():
                relpath = path.relative_to(dir_path).as_posix()
                files.append((relpath, path))
    files.sort(key=lambda item: item[0])
    hasher = hashlib.sha256()
    for relpath, path in files:
        data = path.read_bytes()
        hasher.update(relpath.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def load_batch(path: str | Path) -> Batch:
    """Load a batch file and validate all task fixtures."""
    batch_file = Path(path)
    if not batch_file.is_file():
        raise ValueError(f"batch file not found: {batch_file}")
    try:
        document = json.loads(batch_file.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot read batch {batch_file.name}: {exc}") from exc

    if not isinstance(document, dict) or "tasks" not in document:
        raise ValueError("batch document must contain a 'tasks' list")
    tasks_raw = document["tasks"]
    if not isinstance(tasks_raw, list):
        raise ValueError("batch tasks must be a list")

    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(tasks_raw):
        if not isinstance(item, dict):
            raise ValueError(f"task at index {idx} must be a JSON object")
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError(f"task at index {idx} missing valid task_id")

        missing_keys = sorted(_REQUIRED_TASK_KEYS - set(item))
        if missing_keys:
            raise ValueError(f"task {task_id}: missing required keys: {missing_keys}")

        category = item["category"]
        if category not in _CATEGORIES_SET:
            raise ValueError(f"task {task_id}: invalid category {category!r}, must be one of {CATEGORIES}")

        source_commit = item["source_commit"]
        if not isinstance(source_commit, str) or not source_commit.strip():
            raise ValueError(f"task {task_id}: source_commit must be a non-empty string")

        rationale = item["rationale"]
        if not isinstance(rationale, str) or not rationale.strip():
            raise ValueError(f"task {task_id}: rationale must be a non-empty string")

        fixture_str = item["fixture"]
        if not isinstance(fixture_str, str) or not fixture_str.strip():
            raise ValueError(f"task {task_id}: fixture must be a non-empty string")

        fixture_dir = _resolve_fixture_dir(fixture_str, task_id, base_path=batch_file.parent)
        if not fixture_dir.is_dir():
            raise ValueError(f"task {task_id}: fixture directory does not exist: {fixture_dir}")

        if "evals/harbor" not in fixture_dir.as_posix():
            raise ValueError(f"task {task_id}: fixture {fixture_str} must be under evals/harbor")

        if fixture_dir.name != task_id:
            raise ValueError(f"task {task_id}: fixture directory {fixture_str} must end in task_id {task_id}")

        try:
            fixture_obj = load_fixture(fixture_dir.parent, task_id)
        except Exception as exc:
            raise ValueError(f"task {task_id}: load_fixture failed: {exc}") from exc

        seed_path = fixture_dir / fixture_obj.seed_patch
        solution_path = fixture_dir / fixture_obj.solution_patch
        if not seed_path.is_file() or not solution_path.is_file():
            raise ValueError(f"task {task_id}: seed or solution patch file missing")

        seed_bytes = seed_path.read_bytes()
        solution_bytes = solution_path.read_bytes()
        if not seed_bytes.strip():
            raise ValueError(f"task {task_id}: seed patch is empty")
        if not solution_bytes.strip():
            raise ValueError(f"task {task_id}: solution patch is empty")
        if seed_bytes == solution_bytes:
            raise ValueError(f"task {task_id}: seed and solution patches must differ")

        has_independent_rule = any(rule.get("type") == "independent_tests_pass" for rule in fixture_obj.rules)
        if not has_independent_rule:
            raise ValueError(f"task {task_id}: missing independent_tests_pass rule")

        if "corpus/tasks" in fixture_dir.as_posix():
            for test in fixture_obj.independent_tests:
                target_base = Path(test.target.split("::")[0]).name
                if not any(Path(f).name == target_base for f in test.files):
                    raise ValueError(
                        f"task {task_id}: test {test.target} missing files entry with target basename {target_base}"
                    )

        source_paths = extract_patch_paths(solution_bytes.decode("utf-8", errors="replace"))
        if not source_paths:
            raise ValueError(f"task {task_id}: solution patch changes no files")
        long_horizon = len(source_paths) >= 4

        entries.append({
            "task_id": task_id,
            "category": category,
            "fixture": fixture_str,
            "source_commit": source_commit,
            "rationale": rationale,
            "source_paths": source_paths,
            "long_horizon": long_horizon,
        })

    return Batch(entries)


def freeze(
    batches: Sequence[str | Path | Mapping[str, Any] | Sequence[Mapping[str, Any]]],
    base_commit: str,
) -> dict[str, Any]:
    """Freeze a corpus from batches and base commit into a signed manifest."""
    if not isinstance(base_commit, str) or not base_commit.strip():
        raise ValueError("base_commit must be a non-empty string")

    batch_identifiers: list[str] = []
    all_tasks: list[dict[str, Any]] = []

    for idx, batch in enumerate(batches):
        if isinstance(batch, (str, Path)):
            batch_identifiers.append(str(batch))
            loaded = load_batch(Path(batch))
            all_tasks.extend(loaded)
        elif isinstance(batch, Mapping) and "tasks" in batch:
            batch_identifiers.append(str(batch.get("name", f"batch_{idx}")))
            for item in batch["tasks"]:
                if isinstance(item, dict) and "source_paths" in item and "long_horizon" in item:
                    all_tasks.append(item)
                else:
                    raise ValueError("tasks in in-memory batch must be loaded task entries")
        elif isinstance(batch, (list, tuple)):
            batch_identifiers.append(f"batch_{idx}")
            all_tasks.extend(batch)
        else:
            raise ValueError(f"unsupported batch type: {type(batch).__name__}")

    tasks_by_id: dict[str, Any] = {}
    for task in all_tasks:
        tid = task["task_id"]
        if tid in tasks_by_id:
            raise ValueError(f"duplicate task_id: {tid}")
        tasks_by_id[tid] = task

    total = len(tasks_by_id)
    if total < 30 or total > 50:
        raise ValueError(f"corpus must have between 30 and 50 tasks, got {total}")

    seen_categories = {t["category"] for t in tasks_by_id.values()}
    missing_categories = sorted(_CATEGORIES_SET - seen_categories)
    if missing_categories:
        raise ValueError(f"corpus missing categories: {missing_categories}")

    long_horizon_count = sum(1 for t in tasks_by_id.values() if t["long_horizon"])
    if long_horizon_count < 3:
        raise ValueError(f"corpus must have at least 3 long_horizon tasks, got {long_horizon_count}")

    frozen_tasks: dict[str, Any] = {}
    for tid in sorted(tasks_by_id):
        t = tasks_by_id[tid]
        fixture_dir = _resolve_fixture_dir(t["fixture"], tid)
        sha = task_sha256(fixture_dir)
        frozen_tasks[tid] = {
            "category": t["category"],
            "fixture": t["fixture"],
            "long_horizon": bool(t["long_horizon"]),
            "source_paths": list(t["source_paths"]),
            "task_sha256": sha,
        }

    manifest_content = {
        "base_commit": base_commit,
        "batches": sorted(batch_identifiers),
        "tasks": frozen_tasks,
    }
    corpus_sha = hashlib.sha256(canonical_json(manifest_content)).hexdigest()
    return {
        "base_commit": base_commit,
        "batches": sorted(batch_identifiers),
        "corpus_sha256": corpus_sha,
        "tasks": frozen_tasks,
    }


def verify(manifest: str | Path | Mapping[str, Any]) -> list[str]:
    """Verify manifest against current files and return a list of drift descriptions."""
    manifest_base: Path | None = None
    if isinstance(manifest, (str, Path)):
        path = Path(manifest)
        manifest_base = path.parent
        if not path.is_file():
            return [f"manifest file missing: {path}"]
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [f"cannot read manifest: {exc}"]
    elif isinstance(manifest, Mapping):
        document = manifest
    else:
        raise ValueError(f"manifest must be a path or mapping, got {type(manifest).__name__}")

    drift: list[str] = []
    for req in ("base_commit", "batches", "tasks", "corpus_sha256"):
        if req not in document:
            drift.append(f"manifest missing required key: {req}")
    if drift:
        return drift

    manifest_body = {
        "base_commit": document["base_commit"],
        "batches": document["batches"],
        "tasks": document["tasks"],
    }
    computed_sha = hashlib.sha256(canonical_json(manifest_body)).hexdigest()
    if computed_sha != document["corpus_sha256"]:
        drift.append(
            f"corpus_sha256 mismatch: manifest={document['corpus_sha256']}, computed={computed_sha}"
        )

    tasks = document.get("tasks", {})
    if not isinstance(tasks, dict):
        drift.append("manifest tasks must be a dictionary")
        return drift

    for tid, task_data in sorted(tasks.items()):
        if not isinstance(task_data, dict):
            drift.append(f"task {tid}: task data must be a dictionary")
            continue
        fixture_str = task_data.get("fixture")
        if not isinstance(fixture_str, str):
            drift.append(f"task {tid}: missing or invalid fixture path")
            continue
        try:
            fixture_dir = _resolve_fixture_dir(fixture_str, tid, base_path=manifest_base)
        except ValueError as exc:
            drift.append(f"task {tid}: {exc}")
            continue
        if not fixture_dir.is_dir():
            drift.append(f"task {tid}: fixture directory missing: {fixture_dir}")
            continue
        expected_task_sha = task_data.get("task_sha256")
        current_sha = task_sha256(fixture_dir)
        if current_sha != expected_task_sha:
            drift.append(
                f"task {tid}: task_sha256 drifted: manifest={expected_task_sha}, current={current_sha}"
            )

    return drift


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.corpus")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--manifest", required=True, help="Path to manifest JSON")
    args = parser.parse_args(argv)
    if args.command == "verify":
        drift = verify(args.manifest)
        if drift:
            for item in drift:
                sys.stderr.write(f"{item}\n")
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
