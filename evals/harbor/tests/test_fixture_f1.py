"""Fixture F1 end to end: seed, solution, Harbor task layout, digest, grading.

The fixture is a repair for the OMP-245 revise_work bug class: the seed makes
``createWorkBackend.reviseWork`` description-only, dropping a caller's ``scope``
and ``acceptance_criteria``; ``solution.patch`` is its exact reverse. These
tests prove the seeded regression is real (the named bun test fails seeded and
passes repaired), that seed plus solution restores the HEAD tree hash, that the
fixture loads with a stable digest and a topology-qualified Harbor task layout,
and that the grader scores an amended readback as pass but a description-only
revision as the expected seeded failure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from harbor.models.task.task import Task

from omp_harbor_eval import EvidenceWriter, IndependentTest, grade, load_fixture
from omp_harbor_eval.grader import SEEDED_LABEL
from omp_harbor_eval.isolation import check_topology, load_compose

HARBOR_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = HARBOR_DIR.parent.parent
FIXTURE_DIR = HARBOR_DIR / "fixtures" / "f1"
TARGET = "session-system/tests/workflow-revise.test.ts"
RUNNER = "bun"
SERVICE_READBACK = "service-readback.json"
WORK_ID = "00000000-0000-7000-8000-000000000001"
OUTCOME = "outcome.json"
TRANSCRIPT = "transcript.jsonl"
INDEPENDENT_TESTS = "independent-tests.json"

# The graded transcript records one refused revise_work: the second attempt
# reuses the first attempt's expected_revision_id, so the host refuses it as a
# stale revision. The fixture pins the refusal count at exactly one.
TRANSCRIPT_RECORDS = 1
STALE_REFUSAL = {
    "decision": "revise_work",
    "refused": "revision_conflict",
    "expected_revision_id": "00000000-0000-7000-8000-000000000010",
    "text": (
        "REFUSED — revision conflict: the item moved to revision "
        "00000000-0000-7000-8000-000000000020 since the preview was generated."
    ),
}


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)


def _bun_report(result: subprocess.CompletedProcess[str]) -> str:
    """bun writes its progress report to stderr and its banner to stdout."""

    return result.stdout + result.stderr


def _git(cwd: Path, *args: str) -> str:
    result = _run(["git", *args], cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    """A detached worktree of HEAD with this checkout's node_modules linked in.

    ``git worktree add`` shares the object store but not the working directory,
    so the seeded tree is exercised without touching this checkout. node_modules
    is a worktree-local symlink to this checkout's install (never a copy); the
    link and the worktree are removed at teardown.
    """

    worktree = tmp_path / "wt"
    result = _run(["git", "worktree", "add", "--detach", str(worktree), "HEAD"], REPO_ROOT)
    assert result.returncode == 0, result.stderr
    link = worktree / "node_modules"
    link.symlink_to(REPO_ROOT / "node_modules", target_is_directory=True)
    try:
        yield worktree
    finally:
        link.unlink(missing_ok=True)
        _run(["git", "worktree", "remove", "--force", str(worktree)], REPO_ROOT)


def _apply(worktree: Path, patch: str) -> subprocess.CompletedProcess[str]:
    return _run(["git", "apply", str(FIXTURE_DIR / patch)], worktree)


def test_seed_applies_to_head_and_touches_only_work_ts(probe: Path) -> None:
    assert _git(probe, "status", "--porcelain") == ""
    applied = _apply(probe, "seed.patch")
    assert applied.returncode == 0, applied.stderr
    assert _git(probe, "status", "--porcelain", "--", "session-system").splitlines() == [
        "M session-system/extensions/workflow/work.ts"
    ]


def test_seeded_tree_fails_the_named_contract(probe: Path) -> None:
    assert _apply(probe, "seed.patch").returncode == 0
    result = _run(["bun", "test", TARGET], probe)
    assert result.returncode != 0, result.stdout
    combined = _bun_report(result)
    assert "createWorkBackend.reviseWork preserves unamended fields" in combined
    assert "amended/scope" in combined


def test_repaired_tree_passes_and_restores_the_head_tree(probe: Path) -> None:
    head_tree = _git(probe, "rev-parse", "HEAD^{tree}")
    assert _apply(probe, "seed.patch").returncode == 0
    assert _apply(probe, "solution.patch").returncode == 0

    result = _run(["bun", "test", TARGET], probe)
    assert result.returncode == 0, _bun_report(result)
    assert "0 fail" in _bun_report(result)

    # seed+solution restore HEAD exactly: stage the repaired worktree into the
    # probe's own index and compare its tree object to HEAD's.
    assert _git(probe, "add", "-A") == ""
    assert _git(probe, "write-tree") == head_tree


def test_fixture_loads_with_a_stable_digest_and_pinned_rules() -> None:
    fixture = load_fixture(FIXTURE_DIR.parent, "f1")
    assert fixture.id == "f1"
    assert fixture.scored_experiment == "repair"
    assert fixture.seed_patch == "seed.patch"
    assert fixture.solution_patch == "solution.patch"
    assert fixture.independent_tests == (IndependentTest(runner=RUNNER, target=TARGET),)
    assert fixture.scenario.command == "/execute OMP-1"
    assert fixture.scenario.terminal.pointer == "/work_item/revision/revision_number"
    assert fixture.scenario.terminal.in_ == (2,)
    assert fixture.scenario.timeout_s == 60
    assert [rule["type"] for rule in fixture.rules] == [
        "readback_equals",
        "transcript_count",
        "readback_equals",
        "independent_tests_pass",
    ]
    assert fixture.rules[0]["pointer"] == "/work_item/revision/acceptance_criteria"
    assert fixture.rules[0]["value"] == ["Amended AC 1", "Amended AC 2"]
    assert fixture.rules[1]["count"] == TRANSCRIPT_RECORDS
    assert fixture.rules[2]["pointer"] == "/work_item/candidate"
    assert fixture.rules[2]["value"] is None

    before = fixture.digest
    scenario = FIXTURE_DIR / "scenario.json"
    original = scenario.read_bytes()
    try:
        scenario.write_bytes(original + b"\n")
        assert load_fixture(FIXTURE_DIR.parent, "f1").digest != before
    finally:
        scenario.write_bytes(original)
    assert load_fixture(FIXTURE_DIR.parent, "f1").digest == before


def test_scenario_scripts_match_their_authored_sidecars() -> None:
    fixture = load_fixture(FIXTURE_DIR.parent, "f1")
    ui_script = json.loads((FIXTURE_DIR / "ui-script.json").read_text(encoding="utf-8"))
    model_script = json.loads((FIXTURE_DIR / "model-script.json").read_text(encoding="utf-8"))

    # scenario.json is what the grader digests and what netns stages. The
    # authored sidecars must be those same rules or the fixture would drift.
    assert ui_script == list(fixture.scenario.ui_script)
    assert model_script == list(fixture.scenario.model_script)

    # The amendment, its confirmation, then the stale retry and its confirmation.
    calls = [
        step["tool_calls"][0]["arguments"]
        for step in fixture.scenario.model_script
        if "tool_calls" in step
    ]
    assert [call.get("confirm") for call in calls] == [None, True, None, True]
    assert calls[0]["expected_revision_id"] == calls[2]["expected_revision_id"] == calls[3]["expected_revision_id"]
    assert calls[0]["scope"] == calls[1]["scope"] == "amended/scope"
    assert calls[0]["acceptance_criteria"] == ["Amended AC 1", "Amended AC 2"]
    assert calls[2]["scope"] == calls[3]["scope"] == "stale/scope"
    assert calls[1]["confirmation_id"] == calls[3]["confirmation_id"] == "$confirmation_id"
    assert ui_script == [{"method": "confirm", "title": "Run work?", "answer": {"confirmed": True}}]


def test_compose_is_qualified() -> None:
    compose = load_compose(FIXTURE_DIR / "environment" / "docker-compose.yaml")
    assert check_topology(compose) == []


def test_task_layout_is_valid_and_instruction_leaks_no_harness_hints() -> None:
    assert Task.is_valid_dir(FIXTURE_DIR) is True
    task = Task(FIXTURE_DIR)
    assert task.paths.discovered_test_path is not None
    assert task.paths.discovered_solve_path is not None
    assert task.config.schema_version == "1.4"

    instruction = task.instruction.lower()
    assert "acceptance criteria" in instruction
    for hint in ("seed.patch", "solution.patch", "workflow-revise", "pytest", "grader", "test.sh"):
        assert hint not in instruction


def _write_evidence(
    directory: Path,
    fixture,
    *,
    variant: str,
    run_id: str,
    nonce: str,
    criteria: list[str],
    candidate: str | None = None,
) -> str:
    writer = EvidenceWriter(
        directory,
        run_id,
        nonce,
        fixture.id,
        fixture.digest,
        variant,
        fixture.scored_experiment,
    )
    # service-readback.json is the probe's {"health", "execution", "work_item"}
    # document, so the amendment is read back from the work_item the service
    # returns, not from a scenario-authored file.
    writer.write_json(
        SERVICE_READBACK,
        {
            "health": {"ready": True},
            "execution": {"items": [{"work_id": WORK_ID}]},
            "work_item": {
                "revision": {"revision_number": 2, "acceptance_criteria": criteria},
                "candidate": candidate,
            },
        },
    )
    writer.write_json(OUTCOME, {"outcome": "completed"})
    writer.append_jsonl(TRANSCRIPT, STALE_REFUSAL)
    writer.write_json(INDEPENDENT_TESTS, [{"runner": RUNNER, "target": TARGET, "passed": True}])
    return writer.seal()


def test_grading_separates_the_amendment_from_the_description_only_seed(tmp_path: Path) -> None:
    fixture = load_fixture(FIXTURE_DIR.parent, "f1")

    good = tmp_path / "good"
    good_hash = _write_evidence(
        good,
        fixture,
        variant="known_good",
        run_id="run-good",
        nonce="nonce-good",
        criteria=["Amended AC 1", "Amended AC 2"],
    )
    assert grade(good, good_hash, fixture, run_id="run-good", nonce="nonce-good") == {
        "status": "pass",
        "label": "pass",
        "reasons": [],
    }

    bad = tmp_path / "bad"
    bad_hash = _write_evidence(
        bad,
        fixture,
        variant="known_bad",
        run_id="run-bad",
        nonce="nonce-bad",
        criteria=["Initial AC 1"],
    )
    failed = grade(bad, bad_hash, fixture, run_id="run-bad", nonce="nonce-bad")
    assert failed["status"] == "fail"
    assert failed["label"] == SEEDED_LABEL
    assert failed["reasons"] == [
        "readback_equals: /work_item/revision/acceptance_criteria "
        'expected ["Amended AC 1","Amended AC 2"] got ["Initial AC 1"]'
    ]

    stale_candidate = tmp_path / "stale-candidate"
    stale_hash = _write_evidence(
        stale_candidate,
        fixture,
        variant="known_good",
        run_id="run-stale",
        nonce="nonce-stale",
        criteria=["Amended AC 1", "Amended AC 2"],
        candidate="00000000-0000-7000-8000-000000000030",
    )
    stale = grade(stale_candidate, stale_hash, fixture, run_id="run-stale", nonce="nonce-stale")
    assert stale["status"] == "fail"
    assert stale["reasons"] == [
        "readback_equals: /work_item/candidate expected null got "
        '"00000000-0000-7000-8000-000000000030"'
    ]
