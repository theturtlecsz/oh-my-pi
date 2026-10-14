from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omp_work import skip_accounting as sa
from omp_work import skip_accounting_report as report

SRC = Path(sa.__file__).resolve().parents[1]
REPO = report.repo_root()

COVER = '''
import os
import pytest

@pytest.mark.skipif(not os.environ.get("HAVE_X"), reason="needs X")
def test_env():
    if os.environ.get("FAIL") == "1":
        raise AssertionError("boom")

@pytest.mark.skipif(not os.environ.get("HAVE_Y"), reason="needs Y")
def test_zeta():
    pass

def test_plain():
    pass
'''

PHASE = '''
import pytest

@pytest.fixture
def teardown_skip():
    yield
    pytest.skip("teardown-skip")

def test_teardown(teardown_skip):
    pass
'''

ENV_NODE = "tests/test_cover.py::test_env"
ZETA_NODE = "tests/test_cover.py::test_zeta"
PHASE_NODE = "tests/test_phase.py::test_teardown"


def run_pytest(
    root: Path,
    *args: str,
    run: str = "r-report",
    check: str = "unit",
    out: Path,
    env: dict[str, str] | None = None,
) -> Path:
    full_env = {
        key: value
        for key, value in {**os.environ, **(env or {})}.items()
        if key != "PYTEST_ADDOPTS" and not key.startswith("PYTEST_")
    }
    full_env.update(
        {
            "PYTHONPATH": str(SRC),
            "OMP_ACCOUNTING_DIR": str(out),
            "OMP_ACCOUNTING_RUN": run,
            "OMP_ACCOUNTING_CHECK": check,
            "OMP_ACCOUNTING_ROOT": str(root),
        }
    )
    basetemp = root.with_name(root.name + "-basetemp")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "omp_work.skip_accounting",
            "-p",
            "no:cacheprovider",
            f"--basetemp={basetemp}",
            *args,
        ],
        cwd=root,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    files = sorted(out.glob(f"*{sa.RECEIPT_SUFFIX}"))
    assert len(files) == 1, proc.stdout + proc.stderr + str(files)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    return files[0]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("acct")
    (root / "tests").mkdir()
    (root / "tests" / "test_cover.py").write_text(COVER)
    (root / "tests" / "test_phase.py").write_text(PHASE)
    out = root / "out"
    receipts = {
        "root": root,
        "unit": run_pytest(root, "tests/test_cover.py", check="unit", out=out / "unit"),
        "postgres": run_pytest(
            root,
            "tests/test_cover.py",
            check="postgres",
            out=out / "postgres",
            env={"HAVE_X": "1", "HAVE_Y": "1"},
        ),
        "partial": run_pytest(
            root,
            "tests/test_cover.py",
            check="partial",
            out=out / "partial",
            env={"HAVE_X": "1"},
        ),
        "fail": run_pytest(
            root,
            "tests/test_cover.py",
            check="fail",
            out=out / "fail",
            env={"HAVE_X": "1", "HAVE_Y": "1", "FAIL": "1"},
        ),
        "subset": run_pytest(
            root,
            "tests/test_cover.py",
            "-k",
            "test_plain",
            check="subset",
            out=out / "subset",
            env={"HAVE_X": "1", "HAVE_Y": "1"},
        ),
        "collect": run_pytest(
            root,
            "tests/test_cover.py",
            "--collect-only",
            "-q",
            check="collect",
            out=out / "collect",
        ),
        "phase": run_pytest(root, "tests/test_phase.py", check="phase", out=out / "phase"),
    }
    return receipts


def invoke(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = {**os.environ, "PYTHONPATH": str(SRC)}
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "omp_work.skip_accounting_report", *args],
        cwd=REPO,
        env=merged_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _values(text: str, prefix: str) -> list[object]:
    head = f"{prefix}: "
    return [json.loads(line[len(head) :]) for line in text.splitlines() if line.startswith(head)]


def _one(text: str, prefix: str) -> object:
    found = _values(text, prefix)
    assert len(found) == 1, (prefix, found)
    return found[0]


