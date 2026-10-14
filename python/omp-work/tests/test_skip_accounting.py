from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from omp_work import skip_accounting as sa

SRC = Path(sa.__file__).resolve().parents[1]

SUITE = '''
import pytest

@pytest.fixture(params=["a", "b"])
def maybe(request):
    if request.param == "b":
        pytest.skip("fixture-needs-b")
    return request.param

def test_fixture(maybe):
    assert maybe == "a"

@pytest.mark.parametrize("n", [1, 2])
def test_body(n):
    if n == 2:
        pytest.skip("body-skip-2")

@pytest.fixture
def teardown_skip():
    yield
    pytest.skip("teardown-skip")

def test_teardown(teardown_skip):
    pass

def test_plain():
    pass
'''


def run_pytest(
    root: Path,
    *args: str,
    run: str = "run1",
    check: str = "unit",
    accounting_root: Path | None = None,
    out: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[sa.Receipt]]:
    out = out or root / "receipts"
    # The gate sets PYTEST_ADDOPTS=--basetemp to a directory that contains this
    # tmp_path. Pytest exits 4 when that basetemp is the cwd or a parent of it
    # (it would delete the suite). Drop the parent's pytest env and give the
    # child a sibling basetemp that is not an ancestor of cwd.
    full_env = {
        k: v
        for k, v in {**os.environ, **(env or {})}.items()
        if k != "PYTEST_ADDOPTS" and not k.startswith("PYTEST_")
    }
    full_env.update(
        {
            "PYTHONPATH": str(SRC),
            "OMP_ACCOUNTING_DIR": str(out),
            "OMP_ACCOUNTING_RUN": run,
            "OMP_ACCOUNTING_CHECK": check,
            "OMP_ACCOUNTING_ROOT": str(accounting_root or root),
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
    )
    receipts = [sa.read_receipt(p) for p in sorted(out.glob(f"*{sa.RECEIPT_SUFFIX}"))]
    return proc, receipts


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_suite.py").write_text(SUITE)
    (tmp_path / "package.json").write_text("{}")
    return tmp_path


def only(receipts: list[sa.Receipt]) -> sa.Receipt:
    assert len(receipts) == 1
    return receipts[0]


def test_skips_in_every_phase_keep_exact_ids_and_reasons(repo: Path) -> None:
    proc, receipts = run_pytest(repo, "tests")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    receipt = only(receipts)
    assert receipt.complete and receipt.run_id == "run1" and receipt.check == "unit"
    skipped = {n: sa._skipped_phases(p) for n, p in receipt.nodes.items() if sa._skipped_phases(p)}
    assert skipped == {
        "tests/test_suite.py::test_fixture[b]": {"setup": "fixture-needs-b"},
        "tests/test_suite.py::test_body[2]": {"call": "body-skip-2"},
        "tests/test_suite.py::test_teardown": {"teardown": "teardown-skip"},
    }
    # teardown skip happens after an executed call, yet the node must still not count as covered
    assert receipt.nodes["tests/test_suite.py::test_teardown"]["call"].outcome == "passed"
    assert receipt.fingerprint == sa.fingerprint(repo)


def test_xdist_controller_writes_single_receipt(repo: Path) -> None:
    pytest.importorskip("xdist")
    proc, receipts = run_pytest(repo, "-n", "3", "tests")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    receipt = only(receipts)
    assert receipt.complete
    assert "tests/test_suite.py::test_body[2]" in receipt.selected
    assert receipt.nodes["tests/test_suite.py::test_body[2]"]["call"].reason == "body-skip-2"
    assert receipt.nodes["tests/test_suite.py::test_fixture[b]"]["setup"].reason == "fixture-needs-b"


def test_node_ids_are_repo_relative_when_rootdir_is_nested(tmp_path: Path) -> None:
    nested = tmp_path / "python" / "omp-work"
    (nested / "tests").mkdir(parents=True)
    (nested / "tests" / "test_suite.py").write_text(SUITE)
    (nested / "pytest.ini").write_text("[pytest]\n")
    proc, receipts = run_pytest(nested, "tests", accounting_root=tmp_path, out=tmp_path / "out")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "python/omp-work/tests/test_suite.py::test_body[2]" in only(receipts).selected


def test_parent_basetemp_ancestor_does_not_abort_the_child(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Reproduce the gate: PYTEST_ADDOPTS names a basetemp that contains the suite cwd.
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--basetemp={repo.parent} -q")
    proc, receipts = run_pytest(repo, "tests")
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert only(receipts).nodes["tests/test_suite.py::test_body[2]"]["call"].reason == "body-skip-2"


def test_deselected_and_collect_only_are_not_complete_evidence(repo: Path) -> None:
    _, receipts = run_pytest(repo, "tests", "-k", "test_plain")
    receipt = only(receipts)
    assert receipt.selected == ["tests/test_suite.py::test_plain"]
    assert "tests/test_suite.py::test_body[2]" in receipt.collected

    collect_repo_out = repo / "collect"
    proc, receipts = run_pytest(repo, "tests", "--collect-only", "-q", out=collect_repo_out)
    assert proc.returncode == 0
    receipt = only(receipts)
    assert not receipt.complete and receipt.nodes == {}


def test_interrupted_run_is_incomplete(repo: Path) -> None:
    (repo / "tests" / "test_stop.py").write_text("def test_a():\n    raise KeyboardInterrupt\n")
    _, receipts = run_pytest(repo, "tests/test_stop.py")
    assert not only(receipts).complete


def make_receipt(check: str = "unit", run: str = "r", fp: str = "f", **nodes: dict[str, tuple[str, str]]) -> sa.Receipt:
    phases = {
        n: {ph: sa.Phase(o, reason) for ph, (o, reason) in spec.items()} for n, spec in nodes.items()
    }
    return sa.Receipt(
        run_id=run,
        fingerprint=fp,
        check=check,
        argv=[],
        prerequisites=[],
        selected=sorted(phases),
        collected=sorted(phases),
        nodes=phases,
        exitstatus=0,
        complete=True,
    )


PASS = {"setup": ("passed", ""), "call": ("passed", ""), "teardown": ("passed", "")}
SKIP_CALL = {"setup": ("passed", ""), "call": ("skipped", "no pg"), "teardown": ("passed", "")}


def test_reconcile_covers_only_executed_unskipped_calls() -> None:
    unit = make_receipt(a=SKIP_CALL, b=SKIP_CALL, c=SKIP_CALL, d=SKIP_CALL, e=SKIP_CALL)
    alt = make_receipt(
        "pg",
        a=PASS,
        b=SKIP_CALL,  # alternate skipped it too
        c={"setup": ("failed", "boom"), "teardown": ("passed", "")},  # no call executed
        d={"setup": ("passed", ""), "call": ("passed", ""), "teardown": ("skipped", "td")},
    )  # e absent from the alternate
    result = sa.reconcile(unit, [alt])
    assert result.covered == ["a"]
    assert sorted(result.pending) == ["b", "c", "d", "e"]
    assert result.pending["b"]["phases"] == {"call": "no pg"}
    assert result.pending["b"]["alternate_skips"] == [{"check": "pg", "phases": {"call": "no pg"}}]


def test_reconcile_keeps_failed_alternate_call_as_failure() -> None:
    unit = make_receipt(a=SKIP_CALL)
    alt = make_receipt("pg", a={"setup": ("passed", ""), "call": ("failed", "assert"), "teardown": ("passed", "")})
    result = sa.reconcile(unit, [alt])
    assert result.covered == ["a"]
    assert result.failed == [{"id": "a", "check": "pg", "phases": {"call": "assert"}}]


def test_reconcile_rejects_stale_other_run_incomplete_and_uncollected() -> None:
    unit = make_receipt(a=SKIP_CALL)
    stale = make_receipt("pg", fp="other", a=PASS)
    other_run = make_receipt("pg", run="other", a=PASS)
    incomplete = make_receipt("pg", a=PASS)
    incomplete.complete = False
    deselected = make_receipt("pg", a=PASS)
    deselected.selected = []
    result = sa.reconcile(unit, [stale, other_run, incomplete, deselected])
    assert sorted(result.pending) == ["a"] and result.covered == []
    assert [r["reason"] for r in result.rejected] == [
        "stale fingerprint",
        "run_id 'other' != 'r'",
        "incomplete (exitstatus 0)",
    ]
    with pytest.raises(ValueError):
        unit.complete = False
        sa.reconcile(unit, [])


def test_end_to_end_alternate_run_covers_env_gated_node(repo: Path, tmp_path: Path) -> None:
    (repo / "tests" / "test_env.py").write_text(
        "import os, pytest\n"
        "@pytest.mark.skipif(not os.environ.get('HAVE_X'), reason='needs X')\n"
        "def test_env():\n    pass\n"
    )
    _, unit = run_pytest(repo, "tests/test_env.py", out=repo / "u")
    _, with_x = run_pytest(repo, "tests/test_env.py", check="x", out=repo / "x", env={"HAVE_X": "1"})
    _, no_x = run_pytest(repo, "tests/test_env.py", check="nox", out=repo / "nox")
    node = "tests/test_env.py::test_env"

    assert sa.reconcile(only(unit), [only(no_x)]).pending[node]["alternate_skips"] == [
        {"check": "nox", "phases": {"setup": "needs X"}}
    ]
    assert sa.reconcile(only(unit), [only(with_x)]).covered == [node]

    # the same check split across two invocations merges into one receipt, then reconciles
    _, part = run_pytest(repo, "tests/test_env.py", check="x", out=repo / "x", env={"HAVE_X": "1"})
    merged = tmp_path / "merged.json"
    paths = [str(p) for p in sorted((repo / "x").glob(f"*{sa.RECEIPT_SUFFIX}"))]
    assert len(paths) == 2
    done = subprocess.run(
        [sys.executable, "-m", "omp_work.skip_accounting", "merge", "--out", str(merged), *paths],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert sa.reconcile(only(unit), [sa.read_receipt(merged)]).covered == [node]

    # editing a test after the alternate ran makes its receipt stale
    (repo / "tests" / "test_env.py").write_text((repo / "tests" / "test_env.py").read_text() + "# edit\n")
    _, fresh_unit = run_pytest(repo, "tests/test_env.py", out=repo / "u2")
    stale = sa.reconcile(only(fresh_unit), [only(with_x)])
    assert stale.rejected == [{"check": "x", "reason": "stale fingerprint"}] and stale.covered == []


def test_merge_never_hides_phase_failures() -> None:
    ok = make_receipt(a=PASS)
    bad = make_receipt(a={"setup": ("passed", ""), "call": ("failed", "boom"), "teardown": ("passed", "")})
    bad.exitstatus = 1
    merged = sa.merge_receipts([ok, bad])
    assert merged.nodes["a"]["call"] == sa.Phase("failed", "boom")
    assert merged.exitstatus == 1
    with pytest.raises(ValueError):
        sa.merge_receipts([ok, make_receipt(check="other", a=PASS)])


def test_cli_pending_emits_skipped_ids_and_reports_absent_files(tmp_path: Path) -> None:
    unit = make_receipt(**{"tests/a.py::t[1]": SKIP_CALL, "tests/b.py::t": SKIP_CALL})
    alt = make_receipt("pg", **{"tests/a.py::t[1]": PASS})
    (tmp_path / "u.json").write_text(json.dumps(unit.to_json()))
    (tmp_path / "a.json").write_text(json.dumps(alt.to_json()))
    done = subprocess.run(
        [
            sys.executable, "-m", "omp_work.skip_accounting", "pending",
            "--unit", str(tmp_path / "u.json"), "--alternate", str(tmp_path / "a.json"),
            "--files", "tests/b.py", "tests/missing.py",
        ],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert [p["id"] for p in out["pending"]] == ["tests/b.py::t"]
    assert out["pending"][0]["phases"] == {"call": "no pg"}
    assert out["absent"] == ["tests/missing.py"]


def test_read_receipt_rejects_malformed(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "run_id": "r"}))
    with pytest.raises(ValueError):
        sa.read_receipt(path)
    path.write_text(textwrap.dedent('{"version": 2}'))
    with pytest.raises(ValueError):
        sa.read_receipt(path)


def test_fingerprint_tracks_inputs_not_reports_or_changelogs(repo: Path) -> None:
    base = sa.fingerprint(repo)
    (repo / "reports").mkdir()
    (repo / "reports" / "x.json").write_text("1")
    (repo / "CHANGELOG.md").write_text("x")
    (repo / "tests" / "extra.skip-receipt.json").write_text("{}")
    assert sa.fingerprint(repo) == base
    (repo / "tests" / "test_suite.py").write_text(SUITE + "\n# edit\n")
    assert sa.fingerprint(repo) != base
