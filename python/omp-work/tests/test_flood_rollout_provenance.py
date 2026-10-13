"""Execute the composed rollout candidate, never the selected external source.

Set OMP584_FLOOD_SOURCE explicitly. Missing source skips ordinary repository
runs; rollout qualification must supply it and execute all real-source tests.
"""
from __future__ import annotations

import datetime as dt
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
PATCHES = [ASSETS / name for name in (
    "omp584-dependencies.patch", "omp584-start-metrics.patch", "omp584-planner-prompt.patch",
)]
RULES = "omp584-planner-rules.md"


def command(argv: list[str], cwd: Path):
    # subprocess.run kills and reaps its own child on timeout.
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20)


def compose(directory: Path, patches: list[Path]) -> None:
    """Normalize only fully reverse-applicable patches, then apply atomically.

    git apply has no patch(1) fuzz or partial writes. A partial installation or
    changed hunk must fail before importing any candidate code.
    """
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
            for p in directory.rglob("*")
            if p.is_file() and (p.suffix in (".py", ".pyc", ".md"))}


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
    source = Path(selected).resolve()
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
    # Support imports and Markdown stay beside the copied source. No bytecode
    # from the selected checkout is loaded, generated or rewritten there.
    for p in source.iterdir():
        if p.is_file() and p.suffix in (".py", ".md"):
            shutil.copy2(p, directory / p.name)
    shutil.copyfile(ASSETS / RULES, directory / RULES)
    init_repo(directory)
    compose(directory, PATCHES)
    path = directory / "flood.py"
    compile(path.read_bytes(), str(path), "exec")
    head = init_repo(directory)
    try:
        yield path, head
    finally:
        assert snapshot(source) == originals, "selected source or bytecode changed"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(f"rollout_{uuid.uuid4().hex}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def isolated_flood(module, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = {"repo": str(repo), "providers": {}, "control_files": [],
           "worktrees_dir": str(tmp_path / "worktrees"), "rider_dir": str(tmp_path / "rider")}
    cfg_file = cfg_dir / "flood.json"
    cfg_file.write_text(json.dumps(cfg))
    flood = module.Flood(cfg, cfg_dir)
    return flood, cfg_file


def loaded_records(output: str):
    return [json.loads(line.split(" loaded ", 1)[1])
            for line in output.splitlines() if " loaded " in line]


def test_run_records_loaded_bytes_despite_disk_edits(candidate, tmp_path, monkeypatch, capsys):
    """Activation must identify executed code and rules, not later disk contents."""
    path, external_head = candidate
    code_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    rules = path.with_name(RULES)
    rules_hash = hashlib.sha256(rules.read_bytes()).hexdigest()
    module = load(path)
    flood, _ = isolated_flood(module, tmp_path)
    assert loaded_records(capsys.readouterr().out) == []  # import/initialization are not activation
    path.write_text(path.read_text() + "\n# replacement on disk after import\n")
    rules.write_text("Replacement rules after import.\n")
    # Run the real entry and loop once, isolating scheduling from host services.
    # The tick persists isolated state, proving entry proceeds into the loop.
    monkeypatch.setattr(flood, "tick", flood.state.save)
    before = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    assert flood.run(once=True) == 0
    after = dt.datetime.now(dt.timezone.utc)
    records = loaded_records(capsys.readouterr().out)
    assert len(records) == 1
    record = records[0]
    assert record == {"event": "loaded", "at": record["at"], "pid": os.getpid(),
                      "code_sha256": code_hash, "rules_sha256": rules_hash,
                      "external_head": external_head}
    assert record["at"].endswith("Z")
    assert before <= dt.datetime.fromisoformat(record["at"]) <= after
    assert record["code_sha256"] != hashlib.sha256(path.read_bytes()).hexdigest()
    assert record["rules_sha256"] != hashlib.sha256(rules.read_bytes()).hexdigest()
    assert json.loads(flood.state.path.read_text())["tasks"] == {}