def parse_nodes(markdown: str) -> list[dict[str, object]]:
    nodes = []
    for part in markdown.split("\n## Node\n")[1:]:
        body = part.split("\n## ", 1)[0]

        def grab(prefix: str, text: str = body) -> list[object]:
            return _values(text, prefix)

        assert len(grab("id")) == 1
        nodes.append(
            {
                "alternate_argv": grab("alternate-argv")[0],
                "alternate_call": grab("alternate-call")[0],
                "alternate_check": grab("alternate-check")[0],
                "alternate_digest": grab("alternate-digest")[0],
                "alternate_prerequisites": grab("alternate-prerequisites")[0],
                "alternate_skips": grab("alternate-skip"),
                "covered": grab("covered")[0],
                "diagnostics": grab("diagnostic"),
                "disposition": grab("disposition")[0],
                "failures": grab("failure"),
                "id": grab("id")[0],
                "skipped_phases": grab("skipped-phase"),
                "unit_phases": grab("unit-phase"),
            }
        )
    return nodes


def assert_same(document: dict[str, object], markdown: str) -> None:
    parsed = parse_nodes(markdown)
    nodes = document["nodes"]
    assert isinstance(nodes, list)
    assert [node["id"] for node in parsed] == [node["id"] for node in nodes]
    assert [node["id"] for node in nodes] == sorted(node["id"] for node in nodes)
    for left, right in zip(parsed, nodes, strict=True):
        assert left["id"] == right["id"]
        assert left["disposition"] == right["disposition"]
        assert left["covered"] == right["covered"]
        assert left["unit_phases"] == [[phase["phase"], phase["outcome"], phase["reason"]] for phase in right["unit_phases"]]
        assert left["skipped_phases"] == [[phase["phase"], phase["reason"]] for phase in right["skipped_phases"]]
        alternate = right["alternate"]
        if alternate is None:
            assert left["alternate_check"] is None
            assert left["alternate_argv"] is None
            assert left["alternate_prerequisites"] is None
            assert left["alternate_call"] is None
            assert left["alternate_digest"] is None
        else:
            assert left["alternate_check"] == alternate["check"]
            assert left["alternate_argv"] == alternate["argv"]
            assert left["alternate_prerequisites"] == alternate["prerequisites"]
            assert left["alternate_call"] == [alternate["call_outcome"], alternate["call_reason"]]
            assert left["alternate_digest"] == alternate["receipt_digest"]
        assert left["alternate_skips"] == [[row["check"], row["phase"], row["reason"]] for row in right["alternate_skips"]]
        assert left["diagnostics"] == right["diagnostics"]
        assert left["failures"] == [[row["check"], row["phase"], row["reason"]] for row in right["failures"]]
    assert _one(markdown, "run-id") == document["run_id"]
    assert _one(markdown, "run-fingerprint") == document["run_fingerprint"]
    assert _one(markdown, "source-fingerprint") == document["source_fingerprint"]
    assert _one(markdown, "historical-commit") == report.HISTORICAL_COMMIT == "5c57a261d6"
    assert _one(markdown, "historical-unit-passed") == report.HISTORICAL_UNIT_PASSED == 4054
    assert _one(markdown, "historical-unit-skipped") == report.HISTORICAL_UNIT_SKIPPED == 5156
    assert _one(markdown, "historical-integration-passed") == report.HISTORICAL_INTEGRATION_PASSED == 858
    observed = document["observed"]
    deltas = document["deltas"]
    assert _one(markdown, "observed-unit-passed") == observed["unit_passed"]
    assert _one(markdown, "observed-unit-skipped") == observed["unit_skipped"]
    assert _one(markdown, "observed-integration-passed") == observed["integration_passed"]
    assert _one(markdown, "delta-unit-passed") == deltas["unit_passed"]
    assert _one(markdown, "delta-unit-skipped") == deltas["unit_skipped"]
    assert _one(markdown, "delta-integration-passed") == deltas["integration_passed"]
    assert _values(markdown, "problem") == document["problems"]
    assert _values(markdown, "check") == document["checks"]
    assert _values(markdown, "rule") == document["rules"]
    assert _one(markdown, "waiting-for-chris") == document["waiting_for_chris"]
    assert _one(markdown, "ok") == document["ok"]


