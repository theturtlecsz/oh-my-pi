"""Tests for OMP-622 rollout verification (preflight+stage+verify).

Verifies:
- incremental fixture: exit 0; both logs name test_omp622_exact.py, lack " failed"/"skipped"/test_flood_rollout_provenance.py;
  checkout files/index/HEAD unchanged; caller PYTEST_ADDOPTS --basetemp sentinel survives.
- staged candidate with PLANNER_INDEPENDENCE = 'broken policy' appended after stage: nonzero, " failed" in pytest.log, checkout unchanged.
- syntax error appended: nonzero, SyntaxError in compile.log, no pytest.log.
"""
from __future__ import annotations

from pathlib import Path

from omp622_rollout_helpers import (
    checkout_snapshot,
    create_checkout,
    run_driver,
)


def test_verify_incremental_fixture_passes_exact_only(tmp_path: Path):
    """Incremental rollout verifies via test_omp622_exact.py, preserving caller basetemp and checkout."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"

    caller_temp = tmp_path / "caller-pytest"
    caller_temp.mkdir()
    sentinel = caller_temp / "caller-fixture"
    sentinel.write_text("Caller-owned fixture.\n")
    env["PYTEST_ADDOPTS"] = f"--basetemp={caller_temp}"

    before = checkout_snapshot(source, env=env)

    result = run_driver(
        source,
        evidence,
        functions=["omp584_preflight", "omp584_stage", "omp584_verify"],
        env=env,
    )
    assert result.returncode == 0, f"Driver failed: {result.stderr}\nSTDOUT:\n{result.stdout}"

    pytest_log_file = evidence / "pytest.log"
    pytest_exact_log_file = evidence / "pytest-exact.log"
    assert pytest_log_file.is_file(), "pytest.log missing"
    assert pytest_exact_log_file.is_file(), "pytest-exact.log missing"

    pytest_log = pytest_log_file.read_text(encoding="utf-8")
    pytest_exact_log = pytest_exact_log_file.read_text(encoding="utf-8")

    for log in (pytest_log, pytest_exact_log):
        assert "test_omp622_exact.py" in log
        assert " failed" not in log
        assert "skipped" not in log
        assert "test_flood_rollout_provenance.py" not in log

    assert checkout_snapshot(source, env=env) == before, "Checkout was modified during verify"
    assert sentinel.read_text() == "Caller-owned fixture.\n", "Caller basetemp sentinel was not preserved"


def test_verify_staged_candidate_broken_policy_fails(tmp_path: Path):
    """Staged candidate with broken planner independence policy fails in pytest.log and leaves checkout unchanged."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    before = checkout_snapshot(source, env=env)

    extra = (
        'printf "\\nPLANNER_INDEPENDENCE = \'broken policy\'\\n" >> "$OMP584_STAGE/flood.py"\n'
        'omp584_verify'
    )
    result = run_driver(
        source,
        evidence,
        functions=["omp584_preflight", "omp584_stage"],
        extra=extra,
        env=env,
    )

    assert result.returncode != 0, f"Expected nonzero returncode, got 0:\n{result.stdout}\n{result.stderr}"
    pytest_log_file = evidence / "pytest.log"
    assert pytest_log_file.is_file(), "pytest.log missing on test failure"
    assert " failed" in pytest_log_file.read_text(encoding="utf-8")
    assert checkout_snapshot(source, env=env) == before, "Checkout was modified on verification failure"


def test_verify_staged_candidate_syntax_error_fails_compile(tmp_path: Path):
    """Staged candidate with syntax error fails compile.log without generating pytest.log and leaves checkout unchanged."""
    source, env = create_checkout(tmp_path)
    evidence = tmp_path / "evidence"
    before = checkout_snapshot(source, env=env)

    extra = (
        'printf "\\nif syntax is broken\\n" >> "$OMP584_STAGE/flood.py"\n'
        'omp584_verify'
    )
    result = run_driver(
        source,
        evidence,
        functions=["omp584_preflight", "omp584_stage"],
        extra=extra,
        env=env,
    )

    assert result.returncode != 0, f"Expected nonzero returncode, got 0:\n{result.stdout}\n{result.stderr}"
    compile_log_file = evidence / "compile.log"
    assert compile_log_file.is_file(), "compile.log missing on compile failure"
    assert "SyntaxError" in compile_log_file.read_text(encoding="utf-8")
    assert not (evidence / "pytest.log").exists(), "pytest.log must not exist when compile fails"
    assert checkout_snapshot(source, env=env) == before, "Checkout was modified on compile failure"
