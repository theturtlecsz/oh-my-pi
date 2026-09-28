"""Bundle verification: independent tests run against a clone, never the worker FS."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from omp_harbor_eval import EvidenceWriter, Fixture, load_fixture
from omp_harbor_eval.verify import REPO_ROOT, main, stage_argv, verify


def _create_synthetic_bundle(repo_dir: Path) -> Path:
    """Create a throwaway git repo, bundle it, and return the bundle path."""

    repo_dir.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "eval@example.com")
    git("config", "user.name", "Eval")
    (repo_dir / "sample.txt").write_text("synthetic content\n", encoding="utf-8")
    git("add", "sample.txt")
    git("commit", "-m", "init")
    bundle = repo_dir.parent / "worker-repo.bundle"
    git("bundle", "create", str(bundle), "--all")
    return bundle


def _write_fixture(
    root: Path,
    fixture_id: str,
    independent_tests: list[dict],
    *,
    rules: list[dict] | None = None,
) -> Fixture:
    directory = root / fixture_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "empty.patch").write_text("", encoding="utf-8")
    scenario = {
        "command": "/test",
        "terminal": {"pointer": "/status", "in": ["ok"]},
        "model_script": [],
        "ui_script": [],
    }
    (directory / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    fixture = {
        "id": fixture_id,
        "scored_experiment": "repair",
        "seed_patch": "empty.patch",
        "solution_patch": "empty.patch",
        "independent_tests": independent_tests,
        "scenario": "scenario.json",
        "rules": [{"type": "independent_tests_pass"}] if rules is None else rules,
    }
    (directory / "fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    return load_fixture(root, fixture_id)


def _seal(tmp_path: Path, fixture: Fixture, bundle: Path | None, *, variant: str = "known_good") -> tuple[Path, str]:
    evidence_dir = tmp_path / "evidence"
    writer = EvidenceWriter(
        evidence_dir,
        run_id="run-001",
        nonce="nonce-001",
        fixture_id=fixture.id,
        fixture_digest=fixture.digest,
        variant=variant,
        experiment=fixture.scored_experiment,
    )
    writer.write_json("outcome.json", {"outcome": "completed", "reason": "terminal", "prompts_sent": 1})
    if bundle is not None:
        shutil.copy(bundle, evidence_dir / "worker-repo.bundle")
    return evidence_dir, writer.seal()


def test_verify_records_bun_exit_codes_and_fails_the_grade(tmp_path: Path) -> None:
    worker_repo = tmp_path / "worker-source-repo"
    bundle = _create_synthetic_bundle(worker_repo)

    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "synth"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "pass.test.ts").write_text(
        'import { test, expect } from "bun:test";\ntest("pass", () => { expect(1).toBe(1); });\n',
        encoding="utf-8",
    )
    (fixture_dir / "fail.test.ts").write_text(
        'import { test, expect } from "bun:test";\ntest("fail", () => { expect(1).toBe(2); });\n',
        encoding="utf-8",
    )
    fixture = _write_fixture(
        fixtures_root,
        "synth",
        [
            {"runner": "bun", "target": "tests/pass.test.ts", "files": ["pass.test.ts"]},
            {"runner": "bun", "target": "tests/fail.test.ts", "files": ["fail.test.ts"]},
        ],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, bundle)
    shutil.rmtree(worker_repo)

    result = verify(evidence_dir, "synth", digest, fixtures_root=fixtures_root)

    assert result["grade"]["status"] == "fail"
    assert result["grade"]["label"] == "fail"
    assert result["grade"]["reasons"] == ["independent_tests_pass: bun tests/fail.test.ts failed"]

    tests = result["tests"]
    assert [record["target"] for record in tests] == ["tests/pass.test.ts", "tests/fail.test.ts"]
    assert tests[0]["exit_code"] == 0
    assert tests[0]["passed"] is True
    assert tests[1]["exit_code"] != 0
    assert tests[1]["passed"] is False

    assert not worker_repo.exists()


_AUDIT_DRIVER = r"""
import json
import sys

from omp_harbor_eval.verify import verify

evidence_dir, fixture_id, digest, fixtures_root, forbidden = sys.argv[1:6]
hits: list[str] = []


def hook(event: str, args: tuple) -> None:
    if event not in {"open", "subprocess.Popen", "os.exec"}:
        return
    text = repr(args)
    if forbidden in text:
        hits.append(f"{event}: {text}")