def run_cli(
    tmp_path: Path,
    unit: Path,
    alternates: list[Path],
    *,
    check: bool,
    root: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object], str]:
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    args = ["--unit", str(unit), "--json", str(json_path), "--markdown", str(markdown_path)]
    if root is not None:
        args.extend(["--root", str(root)])
    for alternate in alternates:
        args.extend(["--alternate", str(alternate)])
    if check:
        args.append("--check")
    env = {"OMP_ACCOUNTING_ROOT": str(root)} if root is not None else None
    proc = invoke(*args, env=env)
    assert json_path.is_file() and json_path.stat().st_size > 0, proc.stderr
    assert markdown_path.is_file() and markdown_path.stat().st_size > 0, proc.stderr
    assert proc.stdout == ""
    document = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert_same(document, markdown)
    return proc, document, markdown


def _node(document: dict[str, object], node_id: str) -> dict[str, object]:
    found = [node for node in document["nodes"] if node["id"] == node_id]
    assert len(found) == 1
    return found[0]


def _receipt_phases(path: Path, node_id: str) -> list[dict[str, str]]:
    receipt = sa.read_receipt(path)
    phases = receipt.nodes[node_id]
    return [
        {"outcome": phases[name].outcome, "phase": name, "reason": phases[name].reason}
        for name in sorted(phases)
    ]


def test_cli_writes_both_destinations_and_keeps_runtime_reasons(world: dict[str, Path], tmp_path: Path) -> None:
    proc, document, _markdown = run_cli(tmp_path, world["unit"], [world["postgres"]], check=True, root=world["root"])
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr == ""
    assert document["ok"] is True
    assert document["source_fingerprint"] == sa.fingerprint(world["root"])
    assert document["run_fingerprint"] == sa.read_receipt(world["unit"]).fingerprint
    assert document["source_matches_run"] is True
    assert document["notes"] == []

    env = _node(document, ENV_NODE)
    zeta = _node(document, ZETA_NODE)
    assert env["disposition"] == "enabled"
    assert zeta["disposition"] == "enabled"
    assert env["unit_phases"] == _receipt_phases(world["unit"], ENV_NODE)
    assert {"outcome": "skipped", "phase": "setup", "reason": "needs X"} in env["unit_phases"]
    assert {"phase": "setup", "reason": "needs Y"} in zeta["skipped_phases"]
    postgres = sa.read_receipt(world["postgres"])
    assert env["alternate"]["check"] == "postgres"
    assert env["alternate"]["argv"] == postgres.argv
    assert env["alternate"]["prerequisites"] == postgres.prerequisites
    assert env["alternate"]["call_outcome"] == "passed"
    assert env["alternate"]["receipt_digest"] == hashlib.sha256(world["postgres"].read_bytes()).hexdigest()
    assert document["observed"]["unit_passed"] == 1
    assert document["observed"]["unit_skipped"] == 2
    assert document["observed"]["integration_passed"] == 3
    assert document["deltas"]["unit_passed"] == 1 - 4054
    assert document["deltas"]["unit_skipped"] == 2 - 5156
    assert document["deltas"]["integration_passed"] == 3 - 858
    assert [node["id"] for node in document["nodes"]] == [ENV_NODE, ZETA_NODE]


def test_uncovered_diagnostic_names_the_unexecuted_id(world: dict[str, Path], tmp_path: Path) -> None:
    quiet, document, markdown = run_cli(tmp_path, world["unit"], [world["partial"]], check=False, root=world["root"])
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stderr == ""
    assert _node(document, ENV_NODE)["disposition"] == "enabled"
    uncovered = _node(document, ZETA_NODE)
    assert uncovered["disposition"] == "uncovered"
    assert uncovered["covered"] is False
    assert uncovered["alternate"] is None
    assert {"check": "partial", "phase": "setup", "reason": "needs Y"} in uncovered["alternate_skips"]
    assert "no executed unskipped call covers this node" in uncovered["diagnostics"]
    assert [node["id"] for node in document["nodes"] if node["disposition"] == "uncovered"] == [ZETA_NODE]
    assert ZETA_NODE in markdown
    assert document["observed"]["integration_passed"] is None
    assert document["deltas"]["integration_passed"] is None

    checked = tmp_path / "checked"
    checked.mkdir()
    proc, again, _markdown = run_cli(checked, world["unit"], [world["partial"]], check=True, root=world["root"])
    assert proc.returncode == 1
    assert f"uncovered: {ZETA_NODE}" in proc.stderr
    assert again["nodes"] == document["nodes"]


