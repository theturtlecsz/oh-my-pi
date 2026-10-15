"""Execute the composed planner rollout candidate, never the selected external source.

Set OMP584_FLOOD_SOURCE explicitly. A missing source skips source-dependent
nodes; rollout qualification must supply it and run them all. The preflight and the staging
functions install nothing on their own: the owner rollout script stages and
qualifies in its own temp dir before target-scoped installation.
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
    path = directory / "flood.py"
    canonical = b'with_name("planner-independence.md")' in path.read_bytes()
    if canonical:
        path.write_bytes(path.read_bytes().replace(b'with_name("planner-independence.md")',
                                                  b'with_name("omp584-planner-rules.md")'))
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
    if canonical:
        path.write_bytes(path.read_bytes().replace(b'with_name("omp584-planner-rules.md")',
                                                  b'with_name("planner-independence.md")'))


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
        if not (directory / name).is_file():
            shutil.copyfile(ASSETS / RULES, directory / name)
    init_repo(directory)
    if os.environ.get("OMP584_TEST_EXACT") != "1":
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
    rules_name = SPEC_RULES if b"planner-independence.md" in candidate.read_bytes() else RULES
    rules = candidate.with_name(rules_name)
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


@pytest.fixture
def install_repo(tmp_path, monkeypatch):
    """Only temporary repos are installation targets; no host service is callable."""
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-config"))
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "flood.py").write_bytes(b"# original flood\n")
    (source / "flood.py").chmod(0o755)
    (source / "staged.txt").write_text("base staged\n")
    (source / "unstaged.txt").write_text("base unstaged\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    service_log = tmp_path / "services"
    for name in ("systemctl", "service", "supervisorctl"):
        tool = tools / name
        tool.write_text(f'#!/bin/sh\necho "$0 $*" >> "{service_log}"\nexit 99\n')
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    yield source
    assert not service_log.exists(), "rollout must rely on in-place reload, never service commands"


def prepare_install_repo(source: Path, rules_present: bool) -> None:
    if rules_present:
        (source / SPEC_RULES).write_bytes(b"Tracked old planner rules\n")
        (source / SPEC_RULES).chmod(0o640)
    init_repo(source)
    for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                       ("commit.gpgsign", "false")):
        assert command(["git", "config", key, value], source).returncode == 0
    (source / "staged.txt").write_text("unrelated staged\n")
    assert command(["git", "add", "staged.txt"], source).returncode == 0
    (source / "staged.txt").write_text("unrelated staged plus unstaged\n")
    (source / "unstaged.txt").write_text("unrelated unstaged\n")
    (source / "untracked.txt").write_text("unrelated untracked\n")
    (source / "flood.py.omp584-new").write_bytes(b"preexisting scratch\n")
    (source / "planner-independence.md.omp584-new.OLD").write_bytes(b"another user's scratch\n")


def transaction_state(source: Path):
    return {
        "head": command(["git", "rev-parse", "HEAD"], source).stdout,
        "index": (source / ".git" / "index").read_bytes(),
        "entries": command(["git", "ls-files", "--stage"], source).stdout,
        "files": {p.name: (p.read_bytes(), p.stat().st_mode & 0o777)
                  for p in source.iterdir() if p.is_file()},
    }


def run_install(source: Path, evidence: Path, seam: str = "", *, main: bool = False):
    """Known tested bytes isolate transaction tests from external Flood availability."""
    driver = r'''source "$1"
omp584_stage() {
    OMP584_STAGE=$(mktemp -d)
    cp -p "$OMP584_FLOOD_DIR/flood.py" "$OMP584_STAGE/original-flood.py"
    printf '# qualified flood\n' > "$OMP584_STAGE/flood.py"
    cp "$OMP584_ASSETS/omp584-planner-rules.md" "$OMP584_STAGE/planner-independence.md"
}
omp584_verify() {
    mkdir -p "$OMP584_EVIDENCE_DIR"
    cp "$OMP584_STAGE/flood.py" "$OMP584_EVIDENCE_DIR/tested-flood.py"
    cp "$OMP584_STAGE/planner-independence.md" "$OMP584_EVIDENCE_DIR/tested-rules.md"
    echo "compiled" > "$OMP584_EVIDENCE_DIR/compile.log"
    echo "tested" > "$OMP584_EVIDENCE_DIR/pytest.log"
}
'''
    driver += seam + "\n"
    if main:
        driver += 'omp584_main --flood-dir "$2" --evidence-dir "$3"\n'
    else:
        driver += r'''trap 'rm -rf -- "${OMP584_STAGE:-}"' EXIT
omp584_preflight --flood-dir "$2" --evidence-dir "$3"
omp584_stage
omp584_verify
omp584_install
'''
    return subprocess.run(["bash", "-c", driver, "test", str(SCRIPT), str(source), str(evidence)],
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("rules_present", [True, False])
@pytest.mark.parametrize("failure", ["first-replace", "second-replace", "commit"])
def test_rollout_transaction_failure_restores_targets_and_preserves_dirt(
        install_repo, tmp_path, rules_present, failure):
    """Failed install/commit preserves HEAD, exact index, old rules/modes and unrelated dirt."""
    source = install_repo
    prepare_install_repo(source, rules_present)
    seam = ""
    if failure == "commit":
        hook = source / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 47\n")
        hook.chmod(0o755)
    elif failure == "first-replace":
        seam = 'omp584_replace() { return 48; }'
    else:
        # New rules must be visible while flood.py still holds old bytes.
        seam = r'''omp584_replace() {
    if [[ "$2" == */flood.py ]]; then
        cmp "$OMP584_STAGE/planner-independence.md" "$OMP584_FLOOD_DIR/planner-independence.md" || exit 91
        cmp "$OMP584_STAGE/original-flood.py" "$OMP584_FLOOD_DIR/flood.py" || exit 92
        echo 'rules replaced first' >&2
        return 49
    fi
    mv -f -- "$1" "$2"
}'''
    before = transaction_state(source)
    result = run_install(source, tmp_path / "evidence", seam)
    assert result.returncode == {"first-replace": 48, "second-replace": 49, "commit": 1}[failure], result.stderr
    if failure == "second-replace":
        assert "rules replaced first" in result.stderr
    assert transaction_state(source) == before
    assert (source / SPEC_RULES).exists() == rules_present


@pytest.mark.parametrize("target", ["flood.py", SPEC_RULES])
@pytest.mark.parametrize("dirt", ["staged", "unstaged", "untracked"])
def test_rollout_dirty_target_fails_before_writes(install_repo, tmp_path, target, dirt):
    """Target dirt is never overwritten or staged, including untracked rules."""
    source = install_repo
    prepare_install_repo(source, target == SPEC_RULES and dirt != "untracked")
    if dirt == "untracked" and target == "flood.py":
        assert command(["git", "rm", "--cached", "flood.py"], source).returncode == 0
    else:
        (source / target).write_bytes(b"operator edits\n")
        if dirt == "staged":
            assert command(["git", "add", target], source).returncode == 0
    before = transaction_state(source)
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert not evidence.exists()
    assert transaction_state(source) == before


def test_rollout_rechecks_target_dirt_after_qualification(install_repo, tmp_path):
    """Edits made during tests survive and block installation before replacements."""
    source = install_repo
    prepare_install_repo(source, True)
    seam = r'''omp584_verify() {
    printf 'operator edit during qualification\n' > "$OMP584_FLOOD_DIR/planner-independence.md"
}'''
    before = transaction_state(source)
    result = run_install(source, tmp_path / "evidence", seam, main=True)
    assert result.returncode != 0
    before["files"][SPEC_RULES] = (b"operator edit during qualification\n", 0o640)
    assert transaction_state(source) == before


@pytest.mark.parametrize("rules_present", [True, False])
def test_rollout_commits_only_tested_targets_without_claiming_activation(install_repo, tmp_path, rules_present):
    """Target commit keeps unrelated staging; CLI fails until activation verification exists."""
    source = install_repo
    prepare_install_repo(source, rules_present)
    before = transaction_state(source)
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode == 1, result.stderr
    assert "activation verification pending" in result.stderr
    assert not (evidence / "rollout.json").exists()
    changed = command(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], source)
    assert set(changed.stdout.splitlines()) == {"flood.py", SPEC_RULES}
    for target, tested in (("flood.py", "tested-flood.py"), (SPEC_RULES, "tested-rules.md")):
        committed = subprocess.run(["git", "show", f"HEAD:{target}"], cwd=source, capture_output=True, check=True)
        assert committed.stdout == (evidence / tested).read_bytes() == (source / target).read_bytes()
    after = transaction_state(source)
    assert after["head"] != before["head"]
    for name, state in before["files"].items():
        if name not in ("flood.py", SPEC_RULES):
            assert after["files"][name] == state
    assert set(after["files"]) == set(before["files"]) | {SPEC_RULES}
    assert (source / "flood.py").stat().st_mode & 0o777 == 0o755
    assert (source / SPEC_RULES).stat().st_mode & 0o777 == (0o640 if rules_present else 0o644)
    assert command(["git", "diff", "--cached", "--name-only"], source).stdout.splitlines() == ["staged.txt"]
    assert command(["git", "show", ":staged.txt"], source).stdout == "unrelated staged\n"
    assert not command(["git", "diff", "HEAD", "--", "flood.py", SPEC_RULES], source).stdout


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_rollout_hidden_target_edits_are_rejected(install_repo, tmp_path, flag):
    """Index flags cannot let status conceal target bytes that would be overwritten."""
    source = install_repo
    prepare_install_repo(source, True)
    assert command(["git", "update-index", flag, "flood.py"], source).returncode == 0
    (source / "flood.py").write_bytes(b"hidden operator edit\n")
    before = transaction_state(source)
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert not evidence.exists()
    assert transaction_state(source) == before


def test_rollout_ignored_untracked_rules_are_rejected(install_repo, tmp_path):
    """Ignored rules are still existing operator data, never an absent target."""
    source = install_repo
    prepare_install_repo(source, False)
    (source / ".git" / "info" / "exclude").write_text(SPEC_RULES + "\n")
    (source / SPEC_RULES).write_bytes(b"ignored rules\n")
    before = transaction_state(source)
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert not evidence.exists()
    assert transaction_state(source) == before


def test_rollout_requires_git_checkout_root(install_repo, tmp_path):
    """Non-Git directory and nested source directory fail before staging or evidence writes."""
    source = install_repo
    evidence = tmp_path / "evidence"
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert not evidence.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    prepare_install_repo(source, False)
    nested = source / "nested"
    nested.mkdir()
    (nested / "flood.py").write_bytes(b"nested source\n")
    before_repo = transaction_state(source)
    result = run_install(nested, evidence, main=True)
    assert result.returncode != 0
    assert not evidence.exists()
    assert transaction_state(source) == before_repo
    assert list(nested.iterdir()) == [nested / "flood.py"]
    assert (nested / "flood.py").read_bytes() == b"nested source\n"


def test_rollout_post_commit_failure_does_not_undo_deployment(install_repo, tmp_path):
    """Index synchronization failure after commit must not revert deployed files or HEAD."""
    source = install_repo
    prepare_install_repo(source, True)
    before = transaction_state(source)
    seam = r'''git() {
    for arg in "$@"; do
        if [[ "$arg" == reset ]]; then return 53; fi
    done
    command git "$@"
}'''
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, seam, main=True)
    assert result.returncode == 53, result.stderr
    after = transaction_state(source)
    assert after["head"] != before["head"]
    assert after["index"] == before["index"]
    assert set(after["files"]) == set(before["files"])
    for target, tested in (("flood.py", "tested-flood.py"), (SPEC_RULES, "tested-rules.md")):
        committed = subprocess.run(["git", "show", f"HEAD:{target}"], cwd=source, capture_output=True, check=True)
        assert committed.stdout == (source / target).read_bytes() == (evidence / tested).read_bytes()
    assert not (evidence / "rollout.json").exists()


def test_rollout_git_filter_cannot_commit_untested_bytes(install_repo, tmp_path):
    """A clean filter that changes candidate bytes fails and restores the transaction."""
    source = install_repo
    prepare_install_repo(source, True)
    (source / ".git" / "info" / "attributes").write_text("flood.py filter=rollout-test\n")
    assert command(["git", "config", "filter.rollout-test.clean", "sed s/qualified/untested/"], source).returncode == 0
    before = transaction_state(source)
    result = run_install(source, tmp_path / "evidence", main=True)
    assert result.returncode == 2, result.stderr
    assert "Git filtering changed tested bytes" in result.stderr
    assert transaction_state(source) == before


def install_reload_hook(source: Path, *, code_sha: str | None = None,
                        rules_sha: str | None = None, commit: str | None = None,
                        at: str = "2026-10-14T12:00:00Z", pid: int = 1234,
                        mutate_after: str | None = None) -> None:
    """Foreground git post-commit hook simulating in-place reload and loaded record."""
    hooks = source / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    post_commit = hooks / "post-commit"
    script = ['#!/bin/sh', 'set -e', 'head_commit=$(git rev-parse HEAD)']
    if code_sha is not None:
        script.append(f'code_sha="{code_sha}"')
    else:
        script.append('code_sha=$(sha256sum flood.py | awk \'{print $1}\')')
    if rules_sha is not None:
        script.append(f'rules_sha="{rules_sha}"')
    else:
        script.append(f'rules_sha=$(sha256sum {SPEC_RULES} | awk \'{{print $1}}\')')
    if commit is not None:
        script.append(f'record_head="{commit}"')
    else:
        script.append('record_head="$head_commit"')

    script.append(f'echo "{at} [reload] flood.py or flood.json changed: exec in place (running jobs continue)" >> flood.log')
    script.append(f'echo "{at} loaded {{\\"event\\": \\"loaded\\", \\"at\\": \\"{at}\\", \\"pid\\": {pid}, \\"code_sha256\\": \\"$code_sha\\", \\"rules_sha256\\": \\"$rules_sha\\", \\"external_head\\": \\"$record_head\\"}}" >> flood.log')
    if mutate_after == "flood.py":
        script.append('echo "# drifted after activation" >> flood.py')
    elif mutate_after == SPEC_RULES:
        script.append('echo "drifted rules after activation" >> ' + SPEC_RULES)
    post_commit.write_text("\n".join(script) + "\n")
    post_commit.chmod(0o755)


def test_rollout_fast_reload_creates_rollout_json_with_proof(install_repo, tmp_path):
    """Fast reload captured from pre-install snapshot proves activation and writes rollout.json."""
    source = install_repo
    prepare_install_repo(source, True)
    (source / "flood.log").write_text("2026-10-14T09:00:00Z flood: old log line 1\n2026-10-14T09:00:01Z flood: old log line 2\n")

    timestamp = "2026-10-14T12:00:00Z"
    install_reload_hook(source, at=timestamp, pid=54321)

    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode == 0, result.stderr
    assert "Rollout completed successfully" in result.stdout
    assert "activation verification pending" in result.stderr

    rollout_path = evidence / "rollout.json"
    assert rollout_path.is_file()
    rollout = json.loads(rollout_path.read_text())

    head_commit = command(["git", "rev-parse", "HEAD"], source).stdout.strip()
    repo_commit = command(["git", "rev-parse", "HEAD"], SCRIPT.parents[2]).stdout.strip()
    code_bytes = (source / "flood.py").read_bytes()
    rules_bytes = (source / SPEC_RULES).read_bytes()
    expected_code_sha = hashlib.sha256(code_bytes).hexdigest()
    expected_rules_sha = hashlib.sha256(rules_bytes).hexdigest()

    assert rollout["activated_at"] == timestamp
    assert rollout["flood_sha256"] == expected_code_sha
    assert rollout["external_commit"] == head_commit
    assert rollout["repo_commit"] == repo_commit
    assert rollout["matched_record"] == {
        "event": "loaded", "at": timestamp, "pid": 54321,
        "code_sha256": expected_code_sha, "rules_sha256": expected_rules_sha,
        "external_head": head_commit
    }
    assert rollout["installed_file_hashes"]["flood.py"] == expected_code_sha
    assert rollout["installed_file_hashes"][SPEC_RULES] == expected_rules_sha

    for name, sha in rollout["artifact_hashes"].items():
        assert hashlib.sha256((ASSETS / name).read_bytes()).hexdigest() == sha

    assert Path(rollout["compile_log"]).is_file()
    assert Path(rollout["pytest_log"]).is_file()
    assert Path(rollout["reload_log"]).is_file()

    reload_text = (evidence / "reload.log").read_text()
    assert "[reload] flood.py or flood.json changed: exec in place" in reload_text
    assert "loaded" in reload_text

    assert code_bytes == (evidence / "tested-flood.py").read_bytes()
    assert rules_bytes == (evidence / "tested-rules.md").read_bytes()
    blob_flood = command(["git", "show", "HEAD:flood.py"], source).stdout.encode()
    blob_rules = command(["git", "show", f"HEAD:{SPEC_RULES}"], source).stdout.encode()
    assert blob_flood == code_bytes
    assert blob_rules == rules_bytes


def test_rollout_stale_config_reload_between_preparation_and_commit_fails(install_repo, tmp_path, monkeypatch):
    """A config-only reload that occurred before commit cannot prove candidate activation."""
    source = install_repo
    prepare_install_repo(source, True)
    monkeypatch.setenv("OMP584_POLL_TIMEOUT", "1")

    old_head = command(["git", "rev-parse", "HEAD"], source).stdout.strip()
    seam = f'''omp584_replace() {{
    mv -f -- "$1" "$2"
    if [[ "$2" == */flood.py ]]; then
        printf "%s\\n%s\\n" \
            "2026-10-14T11:00:00Z [reload] flood.py or flood.json changed: exec in place (running jobs continue)" \
            "2026-10-14T11:00:00Z loaded {{\\"event\\": \\"loaded\\", \\"at\\": \\"2026-10-14T11:00:00Z\\", \\"pid\\": 1111, \\"code_sha256\\": \\"stale_code\\", \\"rules_sha256\\": \\"stale_rules\\", \\"external_head\\": \\"{old_head}\\"}}" \
            >> "$OMP584_FLOOD_DIR/flood.log"
    fi
}}'''
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, seam=seam, main=True)
    assert result.returncode != 0
    assert not (evidence / "rollout.json").exists()


@pytest.mark.parametrize("mismatch", ["code_sha", "rules_sha", "commit"])
def test_rollout_wrong_hashes_or_commit_rejected(install_repo, tmp_path, mismatch, monkeypatch):
    """Reload record with wrong code hash, rules hash, or commit cannot prove activation."""
    source = install_repo
    prepare_install_repo(source, True)
    monkeypatch.setenv("OMP584_POLL_TIMEOUT", "1")

    kwargs = {}
    if mismatch == "code_sha":
        kwargs["code_sha"] = "0" * 64
    elif mismatch == "rules_sha":
        kwargs["rules_sha"] = "1" * 64
    else:
        kwargs["commit"] = "2" * 40

    install_reload_hook(source, **kwargs)
    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert not (evidence / "rollout.json").exists()


def test_rollout_activation_timeout_fails_without_rollout_json(install_repo, tmp_path, monkeypatch):
    """Timeout waiting for activation fails without creating rollout.json."""
    source = install_repo
    prepare_install_repo(source, True)
    monkeypatch.setenv("OMP584_POLL_TIMEOUT", "1")

    hooks = source / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "post-commit").write_text("#!/bin/sh\necho 'flood: standard tick' >> flood.log\n")
    (hooks / "post-commit").chmod(0o755)

    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert "Activation timeout" in result.stderr
    assert not (evidence / "rollout.json").exists()


@pytest.mark.parametrize("target", ["flood.py", SPEC_RULES])
def test_rollout_mutation_during_wait_fails_without_rollout_json(install_repo, tmp_path, target):
    """Disk mutation of code or rules during wait invalidates activation proof."""
    source = install_repo
    prepare_install_repo(source, True)

    install_reload_hook(source, mutate_after=target)

    evidence = tmp_path / "evidence"
    result = run_install(source, evidence, main=True)
    assert result.returncode != 0
    assert "drifted after activation wait" in result.stderr
    assert not (evidence / "rollout.json").exists()