sys.addaudithook(hook)
result = verify(evidence_dir, fixture_id, digest, fixtures_root=fixtures_root)
print(json.dumps({"status": result["grade"]["status"], "hits": hits}))
"""


def test_verify_opens_and_spawns_nothing_under_the_bundle_source_repo(tmp_path: Path) -> None:
    worker_repo = tmp_path / "worker-source-repo"
    bundle = _create_synthetic_bundle(worker_repo)

    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "synth"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "pass.test.ts").write_text(
        'import { test, expect } from "bun:test";\ntest("pass", () => { expect(1).toBe(1); });\n',
        encoding="utf-8",
    )
    fixture = _write_fixture(
        fixtures_root,
        "synth",
        [{"runner": "bun", "target": "tests/pass.test.ts", "files": ["pass.test.ts"]}],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, bundle)
    shutil.rmtree(worker_repo)

    # The audit hook cannot be removed once installed, so run it in a child so
    # it cannot slow down or poison the rest of the suite.
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _AUDIT_DRIVER,
            str(evidence_dir),
            "synth",
            digest,
            str(fixtures_root),
            str(worker_repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    body = json.loads(completed.stdout)
    assert body["status"] == "pass"
    assert body["hits"] == []


def test_pytest_skip_is_recorded_as_not_passed(tmp_path: Path) -> None:
    worker_repo = tmp_path / "worker-source-repo"
    bundle = _create_synthetic_bundle(worker_repo)

    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "skip"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "test_skip.py").write_text(
        "import pytest\n\n\ndef test_skip() -> None:\n    pytest.skip('not applicable')\n",
        encoding="utf-8",
    )
    fixture = _write_fixture(
        fixtures_root,
        "skip",
        [{"runner": "pytest", "target": "tests/test_skip.py", "files": ["test_skip.py"]}],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, bundle)
    shutil.rmtree(worker_repo)

    result = verify(
        evidence_dir,
        "skip",
        digest,
        fixtures_root=fixtures_root,
        runners={"pytest": [sys.executable, "-m", "pytest"]},
    )

    record = result["tests"][0]
    assert record["runner"] == "pytest"
    assert record["passed"] is False
    assert record["reason"] == "junit reported 1 skipped"
    assert result["grade"]["status"] == "fail"


def test_missing_bundle_marks_every_test_failed(tmp_path: Path) -> None:
    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "missing"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "test_pass.py").write_text("def test_ok() -> None:\n    assert True\n", encoding="utf-8")
    fixture = _write_fixture(
        fixtures_root,
        "missing",
        [{"runner": "pytest", "target": "tests/test_pass.py", "files": ["test_pass.py"]}],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, None)

    result = verify(evidence_dir, "missing", digest, fixtures_root=fixtures_root)

    assert result["grade"]["status"] == "fail"
    record = result["tests"][0]
    assert record["passed"] is False
    assert record["exit_code"] is None
    assert record["reason"] == "missing file: worker-repo.bundle"


def test_invalid_manifest_yields_invalid_evidence_and_no_tests(tmp_path: Path) -> None:
    fixtures_root = tmp_path / "fixtures"
    fixture = _write_fixture(
        fixtures_root,
        "synth",
        [{"runner": "pytest", "target": "tests/test_pass.py"}],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, None)

    result = verify(evidence_dir, "synth", "0" * 64, fixtures_root=fixtures_root)

    assert result["tests"] == []
    assert result["grade"]["status"] == "invalid_evidence"


def test_repo_root_has_stage_script_and_cli_writes_out(tmp_path: Path) -> None:
    assert (REPO_ROOT / "session-system" / "runtime" / "stage.ts").is_file()

    worker_repo = tmp_path / "worker-source-repo"
    bundle = _create_synthetic_bundle(worker_repo)
    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "synth"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "pass.test.ts").write_text(
        'import { test, expect } from "bun:test";\ntest("pass", () => { expect(1).toBe(1); });\n',
        encoding="utf-8",
    )
    fixture = _write_fixture(
        fixtures_root,
        "synth",
        [{"runner": "bun", "target": "tests/pass.test.ts", "files": ["pass.test.ts"]}],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, bundle)
    out_file = tmp_path / "grade.json"

    exit_code = main(
        [
            "--evidence",
            str(evidence_dir),
            "--fixture",
            "synth",
            "--manifest-sha256",
            digest,
            "--out",
            str(out_file),
            "--fixtures-root",
            str(fixtures_root),
        ]
    )

    assert exit_code == 0
    assert out_file.is_file()
    document = json.loads(out_file.read_text(encoding="utf-8"))
    assert document["grade"]["status"] == "pass"
    assert document["tests"][0]["passed"] is True

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "omp_harbor_eval.verify",
            "--evidence",
            str(evidence_dir),
            "--fixture",
            "synth",
            "--manifest-sha256",
            digest,
            "--out",
            str(tmp_path / "cli.json"),
            "--fixtures-root",
            str(fixtures_root),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert cli.returncode == 0, cli.stderr
    assert (tmp_path / "cli.json").is_file()


def _marker_test(marker: Path) -> str:
    target = json.dumps(str(marker))
    return (
        'import { test, expect } from "bun:test";\n'
        'import { writeFileSync } from "node:fs";\n'
        "test('needs release', () => {\n"
        f"  writeFileSync({target}, 'ran\\n');\n"
        "  expect(1).toBe(1);\n"
        "});\n"
    )


def _bundle_with(repo_dir: Path, files: dict[str, str]) -> Path:
    repo_dir.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo_dir, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "eval@example.com")
    git("config", "user.name", "Eval")
    for relative, content in files.items():
        path = repo_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git("add", relative)
    git("commit", "-m", "init")
    bundle = repo_dir.parent / "worker-repo.bundle"
    git("bundle", "create", str(bundle), "--all")
    return bundle


def test_stage_failure_does_not_run_requiring_target(tmp_path: Path) -> None:
    marker = tmp_path / "requiring-target-ran"
    worker_repo = tmp_path / "worker-source-repo"
    bundle = _bundle_with(
        worker_repo,
        {"sample.txt": "synthetic content\n", "tests/needs-release.test.ts": _marker_test(marker)},
    )

    fixtures_root = tmp_path / "fixtures"
    fixture_dir = fixtures_root / "staged"
    fixture_dir.mkdir(parents=True)
    (fixture_dir / "needs-release.test.ts").write_text(_marker_test(marker), encoding="utf-8")
    (fixture_dir / "pass.test.ts").write_text(
        'import { test, expect } from "bun:test";\ntest("pass", () => { expect(1).toBe(1); });\n',
        encoding="utf-8",
    )
    fixture = _write_fixture(
        fixtures_root,
        "staged",
        [
            {
                "runner": "bun",
                "target": "tests/needs-release.test.ts",
                "files": ["needs-release.test.ts"],
                "requires": ["OMP_INSTALLED_RELEASE"],
            },
            {"runner": "bun", "target": "tests/pass.test.ts", "files": ["pass.test.ts"]},
        ],
    )
    evidence_dir, digest = _seal(tmp_path, fixture, bundle)
    shutil.rmtree(worker_repo)

    result = verify(evidence_dir, "staged", digest, fixtures_root=fixtures_root)

    assert [record["target"] for record in result["tests"]] == [
        "tests/needs-release.test.ts",
        "tests/pass.test.ts",
    ]
    requiring = result["tests"][0]
    assert requiring["passed"] is False
    assert requiring["reason"].startswith("stage failed:")
    assert requiring["reason"] != "stage failed:"
    assert not marker.exists()
    sibling = result["tests"][1]
    assert sibling["exit_code"] == 0
    assert sibling["passed"] is True
    assert "reason" not in sibling


def test_stage_argv_matches_installed_gate() -> None:
    clone = "/tmp/clone"
    dest = "/tmp/omp-installed-runtime"
    tools = {
        "bun": "/usr/local/bin/bun",
        "bun_version": "1.2.3",
        "uv": "/usr/local/bin/uv",
        "python": "/usr/local/bin/python3.13",
    }
    native = REPO_ROOT / "packages" / "natives" / "native"
    assert stage_argv(clone, dest, REPO_ROOT, tools) == [
        "bun",
        "session-system/runtime/stage.ts",
        "--source",
        clone,
        "--destination",
        dest,
        "--bun",
        tools["bun"],
        "--bun-version",
        tools["bun_version"],
        "--uv",
        tools["uv"],
        "--python",
        tools["python"],
        "--native",
        str(native / "pi_natives.linux-x64-baseline.node"),
        "--native",
        str(native / "pi_natives.linux-x64-modern.node"),
    ]