def test_failed_call_exits_nonzero_and_still_writes_both_outputs(world: dict[str, Path], tmp_path: Path) -> None:
    proc, document, markdown = run_cli(tmp_path, world["unit"], [world["fail"]], check=True, root=world["root"])
    assert proc.returncode == 1
    assert (tmp_path / "report.json").is_file()
    assert (tmp_path / "report.md").is_file()
    failed = _node(document, ENV_NODE)
    reason = sa.read_receipt(world["fail"]).nodes[ENV_NODE]["call"].reason
    assert "boom" in reason
    assert failed["disposition"] == "failing"
    assert failed["covered"] is True
    assert failed["alternate"]["check"] == "fail"
    assert failed["alternate"]["call_outcome"] == "failed"
    assert failed["alternate"]["call_reason"] == reason
    assert {"check": "fail", "phase": "call", "reason": reason} in failed["failures"]
    assert f"failing: {ENV_NODE}" in proc.stderr
    assert reason in markdown
    assert _node(document, ZETA_NODE)["disposition"] == "enabled"
    assert document["ok"] is False


def test_subset_and_collection_only_evidence_is_rejected(world: dict[str, Path], tmp_path: Path) -> None:
    subset = sa.read_receipt(world["subset"])
    collect = sa.read_receipt(world["collect"])
    assert subset.complete is True
    assert subset.selected == ["tests/test_cover.py::test_plain"]
    assert ENV_NODE in subset.collected
    assert collect.complete is False

    proc, document, _markdown = run_cli(tmp_path, world["unit"], [world["subset"], world["collect"]], check=True, root=world["root"])
    assert proc.returncode == 1
    assert [node["id"] for node in document["nodes"] if not node["covered"]] == [ENV_NODE, ZETA_NODE]
    for node_id in (ENV_NODE, ZETA_NODE):
        diagnostics = _node(document, node_id)["diagnostics"]
        assert "subset did not select this node (subset-only evidence)" in diagnostics
        assert _node(document, node_id)["alternate"] is None
    reasons = [item["reason"] for item in document["rejected"]]
    assert any(reason.startswith("incomplete") and "collect" in item["check"] for item, reason in zip(document["rejected"], reasons, strict=True))
    assert all(item["check"] != "subset" for item in document["rejected"])
    subset_view = next(item for item in document["alternates"] if item["check"] == "subset")
    assert subset_view["accepted"] is True
    assert subset_view["subset_only"] is True
    assert document["ok"] is False

    covered = tmp_path / "covered"
    covered.mkdir()
    proc, document, _markdown = run_cli(covered, world["unit"], [world["postgres"], world["collect"]], check=True, root=world["root"])
    assert proc.returncode == 1
    assert document["counts"]["enabled"] == 2
    assert document["counts"]["uncovered"] == 0
    assert any(item["check"] == "collect" and item["reason"].startswith("incomplete") for item in document["rejected"])
    assert any(problem.startswith("rejected: collect:") for problem in document["problems"])


