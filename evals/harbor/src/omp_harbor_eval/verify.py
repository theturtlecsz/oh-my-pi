"""Verify sealed Harbor evidence by running independent tests against a cloned bundle.

Clones ``worker-repo.bundle`` out of the evidence directory into a fresh
temporary directory, links the gitignored host install (``node_modules`` and the
compiled ``pi_natives.*.node`` addons) into that clone, and, when any fixture
test requires ``OMP_INSTALLED_RELEASE``, stages that clone once before copying
fixture files. It then copies each test's ``files`` next to its ``target``,
runs every independent test, records the exit codes, and grades the sealed
evidence with those records in place of ``independent-tests.json``. The worker
filesystem is never read: the only inputs are the evidence directory, the
fixture directory, ``REPO_ROOT``, and a temporary directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

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
_INSTALLED_RELEASE = "OMP_INSTALLED_RELEASE"
_STAGE_DEST = "omp-installed-runtime"
_STDERR_TAIL = 500
_NATIVE_ADDONS = (
    "pi_natives.linux-x64-baseline.node",
    "pi_natives.linux-x64-modern.node",
)


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


class _StageFailure(NamedTuple):
    exit_code: int | None
    detail: str


def stage_argv(
    clone: str | Path,
    dest: str | Path,
    repo_root: str | Path,
    tools: Mapping[str, str],
) -> list[str]:
    """Argv for ``stage.ts`` with cwd = ``clone``.

    ``tools`` maps ``bun`` (absolute interpreter), ``bun_version``, ``uv``, and
    ``python`` (``uv python find 3.13``). Both ``--native`` paths are under
    ``repo_root``.
    """

    native = Path(repo_root) / "packages" / "natives" / "native"
    return [
        "bun",
        "session-system/runtime/stage.ts",
        "--source",
        str(clone),
        "--destination",
        str(dest),
        "--bun",
        tools["bun"],
        "--bun-version",
        tools["bun_version"],
        "--uv",
        tools["uv"],
        "--python",
        tools["python"],
        "--native",
        str(native / _NATIVE_ADDONS[0]),
        "--native",
        str(native / _NATIVE_ADDONS[1]),
    ]


def _stderr_tail(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _STDERR_TAIL:
        return collapsed
    return collapsed[-_STDERR_TAIL:]


def _stage_reason(detail: str) -> str:
    tail = _stderr_tail(detail)
    if not tail:
        tail = "no stage output"
    return f"stage failed: {tail}"


def _requires_installed_release(test: IndependentTest) -> bool:
    return _INSTALLED_RELEASE in test.requires


def _command_detail(completed: subprocess.CompletedProcess[str], fallback: str) -> str:
    return _stderr_tail(completed.stderr) or _stderr_tail(completed.stdout) or fallback


def _resolve_stage_tools() -> tuple[dict[str, str] | None, str]:
    bun = shutil.which("bun")
    if bun is None:
        return None, "bun not found"
    uv = shutil.which("uv")
    if uv is None:
        return None, "uv not found"
    version = subprocess.run(  # nosec B603 - absolute interpreter from shutil.which, fixed argv
        [bun, "--version"], capture_output=True, text=True, check=False
    )
    if version.returncode != 0:
        return None, _command_detail(version, "bun --version failed")
    bun_version = version.stdout.strip()
    if not bun_version:
        return None, "bun --version produced no version"
    python = subprocess.run(  # nosec B603 - absolute interpreter from shutil.which, fixed argv
        [uv, "python", "find", "3.13"], capture_output=True, text=True, check=False
    )
    if python.returncode != 0:
        return None, _command_detail(python, "uv python find 3.13 failed")
    python_path = python.stdout.strip()
    if not python_path:
        return None, "uv python find 3.13 produced no interpreter"
    return {"bun": bun, "bun_version": bun_version, "uv": uv, "python": python_path}, ""


def _stage_installed(clone: Path, temp_dir: Path) -> tuple[dict[str, str] | None, _StageFailure | None]:
    dest = temp_dir / _STAGE_DEST
    tools, tool_error = _resolve_stage_tools()
    if tools is None:
        return None, _StageFailure(None, tool_error)
    try:
        completed = subprocess.run(  # nosec B603 - argv list built from verified tools, no shell
            stage_argv(clone, dest, REPO_ROOT, tools),
            cwd=clone,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return None, _StageFailure(None, str(exc))
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        return None, _StageFailure(completed.returncode, detail)
    manifest = dest / "manifest.json"
    if not manifest.is_file():
        return None, _StageFailure(completed.returncode, completed.stderr.strip() or "manifest missing")
    try:
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    except OSError as exc:
        return None, _StageFailure(completed.returncode, str(exc))
    return {
        "OMP_INSTALLED_RELEASE": str(dest),
        "OMP_INSTALLED_MANIFEST_SHA256": digest,
    }, None


def _stage_failed_record(test: IndependentTest, failure: _StageFailure) -> dict[str, Any]:
    return {
        "runner": test.runner,
        "target": test.target,
        "exit_code": failure.exit_code,
        "passed": False,
        "reason": _stage_reason(failure.detail),
    }


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
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    command = [*prefix, target]
    junit_path = temp_dir / f"junit-{index}.xml"
    if runner == "pytest":
        command.append(f"--junitxml={junit_path}")
    run_env = None if env is None else {**os.environ, **env}
    try:
        completed = subprocess.run(  # nosec B603 - argv list; runner/target come from the sealed fixture
            command, cwd=repo, capture_output=True, text=True, check=False, env=run_env
        )
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
                # stage.ts refuses a dirty tree, including untracked fixture files.
                installed_env: Mapping[str, str] | None = None
                stage_failure: _StageFailure | None = None
                if any(_requires_installed_release(test) for test in tests):
                    installed_env, stage_failure = _stage_installed(repo, temp_dir)
                for index, test in enumerate(tests):
                    if _requires_installed_release(test) and stage_failure is not None:
                        records.append(_stage_failed_record(test, stage_failure))
                        continue
                    _copy_fixture_files(fixture, test, repo)
                    prefix = merged.get(test.runner, (test.runner,))
                    env = installed_env if _requires_installed_release(test) else None
                    records.append(_run_test(test.runner, test.target, repo, temp_dir, prefix, index, env))

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