@pytest.mark.parametrize("continuation", [
    {"attempts": 1, "history": []},
    {"attempts": 0, "history": [{"phase": "plan-review"}]},
])
def test_planner_and_reviewer_share_loaded_rules_on_continuations(candidate, tmp_path, continuation):
    """A disk replacement must not split planner/reviewer policy mid-process."""
    path, _ = candidate
    rules = path.with_name(RULES)
    # CRLF and Unicode defend hashing the original bytes rather than read_text's
    # newline-normalized/re-encoded representation.
    loaded = rules.read_bytes().replace(b"\n", b"\r\n") + "\r\nLoaded marker: 独立.\r\n".encode()
    rules.write_bytes(loaded)
    module = load(path)
    flood, _ = isolated_flood(module, tmp_path)
    task = {"id": "OMP-584", "body": "Implement isolated change."}
    st = flood.state.task(task["id"])
    wt, pdir = flood.plan_worktree(task["id"]), flood.proposed_dir(task["id"])
    wt.mkdir(parents=True)
    pdir.mkdir(parents=True)
    (pdir / "OMP-584-s01.md").write_text("Slice output: isolated.json")
    reviewer = {"cmd": [sys.executable, "-c", "print('VERDICT: PASS')", "{prompt}", "{prompt_file}"]}
    flood.reviewers = [reviewer]
    children = []
    rules.write_text("Replacement marker: must not enter prompts.\n")
    try:
        for state in ({"attempts": 0, "history": []}, continuation):
            st.update(**state, last_error="Retry required", note="Owner context")
            planner = flood.plan_prompt(task, st, wt, pdir)
            flood.start_plan_review(task)
            child = flood.procs[task["id"]]
            children.append(child)
            assert child.wait(timeout=10) == 0
            review = (flood.logs_dir / f"OMP-584.plan-review-prompt.{st['attempts']}.md").read_text()
            for prompt in (planner, review):
                assert loaded.decode().replace("\r\n", "\n") in prompt.replace("\r\n", "\n")
                assert "Replacement marker" not in prompt
                assert task["body"] in prompt
            if state is continuation:
                assert "# Continuation" in planner
                assert st["last_error"] in planner
                assert st["note"] in planner
        assert module.PLANNER_RULES_SHA256 == hashlib.sha256(loaded).hexdigest()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()  # only the exact child this test started
            child.wait(timeout=10)


def test_check_plan_cli_emits_no_activation_record(candidate, tmp_path):
    """Read-only plan checking must not masquerade as rollout activation."""
    path, _ = candidate
    module = load(path)
    flood, cfg_file = isolated_flood(module, tmp_path)
    pdir = flood.proposed_dir("OMP-584")
    pdir.mkdir(parents=True)
    (pdir / "OMP-584-s01.md").write_text(
        "---\nid: OMP-584-s01\nfiles: [src/sample.py]\ndepends_on: []\n---\n"
        "Extend tests/test_sample.py and implement sample change.\n"
        "Done when: sample regression passes.\nDo not touch: unrelated files.\n")
    result = command([sys.executable, "-B", str(path), "check-plan", "OMP-584",
                      "--config", str(cfg_file)], path.parent)
    assert result.returncode == 0, result.stderr + result.stdout
    assert "OK: 1 slices pass the plan gate" in result.stdout
    assert loaded_records(result.stdout) == []
    assert not flood.state.path.exists()


def test_composed_patches_accept_exact_already_applied_candidate(candidate):
    """Requalification must accept an exact installation without double insertion."""
    path, _ = candidate
    before = path.read_bytes()
    compose(path.parent, PATCHES)
    assert path.read_bytes() == before
    compile(path.read_bytes(), str(path), "exec")


@pytest.mark.parametrize("content", [
    "wrong\nend\nbetween\nlast\ntail\n",
    "new\nend\nbetween\nlast\ntail\n",
])
def test_patch_mismatch_and_partial_installation_fail_without_partial_writes(tmp_path, content):
    """Unknown or half-installed patch must fail, never silently execute a mix."""
    init_repo(tmp_path)
    target = tmp_path / "flood.py"
    target.write_text(content)
    patch = tmp_path / "two-hunks.patch"
    patch.write_text("--- a/flood.py\n+++ b/flood.py\n@@ -1,2 +1,2 @@\n-old\n+new\n end\n"
                     "@@ -4,2 +4,2 @@\n-last\n+changed\n tail\n")
    with pytest.raises(AssertionError, match="two-hunks.patch"):
        compose(tmp_path, [patch])
    assert target.read_text() == content


def test_actual_prompt_partial_installation_is_rejected(candidate, tmp_path):
    """An installation missing only run-entry provenance must not qualify."""
    path, _ = candidate
    lines = PATCHES[-1].read_text().splitlines(keepends=True)
    last_hunk = max(i for i, line in enumerate(lines) if line.startswith("@@"))
    partial = tmp_path / "activation-only.patch"
    partial.write_text("".join(lines[:2] + lines[last_hunk:]))
    result = command(["git", "apply", "--reverse", str(partial)], path.parent)
    assert result.returncode == 0, result.stderr
    with pytest.raises(AssertionError):
        compose(path.parent, PATCHES)
