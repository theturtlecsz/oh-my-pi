"""Harbor corpus: materialize tasks and gradeability check tools."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .corpus import CATEGORIES, _resolve_fixture_dir, extract_patch_paths
from .fixtures import Fixture, IndependentTest, load_fixture
from .verify import DEFAULT_RUNNERS, REPO_ROOT, _copy_fixture_files, _link_host_install, _run_test

_parents = Path(__file__).resolve().parents
EVALS_HARBOR_ROOT = _parents[2] if len(_parents) > 2 else Path("/evals/harbor")


class Worktree(type(Path())):
    """A git worktree Path that also implements context manager cleanup."""

    def __new__(cls, *args: Any, **kwargs: Any) -> Worktree:
        return super().__new__(cls, *args, **kwargs)

    def init_worktree(self, repo: Path) -> None:
        self._repo = repo
        self._cleaned = False

    def cleanup(self) -> None:
        if not getattr(self, "_cleaned", False):
            self._cleaned = True
            repo = getattr(self, "_repo", None)
            if repo is not None:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "remove", "--force", str(self)],
                    capture_output=True,
                    check=False,
                )
            if self.exists():
                shutil.rmtree(self, ignore_errors=True)
            if repo is not None:
                subprocess.run(
                    ["git", "-C", str(repo), "worktree", "prune"],
                    capture_output=True,
                    check=False,
                )

    def __enter__(self) -> Worktree:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.cleanup()


def worktree(repo: str | Path, commit: str = "HEAD") -> Worktree:
    """Create a temporary git worktree at commit with host install linked in."""
    repo_path = Path(repo).resolve()
    temp_dir = tempfile.mkdtemp(prefix="omp-harbor-wt-")
    shutil.rmtree(temp_dir, ignore_errors=True)
    res = subprocess.run(  # nosec B603
        ["git", "-C", str(repo_path), "worktree", "add", "--detach", temp_dir, commit],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"git worktree add failed: {res.stderr.strip()}")
    wt = Worktree(temp_dir)
    wt.init_worktree(repo_path)
    _link_host_install(wt)
    return wt


def apply(tree: str | Path, patch: str | Path) -> None:
    """Apply a patch file or patch string to a git worktree."""
    tree_path = Path(tree).resolve()
    if isinstance(patch, (str, Path)) and "\n" not in str(patch) and Path(patch).is_file():
        patch_path = Path(patch).resolve()
        res = subprocess.run(  # nosec B603
            ["git", "-C", str(tree_path), "apply", str(patch_path)],
            capture_output=True,
            text=True,
        )
    else:
        patch_text = str(patch)
        res = subprocess.run(  # nosec B603
            ["git", "-C", str(tree_path), "apply", "-"],
            input=patch_text,
            capture_output=True,
            text=True,
            cwd=tree_path,
        )
    if res.returncode != 0:
        raise RuntimeError(f"git apply failed: {res.stderr.strip()}")


def run_tests(
    tree: str | Path,
    tests: Sequence[IndependentTest | Mapping[str, Any]],
    fixture: Fixture | None = None,
) -> list[dict[str, Any]]:
    """Run independent tests against tree, linking host install and copying fixture files."""
    tree_path = Path(tree).resolve()
    _link_host_install(tree_path)
    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="omp-harbor-run-tests-") as temp:
        temp_dir = Path(temp)
        for index, test_item in enumerate(tests):
            if isinstance(test_item, Mapping):
                test = IndependentTest(
                    runner=str(test_item["runner"]),
                    target=str(test_item["target"]),
                    requires=tuple(test_item.get("requires", ())),
                    files=tuple(test_item.get("files", ())),
                )
            else:
                test = test_item
            if fixture is not None:
                _copy_fixture_files(fixture, test, tree_path)
            prefix = DEFAULT_RUNNERS.get(test.runner, (test.runner,))
            records.append(_run_test(test.runner, test.target, tree_path, temp_dir, prefix, index, env=None))
    return records


def gradeable(fixture: Fixture | Path | str, repo: Path | str, commit: str = "HEAD") -> dict[str, Any]:
    """Check gradeability: each test without requires fails on seed and passes on seed+solution."""
    repo_path = Path(repo).resolve()
    if isinstance(fixture, Fixture):
        fixture_obj = fixture
        fixture_dir = fixture_obj.directory
    else:
        fixture_dir = Path(fixture).resolve()
        fixture_obj = load_fixture(fixture_dir.parent, fixture_dir.name)

    target_tests = [t for t in fixture_obj.independent_tests if not t.requires]
    if not target_tests:
        return {
            "task_id": fixture_obj.id,
            "seed_fails": False,
            "solution_passes": False,
            "ok": False,
        }

    with worktree(repo_path, commit) as wt:
        seed_path = fixture_dir / fixture_obj.seed_patch
        try:
            apply(wt, seed_path)
        except Exception:
            return {
                "task_id": fixture_obj.id,
                "seed_fails": False,
                "solution_passes": False,
                "ok": False,
            }

        seed_records = run_tests(wt, target_tests, fixture_obj)
        seed_fails = all(not r.get("passed", False) for r in seed_records)

        solution_path = fixture_dir / fixture_obj.solution_patch
        try:
            apply(wt, solution_path)
            solution_records = run_tests(wt, target_tests, fixture_obj)
            solution_passes = all(bool(r.get("passed", False)) for r in solution_records)
        except Exception:
            solution_passes = False

    ok = bool(seed_fails and solution_passes)
    return {
        "task_id": fixture_obj.id,
        "seed_fails": seed_fails,
        "solution_passes": solution_passes,
        "ok": ok,
    }


def _make_delete_patch(repo: Path, target_file: str, commit: str) -> str:
    empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    res = subprocess.run(  # nosec B603
        ["git", "-C", str(repo), "diff", "HEAD", empty_tree, "--", target_file],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout
    if commit != "HEAD":
        res = subprocess.run(  # nosec B603
            ["git", "-C", str(repo), "diff", commit, empty_tree, "--", target_file],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout
    disk_file = repo / target_file
    if disk_file.is_file():
        lines = disk_file.read_text(encoding="utf-8").splitlines()
        del_lines = [f"-{line}" for line in lines]
        n = len(lines)
        return (
            f"diff --git a/{target_file} b/{target_file}\n"
            f"deleted file mode 100644\n"
            f"--- a/{target_file}\n"
            f"+++ /dev/null\n"
            f"@@ -1,{n} +0,0 @@\n" + "\n".join(del_lines) + "\n"
        )
    return ""


def _reverse_patch_in_worktree(repo: Path, commit: str, patch_text: str) -> str:
    wt = worktree(repo, commit)
    try:
        apply(wt, patch_text)
        res = subprocess.run(  # nosec B603
            ["git", "-C", str(wt), "diff", "-R"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout
    finally:
        wt.cleanup()


def _resolve_templates_dir(repo_path: Path) -> Path:
    candidates = [
        EVALS_HARBOR_ROOT / "corpus" / "templates",
        REPO_ROOT / "evals" / "harbor" / "corpus" / "templates",
        repo_path / "evals" / "harbor" / "corpus" / "templates",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    raise ValueError(f"templates directory not found; searched {candidates}")


def materialize(
    spec: Path | str | Mapping[str, Any],
    repo: Path | str,
    out: Path | str,
) -> Path:
    """Materialize a Harbor task fixture from a specification."""
    if isinstance(spec, (str, Path)):
        spec_path = Path(spec)
        if not spec_path.is_file():
            raise ValueError(f"spec file not found: {spec_path}")
        try:
            document = json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"cannot parse spec JSON {spec_path}: {exc}") from exc
    elif isinstance(spec, Mapping):
        document = dict(spec)
    else:
        raise ValueError(f"invalid spec type: {type(spec).__name__}")

    task_id = document.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("spec missing valid task_id")

    category = document.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"invalid category {category!r}, must be one of {CATEGORIES}")

    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("spec missing valid title")

    source_commit = document.get("source_commit")
    if not isinstance(source_commit, str) or not source_commit.strip():
        raise ValueError("spec missing valid source_commit")

    test_target = document.get("test_target")
    if not isinstance(test_target, str) or not test_target.strip():
        raise ValueError("spec missing valid test_target")

    runner = document.get("runner")
    if runner not in ("bun", "pytest"):
        raise ValueError(f"invalid runner {runner!r}, must be 'bun' or 'pytest'")

    instruction = document.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("spec missing valid instruction")

    ac_raw = document.get("acceptance_criteria")
    if ac_raw is None:
        ac_raw = document.get("ac") or document.get("AC")
    if not isinstance(ac_raw, list) or not (1 <= len(ac_raw) <= 3) or not all(isinstance(c, str) and c.strip() for c in ac_raw):
        raise ValueError("spec must have 1 to 3 acceptance criteria")
    ac = [c.strip() for c in ac_raw]

    # Refuse leaked withheld_detail in title, instruction, or AC (case-insensitive)
    withheld = document.get("withheld_detail")
    if withheld:
        details = [withheld] if isinstance(withheld, str) else list(withheld)
        for d in details:
            if not isinstance(d, str) or not d.strip():
                continue
            d_lower = d.strip().lower()
            if d_lower in title.lower():
                raise ValueError(f"withheld_detail leaked into title: {d}")
            if d_lower in instruction.lower():
                raise ValueError(f"withheld_detail leaked into instruction: {d}")
            for c in ac:
                if d_lower in c.lower():
                    raise ValueError(f"withheld_detail leaked into acceptance criteria: {d}")

    repo_path = Path(repo).resolve()
    test_target_file = test_target.split("::")[0]
    basename = Path(test_target_file).name

    # Read independent/<basename> from HEAD test_target
    res_head = subprocess.run(  # nosec B603
        ["git", "-C", str(repo_path), "show", f"HEAD:{test_target_file}"],
        capture_output=True,
        text=True,
    )
    if res_head.returncode == 0:
        head_test_content = res_head.stdout
    else:
        disk_test = repo_path / test_target_file
        if disk_test.is_file():
            head_test_content = disk_test.read_text(encoding="utf-8")
        else:
            raise ValueError(f"test_target not found at HEAD: {test_target_file}")

    delete_patch = _make_delete_patch(repo_path, test_target_file, source_commit)

    if category == "seeded_defect":
        raw_defect = document.get("defect_patch")
        if raw_defect is None:
            raise ValueError("defect_patch required for category seeded_defect")
        if isinstance(raw_defect, (str, Path)) and "\n" not in str(raw_defect) and Path(raw_defect).is_file():
            defect_patch_text = Path(raw_defect).read_text(encoding="utf-8")
        else:
            defect_patch_text = str(raw_defect)
        if not defect_patch_text.strip():
            raise ValueError("empty diff")

        source_paths = document.get("source_paths")
        if source_paths:
            extracted = set(extract_patch_paths(defect_patch_text))
            untouched = set(source_paths) - extracted
            if untouched:
                raise ValueError(f"untouched path in source_paths: {sorted(untouched)}")

        seed_patch_text = defect_patch_text.rstrip("\n") + "\n" + delete_patch
        try:
            solution_patch_text = _reverse_patch_in_worktree(repo_path, source_commit, defect_patch_text)
        except Exception as exc:
            raise ValueError(f"seed not applying: {exc}") from exc
        if not solution_patch_text.strip():
            raise ValueError("empty diff")
    else:
        source_paths_raw = document.get("source_paths")
        if not isinstance(source_paths_raw, list) or not source_paths_raw:
            raise ValueError("source_paths must be a non-empty list")
        source_paths = [str(p) for p in source_paths_raw]

        diff_res = subprocess.run(  # nosec B603
            ["git", "-C", str(repo_path), "diff", source_commit, f"{source_commit}^", "--"] + source_paths,
            capture_output=True,
            text=True,
        )
        if diff_res.returncode != 0:
            raise ValueError(f"git diff failed: {diff_res.stderr.strip()}")
        revert_diff = diff_res.stdout
        if not revert_diff.strip():
            raise ValueError("empty diff")

        extracted = set(extract_patch_paths(revert_diff))
        untouched = set(source_paths) - extracted
        if untouched:
            raise ValueError(f"untouched path in source_paths: {sorted(untouched)}")

        seed_patch_text = revert_diff.rstrip("\n") + "\n" + delete_patch

        sol_res = subprocess.run(  # nosec B603
            ["git", "-C", str(repo_path), "diff", f"{source_commit}^", source_commit, "--"] + source_paths,
            capture_output=True,
            text=True,
        )
        if sol_res.returncode != 0:
            raise ValueError(f"git diff solution failed: {sol_res.stderr.strip()}")
        solution_patch_text = sol_res.stdout
        if not solution_patch_text.strip():
            raise ValueError("empty diff")

    # Check seed applies cleanly
    wt_check = worktree(repo_path, source_commit)
    try:
        apply(wt_check, seed_patch_text)
    except Exception as exc:
        raise ValueError(f"seed not applying: {exc}") from exc
    finally:
        wt_check.cleanup()

    # Materialize output files
    out_dir = Path(out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy template files
    templates_dir = _resolve_templates_dir(repo_path)
    for item in templates_dir.iterdir():
        dest = out_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)

    (out_dir / "seed.patch").write_text(seed_patch_text, encoding="utf-8")
    (out_dir / "solution.patch").write_text(solution_patch_text, encoding="utf-8")
    (out_dir / "instruction.md").write_text(instruction.strip() + "\n", encoding="utf-8")

    # independent/<basename>
    independent_dir = out_dir / "independent"
    independent_dir.mkdir(parents=True, exist_ok=True)
    (independent_dir / basename).write_text(head_test_content, encoding="utf-8")

    # ledger-seed.json
    ledger_seed_doc = {
        "project": {
            "project_id": "00000000-0000-7000-8000-0000000000b1",
            "name": "The Bookends",
            "kind": "surface",
        },
        "items": [
            {
                "key": "OMP-1",
                "work_id": "00000000-0000-7000-8000-000000000001",
                "revision_id": "00000000-0000-7000-8000-000000000010",
                "revision_number": 1,
                "title": title.strip(),
                "description": instruction.strip(),
                "scope": document.get("scope", "initial/scope"),
                "acceptance_criteria": ac,
                "state": "IN_PROGRESS",
            }
        ],
    }
    (out_dir / "ledger-seed.json").write_text(
        json.dumps(ledger_seed_doc, indent=2) + "\n",
        encoding="utf-8",
    )

    # fixture.json
    fixture_doc = {
        "id": task_id,
        "scored_experiment": "repair",
        "seed_patch": "seed.patch",
        "solution_patch": "solution.patch",
        "independent_tests": [
            {
                "runner": runner,
                "target": test_target,
                "files": [f"independent/{basename}"],
            }
        ],
        "scenario": "scenario.json",
        "rules": [
            {
                "type": "independent_tests_pass",
            }
        ],
    }
    (out_dir / "fixture.json").write_text(
        json.dumps(fixture_doc, indent=2) + "\n",
        encoding="utf-8",
    )

    return out_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.corpus_build")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mat_parser = subparsers.add_parser("materialize")
    mat_parser.add_argument("--spec", required=True, help="Spec JSON path")
    mat_parser.add_argument("--repo", required=True, help="Repository path")
    mat_parser.add_argument("--out", required=True, help="Output directory")

    chk_parser = subparsers.add_parser("check")
    chk_parser.add_argument("--fixture", required=True, help="Fixture directory path")
    chk_parser.add_argument("--repo", required=True, help="Repository path")
    chk_parser.add_argument("--commit", default="HEAD", help="Base commit")

    args = parser.parse_args(argv)

    if args.command == "materialize":
        try:
            materialize(spec=args.spec, repo=args.repo, out=args.out)
        except Exception as exc:
            sys.stderr.write(f"materialize failed: {exc}\n")
            return 1
        return 0

    if args.command == "check":
        result = gradeable(fixture=args.fixture, repo=args.repo, commit=args.commit)
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
        return 0 if result.get("ok") else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
