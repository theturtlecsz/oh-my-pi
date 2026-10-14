"""Execute the composed planner rollout candidate, never the selected external source.

Set OMP584_FLOOD_SOURCE explicitly. A missing source skips every node; rollout
qualification must supply it and run them all. The preflight and the staging
functions install nothing on their own: the owner rollout script stages and
qualifies only in its own temp dir until OMP-584-s03 wires installation.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ASSETS = Path(__file__).resolve().parents[3] / "infra" / "automation-user"
PROMPT_PATCH = ASSETS / "omp584-prompt.patch"
if not PROMPT_PATCH.exists():
    PROMPT_PATCH = ASSETS / "omp584-planner-prompt.patch"
PATCHES = [ASSETS / "omp584-dependencies.patch", ASSETS / "omp584-start-metrics.patch", PROMPT_PATCH]
RULES = "omp584-planner-rules.md"
SPEC_RULES = "planner-independence.md"
ITEM = "OMP-584"
SCRIPT = ASSETS / "omp584-rollout.sh"


def command(argv: list[str], cwd: Path):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=30)


def compose(directory: Path, patches: list[Path]) -> None:
    """Reverse only fully reverse-applicable patches, then check and apply each forward."""
    for patch in reversed(patches):
        reverse = ["git", "apply", "--reverse", str(patch)]
        if command([*reverse, "--check"], directory).returncode == 0:
            result = command(reverse, directory)
            assert result.returncode == 0, result.stderr
    for patch in patches:
        result = command(["git", "apply", "--check", str(patch)], directory)
        assert result.returncode == 0, f"{patch.name}: {result.stderr}"
        result = command(["git", "apply", str(patch)], directory)
        assert result.returncode == 0, result.stderr


def snapshot(directory: Path):
    return {p.relative_to(directory): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in directory.rglob("*") if p.is_file() and p.suffix in (".py", ".pyc", ".md")}


def init_repo(directory: Path) -> str:
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "isolated fixture"],
    ):
        result = command(argv, directory)
        assert result.returncode == 0, result.stderr
    return command(["git", "rev-parse", "HEAD"], directory).stdout.strip()


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    if not selected:
        pytest.skip("rollout qualification requires explicit OMP584_FLOOD_SOURCE")
    source = Path(selected).expanduser().resolve()
    assert (source / "flood.py").is_file(), f"no flood.py in {source}"
    originals = snapshot(source)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    directory = tmp_path / "candidate"
    directory.mkdir()
    for p in source.iterdir():
        if p.is_file() and p.suffix in (".py", ".md"):
            shutil.copy2(p, directory / p.name)
    for name in (RULES, SPEC_RULES):
        shutil.copyfile(ASSETS / RULES, directory / name)
    init_repo(directory)
    compose(directory, PATCHES)
    path = directory / "flood.py"
    compile(path.read_bytes(), str(path), "exec")
    try:
        yield path
    finally:
        assert snapshot(source) == originals, "selected source or bytecode changed"


def slice_text(n: int, depends_on: str, files: str, body: str) -> str:
    return (f"---\nid: {ITEM}-s0{n}\nfiles: [{files}]\ndepends_on: {depends_on}\n---\n"
            f"{body}\nExtend tests/test_{ITEM.replace('-', '_')}_s0{n}.py.\n"
            "Done when: the new test passes.\n")


def check_plan(path: Path, tmp_path: Path, slices: list[str]):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    init_repo(repo)
    cfg_dir = tmp_path / "config"
    pdir = cfg_dir / "tasks" / "proposed" / ITEM
    pdir.mkdir(parents=True)
    for n, text in enumerate(slices, 1):
        (pdir / f"{ITEM}-s0{n}.md").write_text(text)
    cfg_file = cfg_dir / "flood.json"
    cfg_file.write_text(json.dumps({"repo": str(repo), "providers": {}, "control_files": [],
                                    "worktrees_dir": str(tmp_path / "worktrees"),
                                    "rider_dir": str(tmp_path / "rider")}))
    return command([sys.executable, "-B", str(path), "check-plan", ITEM, "--config", str(cfg_file)],
                   path.parent)


@pytest.mark.parametrize("section", ["Do not touch", "Assumptions", "Deferred"])
def test_combined_check_plan_dependency_boundaries(candidate, tmp_path, section):
    """Mentions inside excluded sections must not justify a depends_on; real reads still do."""
    producer = "src/producer.py"
    first = slice_text(1, "[]", producer, f"Implement {producer}.\nDo not touch: unrelated files.")
    excluded = f"Implement src/consumer.py.\n{section}: {producer} belongs to {ITEM}-s01.\n"
    if section != "Do not touch":
        excluded += "Do not touch: unrelated files.\n"

    ordered = check_plan(candidate, tmp_path / "a", [
        first, slice_text(2, f"[{ITEM}-s01]", "src/consumer.py", excluded)])
    assert ordered.returncode == 1, ordered.stdout + ordered.stderr
    assert f"{ITEM}-s02: depends_on {ITEM}-s01 orders the slice only" in ordered.stdout
    assert "names no test file" not in ordered.stdout

    independent = check_plan(candidate, tmp_path / "b", [
        first, slice_text(2, "[]", "src/consumer.py", excluded)])
    assert independent.returncode == 0, independent.stdout + independent.stderr
    assert "OK: 2 slices" in independent.stdout

    reads = check_plan(candidate, tmp_path / "c", [
        first, slice_text(2, f"[{ITEM}-s01]", "src/consumer.py",
                          f"Implement src/consumer.py; it reads {producer}.\n"
                          "Do not touch: unrelated files.\n")])
    assert reads.returncode == 0, reads.stdout + reads.stderr


def test_combined_candidate_compiles_and_requalifies(candidate):
    """Composing an installed candidate again must be a byte-identical no-op."""
    before = candidate.read_bytes()
    compose(candidate.parent, PATCHES)
    assert candidate.read_bytes() == before
    compile(candidate.read_bytes(), str(candidate), "exec")


@pytest.mark.parametrize("continuation", [
    {"attempts": 1, "history": []},
    {"attempts": 0, "history": [{"phase": "plan-review"}]},
])
def test_combined_continuation_prompts_keep_loaded_rules(candidate, tmp_path, continuation):
    """Disk replacement must not change planner or reviewer rules during a retry."""
    marker = f"Loaded marker: {uuid.uuid4().hex}"
    rules = candidate.with_name(RULES)
    loaded = rules.read_bytes() + f"\n{marker}\n".encode()
    rules.write_bytes(loaded)
    spec = importlib.util.spec_from_file_location(f"flood_combined_{uuid.uuid4().hex}", candidate)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "flood.json"
    cfg_file.write_text(json.dumps({"repo": str(repo), "providers": {}, "control_files": [],
                                    "worktrees_dir": str(tmp_path / "worktrees"),
                                    "rider_dir": str(tmp_path / "rider")}))
    flood = module.Flood(json.loads(cfg_file.read_text()), cfg_dir)
    rules.write_text("Replacement marker\n")
    task = {"id": ITEM, "body": "Implement isolated continuation change."}
    st = flood.state.task(task["id"])
    wt, pdir = flood.plan_worktree(task["id"]), flood.proposed_dir(task["id"])
    wt.mkdir(parents=True)
    pdir.mkdir(parents=True)
    (pdir / f"{ITEM}-s01.md").write_text("Slice output: isolated.json\n")
    flood.reviewers = [{"cmd": [sys.executable, "-c", "print('VERDICT: PASS')",
                                "{prompt}", "{prompt_file}"]}]
    children = []
    try:
        for state in ({"attempts": 0, "history": []}, continuation):
            st.update(**state, last_error="Retry required", note="Owner context")
            planner = flood.plan_prompt(task, st, wt, pdir)
            flood.start_plan_review(task)
            child = flood.procs[task["id"]]
            children.append(child)
            assert child.wait(timeout=10) == 0
            review = (flood.logs_dir / f"{ITEM}.plan-review-prompt.{st['attempts']}.md").read_text()
            for prompt in (planner, review):
                assert marker in prompt
                assert task["body"] in prompt
                assert "Replacement marker" not in prompt
            if state is continuation:
                assert "# Continuation" in planner
                assert "Retry required" in planner
                assert "Owner context" in planner
        assert module.PLANNER_RULES_SHA256 == hashlib.sha256(loaded).hexdigest()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_combined_real_spawn_persists_start_hashes_and_attempts(candidate, tmp_path):
    """Real spawns record the loaded code hash and per-spawn attempts, not the edited file's."""
    loaded = hashlib.sha256(candidate.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location(f"flood_combined_{uuid.uuid4().hex}", candidate)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate.write_bytes(candidate.read_bytes() + b"\n# edited after import\n")

    flood = module.Flood.__new__(module.Flood)
    flood.cfg = {"start_gap_sec": 0}
    flood.cfg_dir = tmp_path
    flood.logs_dir = tmp_path / "logs"
    flood.logs_dir.mkdir()
    flood.wt_dir = tmp_path / "wt"
    flood.slots, flood.test_slots, flood.test_lock = 2, 1, None
    flood.providers = {"alpha": {"role": "implement", "max_parallel": 2}}
    flood.fence, flood.control_globs, flood.procs = {}, [], {}
    flood.state = module.State(tmp_path / "state.json")
    flood.proposed_dir = lambda tid: tmp_path / "proposed" / tid
    argv = [sys.executable, "-c", "pass"]
    children = []
    try:
        assert flood.spawn({"id": "P"}, "plan", "alpha", argv, tmp_path, 1)
        children.append(flood.procs["P"])
        children[-1].wait(timeout=10)
        for n in (1, 2):
            assert flood.spawn({"id": "I"}, "implement", "alpha", argv, tmp_path, 1)
            children.append(flood.procs["I"])
            children[-1].wait(timeout=10)
            if n == 1:
                task = flood.state.task("I")
                task["history"].append({"phase": "implement", "rc": 1, "attempt": 1})
                task.update(status="pending", job=None)
    finally:
        for child in children:
            child.wait(timeout=10)

    tasks = json.loads((tmp_path / "state.json").read_text())["tasks"]
    plans = [e for e in tasks["P"]["history"] if e["phase"] == "plan-start"]
    assert [(e["attempt"], e["code_sha256"]) for e in plans] == [(1, loaded)]
    starts = [e for e in tasks["I"]["history"] if e["phase"] == "implement-start"]
    assert [e["attempt"] for e in starts] == [1, 2]
    for event in starts:
        assert event["code_sha256"] == loaded
        for key in ("slots", "running_implementers", "occupied_slots"):
            assert isinstance(event[key], int)
    assert module.CODE_SHA256 == loaded
    assert module.CODE_SHA256 != hashlib.sha256(candidate.read_bytes()).hexdigest()


def selected_source() -> Path:
    """The explicitly selected patched Flood directory, or a graceful skip."""
    selected = os.environ.get("OMP584_FLOOD_SOURCE")
    if not selected:
        pytest.skip("rollout staging requires explicit OMP584_FLOOD_SOURCE")
    source = Path(selected).expanduser().resolve()
    assert (source / "flood.py").is_file(), f"no flood.py in {source}"
    return source


def staged_source(tmp_path: Path) -> Path:
    """A mutable copy of the selected source that a test owns and can corrupt."""
    source = tmp_path / "source"
    source.mkdir()
    selected = selected_source()
    for entry in sorted(selected.iterdir()):
        if entry.is_file() and entry.suffix in (".py", ".md"):
            shutil.copy2(entry, source / entry.name)
    return source


def run_functions(source: Path, evidence: Path, extra: str = "") -> subprocess.CompletedProcess:
    """Source the script and drive preflight/stage/verify, never its installation entry.

    `extra` runs after staging so a test can corrupt the staged candidate only.
    """
    driver = f'''source "$1"
trap 'if [[ -n "${{OMP584_STAGE:-}}" ]]; then rm -rf -- "$OMP584_STAGE"; fi' EXIT
omp584_preflight --flood-dir "$2" --evidence-dir "$3"
omp584_stage
{extra}
omp584_verify
'''
    return subprocess.run(["bash", "-c", driver, "test", str(SCRIPT), str(source), str(evidence)],
                          capture_output=True, text=True, timeout=300)


@pytest.mark.parametrize("args", [[], ["--unknown"], ["--flood-dir"], ["--evidence-dir"],
                                  ["--flood-dir", ""], ["--evidence-dir", ""],
                                  ["--flood-dir", "--evidence-dir", "x"]])
def test_rollout_arguments_fail_before_writes(tmp_path, args):
    """Unknown, missing or empty arguments must fail before any file is created."""
    result = command(["bash", str(SCRIPT), *args], tmp_path)
    assert result.returncode == 2
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("suffix", [["--unknown"], ["--flood-dir"], ["--evidence-dir"],
                                   ["--flood-dir", "duplicate"],
                                   ["--evidence-dir", "duplicate"]])
def test_rollout_late_bad_arguments_fail_before_evidence_writes(tmp_path, suffix):
    """Validation happens before any evidence write, even with valid leading options."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "flood.py").write_text("pass\n")
    before = snapshot(source)
    evidence = tmp_path / "evidence"
    result = command(["bash", str(SCRIPT), "--flood-dir", str(source),
                      "--evidence-dir", str(evidence), *suffix], tmp_path)
    assert result.returncode == 2
    assert not evidence.exists()
    assert snapshot(source) == before


def test_rollout_evidence_symlink_into_source_is_rejected_before_writes(tmp_path):
    """Evidence that resolves inside the supplied checkout would replace source bytes."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "flood.py").write_text("pass\n")
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    result = command(["bash", str(SCRIPT), "--flood-dir", str(source),
                      "--evidence-dir", str(alias / "evidence")], tmp_path)
    assert result.returncode == 2
    assert "overlaps source" in result.stderr
    assert list(source.iterdir()) == [source / "flood.py"]


def test_rollout_failed_staging_preserves_source_index_head(tmp_path):
    """A patch that cannot compose must leave the target, its index and HEAD untouched."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "flood.py").write_text('print("incompatible source")\n')
    (source / "flood.pyc").write_bytes(b"preexisting bytecode")
    head = init_repo(source)
    before = snapshot(source)
    index = (source / ".git" / "index").read_bytes()
    evidence = tmp_path / "evidence"
    result = command(["bash", str(SCRIPT), "--flood-dir", str(source),
                      "--evidence-dir", str(evidence)], tmp_path)
    assert result.returncode != 0
    assert snapshot(source) == before
    assert (source / ".git" / "index").read_bytes() == index
    assert command(["git", "rev-parse", "HEAD"], source).stdout.strip() == head
    assert not evidence.exists()


def test_rollout_functions_stage_and_verify_directly(tmp_path, monkeypatch):
    """Direct preflight+stage+verify compiles the candidate, logs under evidence, and installs nothing."""
    source = staged_source(tmp_path)
    head = init_repo(source)
    before = snapshot(source)
    index = (source / ".git" / "index").read_bytes()
    evidence = tmp_path / "evidence"
    # Nested qualification must not inherit a caller's pytest base directory:
    # pytest clears it at startup, deleting caller-owned fixtures and evidence.
    caller_temp = tmp_path / "caller-pytest"
    caller_temp.mkdir()
    sentinel = caller_temp / "caller-fixture"
    sentinel.write_text("Caller-owned fixture.\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", f"--basetemp={caller_temp}")

    result = run_functions(source, evidence)

    pytest_log = (evidence / "pytest.log").read_text() if (evidence / "pytest.log").exists() else ""
    assert result.returncode == 0, result.stderr + result.stdout + pytest_log
    assert sentinel.read_text() == "Caller-owned fixture.\n"
    assert "Candidate compiles" in (evidence / "compile.log").read_text()
    assert " failed" not in pytest_log
    assert "skipped" not in pytest_log
    # The candidate is composed and installed only in the invocation temp tree.
    assert "test_flood_rollout_provenance.py" in pytest_log
    assert snapshot(source) == before
    assert (source / ".git" / "index").read_bytes() == index
    assert command(["git", "rev-parse", "HEAD"], source).stdout.strip() == head


@pytest.mark.parametrize("failure", ["compile", "pytest"])
def test_rollout_verification_failure_cannot_install(tmp_path, failure):
    """A candidate that fails compile or the combined tests must not install anything."""
    source = staged_source(tmp_path)
    head = init_repo(source)
    before = snapshot(source)
    index = (source / ".git" / "index").read_bytes()
    evidence = tmp_path / "evidence"
    # Corrupt only the staged candidate: a syntax error, or rules the combined
    # tests must reject, without replacing the verification tooling.
    extra = ('printf "\\n%s\\n" "if syntax is broken" >> "$OMP584_STAGE/flood.py"'
             if failure == "compile" else
             'printf "\\nPLANNER_INDEPENDENCE = \'broken policy\'\\n" >> "$OMP584_STAGE/flood.py"')

    result = run_functions(source, evidence, extra=extra)

    assert result.returncode != 0
    if failure == "compile":
        assert "SyntaxError" in (evidence / "compile.log").read_text()
        assert not (evidence / "pytest.log").exists()
    else:
        assert " failed" in (evidence / "pytest.log").read_text()
    assert snapshot(source) == before
    assert (source / ".git" / "index").read_bytes() == index
    assert command(["git", "rev-parse", "HEAD"], source).stdout.strip() == head
