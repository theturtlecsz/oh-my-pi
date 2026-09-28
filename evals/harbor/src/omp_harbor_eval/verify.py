"""Verify sealed Harbor evidence by running independent tests against a cloned bundle.

Clones ``worker-repo.bundle`` out of the evidence directory into a fresh
temporary directory, links the gitignored host install (``node_modules`` and the
compiled ``pi_natives.*.node`` addons) into that clone, copies each fixture
test's ``files`` next to its ``target``, runs every independent test, records
the exit codes, and grades the sealed evidence with those records in place of
``independent-tests.json``. The worker filesystem is never read: the only
inputs are the evidence directory, the fixture directory, ``REPO_ROOT``, and a
temporary directory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .evidence import EvidenceError, load_evidence
from .fixtures import Fixture, IndependentTest, load_fixture
from .grader import grade

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES_ROOT = Path(__file__).resolve().parents[2] / "fixtures"
BUNDLE_NAME = "worker-repo.bundle"

DEFAULT_RUNNERS: dict[str, tuple[str, ...]] = {
    "bun": ("bun", "test"),
    "pytest": ("uv", "run", "--project", "python/omp-work", "--extra", "dev", "pytest"),
}

_INVALID_EVIDENCE = "invalid_evidence"


def _invalid_verdict(reasons: list[str]) -> dict[str, Any]:
    return {"status": _INVALID_EVIDENCE, "label": _INVALID_EVIDENCE, "reasons": list(reasons)}


def _junit_counts(path: Path) -> tuple[int, int]:
    """Return ``(total, skipped)`` testcase counts from a JUnit XML report."""

    root = ElementTree.parse(path).getroot()
    cases = root.iter("testcase")
    total = 0
    skipped = 0
    for case in cases:
        total += 1
        if case.find("skipped") is not None:
            skipped += 1
    return total, skipped


def _link(link: Path, target: Path) -> None:
    if link.exists() or link.is_symlink():
        return
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    except OSError:
        return


def _link_host_install(repo: Path) -> None:
    """Symlink gitignored host artifacts into the clone (node_modules, natives)."""

    node_modules = REPO_ROOT / "node_modules"
    if node_modules.is_dir():
        _link(repo / "node_modules", node_modules)
    for native in sorted((REPO_ROOT / "packages" / "natives" / "native").glob("pi_natives.*.node")):
        _link(repo / native.relative_to(REPO_ROOT), native)


def _copy_fixture_files(fixture: Fixture, test: IndependentTest, repo: Path) -> None:
    for relative in test.files:
        source = fixture.directory / relative
        destination = repo / Path(test.target).parent / Path(relative).name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())


def _run_test(
    runner: str,
    target: str,
    repo: Path,
    temp_dir: Path,
    prefix: Sequence[str],
    index: int,
) -> dict[str, Any]:
    command = [*prefix, target]
    junit_path = temp_dir / f"junit-{index}.xml"
    if runner == "pytest":
        command.append(f"--junitxml={junit_path}")
    try:
        completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
    except OSError as exc:
        return {"runner": runner, "target": target, "exit_code": None, "passed": False, "reason": str(exc)}

    exit_code = completed.returncode
    passed = exit_code == 0
    reason: str | None = None
    if runner == "pytest":
        if not passed:
            reason = f"exit code {exit_code}"
        else:
            try:
                total, skipped = _junit_counts(junit_path)
            except (OSError, ElementTree.ParseError):
                passed = False
                reason = "junit report missing or malformed"
            else:
                if total < 1:
                    passed = False
                    reason = "junit reported no tests"
                elif skipped > 0:
                    passed = False
                    reason = f"junit reported {skipped} skipped"
    elif not passed:
        reason = f"exit code {exit_code}"

    record: dict[str, Any] = {"runner": runner, "target": target, "exit_code": exit_code, "passed": passed}
    if reason is not None:
        record["reason"] = reason
    return record


def verify(
    evidence_dir: str | Path,
    fixture_id: str,
    manifest_sha256: str,
    *,
    fixtures_root: str | Path | None = None,
    out: str | Path | None = None,
    runners: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, Any]:
    """Grade ``evidence_dir`` with freshly executed independent tests.

    Returns ``{"tests": [...], "grade": <verdict>}``. When the evidence cannot
    be loaded, ``grade`` is an ``invalid_evidence`` verdict and ``tests`` is
    empty.
    """

    evidence_path = Path(evidence_dir)
    try:
        evidence = load_evidence(evidence_path, manifest_sha256)
    except EvidenceError as exc:
        result = {"tests": [], "grade": _invalid_verdict(exc.reasons)}
        _write(out, result)
        return result

    root = Path(fixtures_root) if fixtures_root is not None else FIXTURES_ROOT
    fixture = load_fixture(root, fixture_id)
    merged = dict(DEFAULT_RUNNERS)
    if runners is not None:
        merged.update({name: tuple(value) for name, value in runners.items()})

    records: list[dict[str, Any]] = []
    bundle = evidence_path / BUNDLE_NAME
    with tempfile.TemporaryDirectory(prefix="omp-harbor-verify-") as temp:
        temp_dir = Path(temp)
        repo = temp_dir / "repo"
        tests = fixture.independent_tests
        if not bundle.is_file():
            records = [
                {
                    "runner": test.runner,
                    "target": test.target,
                    "exit_code": None,
                    "passed": False,
                    "reason": f"missing file: {BUNDLE_NAME}",
                }
                for test in tests
            ]
        else:
            cloned = subprocess.run(
                ["git", "clone", "-q", str(bundle), str(repo)],
                capture_output=True,
                text=True,
                check=False,
            )
            if cloned.returncode != 0:
                records = [
                    {
                        "runner": test.runner,
                        "target": test.target,
                        "exit_code": None,
                        "passed": False,
                        "reason": f"git clone failed: {cloned.stderr.strip()}",
                    }
                    for test in tests
                ]
            else:
                _link_host_install(repo)
                for index, test in enumerate(tests):
                    _copy_fixture_files(fixture, test, repo)
                    prefix = merged.get(test.runner, (test.runner,))
                    records.append(_run_test(test.runner, test.target, repo, temp_dir, prefix, index))

    verdict = grade(
        evidence_path,
        manifest_sha256,
        fixture,
        run_id=evidence.run_id,
        nonce=evidence.nonce,
        test_results=records,
    )
    result = {"tests": records, "grade": verdict}
    _write(out, result)
    return result


def _write(out: str | Path | None, document: dict[str, Any]) -> None:
    if out is None:
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m omp_harbor_eval.verify")
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fixtures-root")
    args = parser.parse_args(argv)

    try:
        result = verify(
            args.evidence,
            args.fixture,
            args.manifest_sha256,
            fixtures_root=args.fixtures_root,
            out=args.out,
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0 if result["grade"]["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