def test_malformed_and_stale_evidence_fail_check_after_writing(world: dict[str, Path], tmp_path: Path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"version": 1, "run_id": "r-report"}\n', encoding="utf-8")
    proc, document, _markdown = run_cli(tmp_path, world["unit"], [malformed], check=True, root=world["root"])
    assert proc.returncode == 1
    assert document["nodes"]
    assert all(node["disposition"] == "uncovered" for node in document["nodes"])
    assert any("malformed" in item["reason"] for item in document["rejected"])
    assert any(problem.startswith("rejected:") for problem in document["problems"])

    stale = tmp_path / "stale.json"
    payload = json.loads(world["postgres"].read_text(encoding="utf-8"))
    payload["fingerprint"] = "0" * 64
    stale.write_text(json.dumps(payload), encoding="utf-8")
    stale_dir = tmp_path / "stale"
    stale_dir.mkdir()
    proc, document, _markdown = run_cli(stale_dir, world["unit"], [stale], check=True, root=world["root"])
    assert proc.returncode == 1
    assert document["rejected"] == [
        {"check": "postgres", "digest": hashlib.sha256(stale.read_bytes()).hexdigest(), "path": str(stale), "reason": "stale fingerprint"}
    ]
    assert [node["id"] for node in document["nodes"] if node["disposition"] == "uncovered"] == [ENV_NODE, ZETA_NODE]

    stale_unit = tmp_path / "stale_unit.json"
    unit_payload = json.loads(world["unit"].read_text(encoding="utf-8"))
    unit_payload["fingerprint"] = "0" * 64
    stale_unit.write_text(json.dumps(unit_payload), encoding="utf-8")
    stale_unit_dir = tmp_path / "stale_unit"
    stale_unit_dir.mkdir()
    proc, document, _markdown = run_cli(stale_unit_dir, stale_unit, [world["postgres"]], check=True, root=world["root"])
    assert proc.returncode == 1
    assert document["ok"] is False
    assert document["source_matches_run"] is False
    assert any("stale" in p for p in document["problems"])
    assert any(item["reason"] == "stale fingerprint" for item in document["rejected"])

    stale_run_dir = tmp_path / "stale_run"
    stale_run_dir.mkdir()
    proc, document, _markdown = run_cli(stale_run_dir, world["unit"], [world["postgres"]], check=True, root=REPO)
    assert proc.returncode == 1
    assert document["ok"] is False
    assert document["source_matches_run"] is False
    assert any("stale" in p for p in document["problems"])
    assert any(item["reason"] == "stale fingerprint" for item in document["rejected"])

    missing = tmp_path / "missing.json"
    missing_dir = tmp_path / "missing"
    missing_dir.mkdir()
    proc, document, _markdown = run_cli(missing_dir, missing, [], check=True, root=world["root"])
    assert proc.returncode == 1
    assert document["nodes"] == []
    assert document["ok"] is False
    assert any(problem.startswith("malformed:") for problem in document["problems"])


def test_teardown_skip_keeps_its_phase_and_stays_uncovered(world: dict[str, Path], tmp_path: Path) -> None:
    proc, document, _markdown = run_cli(tmp_path, world["phase"], [], check=True, root=world["root"])
    assert proc.returncode == 1
    node = _node(document, PHASE_NODE)
    assert node["unit_phases"] == _receipt_phases(world["phase"], PHASE_NODE)
    assert {"outcome": "passed", "phase": "call", "reason": ""} in node["unit_phases"]
    assert {"phase": "teardown", "reason": "teardown-skip"} in node["skipped_phases"]
    assert node["disposition"] == "uncovered"
    assert f"uncovered: {PHASE_NODE}" in proc.stderr


def test_source_fingerprint_ignores_generated_reports(tmp_path: Path) -> None:
    root = tmp_path
    source = root / "python" / "omp-work" / "src"
    source.mkdir(parents=True)
    module = source / "mod.py"
    module.write_text("x = 1\n", encoding="utf-8")
    reports = root / "reports"
    reports.mkdir()
    generated = source / "generated"
    generated.mkdir()
    (reports / "work-ledger-skipped-tests.json").write_text("{}\n", encoding="utf-8")
    (reports / "work-ledger-skipped-tests.md").write_text("# report\n", encoding="utf-8")
    (generated / "out.json").write_text("{}\n", encoding="utf-8")
    tests = root / "python" / "omp-work" / "tests"
    tests.mkdir(parents=True)
    (tests / "extra.skip-receipt.json").write_text("{}\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("x\n", encoding="utf-8")
    before = report.source_fingerprint(root)
    assert before == sa.fingerprint(root)
    (reports / "work-ledger-skipped-tests.json").write_text('{"n": 2}\n', encoding="utf-8")
    (generated / "out.json").write_text('{"n": 2}\n', encoding="utf-8")
    (tests / "extra.skip-receipt.json").write_text('{"n": 2}\n', encoding="utf-8")
    assert report.source_fingerprint(root) == before
    module.write_text("x = 2\n", encoding="utf-8")
    assert report.source_fingerprint(root) != before
