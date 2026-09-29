"""Tests for project, ADR, and repository context items (OMP-419).

Defends observable contracts:
- unfinished missions yield no context items; finished missions admit with last history summary;
- research items format domain: outcome, omitting reason when None;
- project refs accept decision and roadmap, while artifact kind is refused by schema;
- ADR items parse first '# ' heading and first paragraph with collapsed whitespace;
- missing docs/adr folder returns empty tuple;
- repository item reports HEAD sha, origin URL/toplevel, branch, and clean or sorted changed paths;
- non-git directory returns empty tuple.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import pytest
from pydantic import ValidationError

from omp_knowledge.context.project_sources import (
    ProjectContext,
    ProjectHistory,
    ProjectMission,
    ProjectRef,
    ProjectResearch,
    adr_items,
    project_items,
    repository_items,
)


def _init_git_repo(path: Path) -> str:
    """Initialize a git repo with one commit, returning its HEAD sha."""
    subprocess.run(["git", "init", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Tester"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True, capture_output=True)
    initial_file = path / "README.md"
    initial_file.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "initial commit"], check=True, capture_output=True)
    res = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return res.stdout.strip()


def test_unfinished_missions_give_no_item() -> None:
    """Unfinished missions (draft, running, paused, etc.) must not emit context items."""
    ctx = ProjectContext(
        missions=(
            ProjectMission(mission_id="m-draft", objective="Drafting", status="draft"),
            ProjectMission(mission_id="m-running", objective="Executing", status="running"),
            ProjectMission(mission_id="m-paused", objective="Holding", status="paused"),
            ProjectMission(mission_id="m-blocked", objective="Blocked on input", status="blocked"),
            ProjectMission(mission_id="m-approved", objective="Ready to start", status="approved"),
            ProjectMission(mission_id="m-awaiting", objective="Waiting confirmation", status="awaiting_confirmation"),
        ),
        history=(
            ProjectHistory(mission_id="m-running", kind="status", summary="Started execution"),
        ),
    )
    items = project_items(ctx)
    assert items == ()


def test_finished_missions_admit_with_last_history_summary() -> None:
    """Finished missions (completed, failed, abandoned) emit items carrying the last history summary."""
    ctx = ProjectContext(
        missions=(
            ProjectMission(mission_id="m-done", objective="Migrate DB", status="completed"),
            ProjectMission(mission_id="m-fail", objective="Verify benchmarks", status="failed"),
            ProjectMission(mission_id="m-drop", objective="Explore prototype", status="abandoned"),
        ),
        history=(
            ProjectHistory(mission_id="m-done", kind="start", summary="Step 1 started"),
            ProjectHistory(mission_id="m-done", kind="finish", summary="Migration executed without error"),
            ProjectHistory(mission_id="m-fail", kind="error", summary="Timeout exceeded"),
        ),
    )
    items = project_items(ctx)
    assert len(items) == 3

    done_item = items[0]
    assert done_item.section == "exact"
    assert done_item.source == "mission"
    assert done_item.ref == "m-done"
    assert done_item.status == "current"
    assert not done_item.mandatory
    assert done_item.text == "Migrate DB \u2014 completed; Migration executed without error"

    fail_item = items[1]
    assert fail_item.source == "mission"
    assert fail_item.ref == "m-fail"
    assert fail_item.text == "Verify benchmarks \u2014 failed; Timeout exceeded"

    # m-drop has no history rows, so no semicolon summary is appended
    drop_item = items[2]
    assert drop_item.source == "mission"
    assert drop_item.ref == "m-drop"
    assert drop_item.text == "Explore prototype \u2014 abandoned"


def test_research_reason_omitted_when_none() -> None:
    """Research context items omit the reason suffix when outcome_reason is None."""
    ctx = ProjectContext(
        research=(
            ProjectResearch(
                campaign_id="rc-1",
                domain="perf",
                outcome="faster",
                outcome_reason=None,
            ),
            ProjectResearch(
                campaign_id="rc-2",
                domain="security",
                outcome="rejected",
                outcome_reason="unbounded memory growth",
            ),
        )
    )
    items = project_items(ctx)
    assert len(items) == 2

    assert items[0].section == "exact"
    assert items[0].source == "research"
    assert items[0].ref == "rc-1"
    assert items[0].status == "current"
    assert not items[0].mandatory
    assert items[0].text == "perf: faster"

    assert items[1].section == "exact"
    assert items[1].source == "research"
    assert items[1].ref == "rc-2"
    assert items[1].status == "current"
    assert not items[1].mandatory
    assert items[1].text == "security: rejected \u2014 unbounded memory growth"


def test_refs_kind_artifact_refused() -> None:
    """Project refs kind must be decision or roadmap; 'artifact' is refused."""
    with pytest.raises(ValidationError):
        ProjectRef(kind="artifact", ref="art-1", title="Artifact Title")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        ProjectContext(refs=[{"kind": "artifact", "ref": "art-1", "title": "Artifact Title"}])  # type: ignore[list-item]

    # Valid decision and roadmap refs succeed
    ctx = ProjectContext(
        refs=(
            ProjectRef(kind="decision", ref="dec-01", title="Use PostgreSQL"),
            ProjectRef(kind="roadmap", ref="rd-02", title="Q4 Milestones"),
        )
    )
    items = project_items(ctx)
    assert len(items) == 2
    assert items[0].source == "decision"
    assert items[0].ref == "dec-01"
    assert items[0].text == "Use PostgreSQL"
    assert items[1].source == "roadmap"
    assert items[1].ref == "rd-02"
    assert items[1].text == "Q4 Milestones"


def test_adr_heading_paragraph_parsing(tmp_path: Path) -> None:
    """ADR parser extracts first '# ' heading and first paragraph, collapsing whitespace."""
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)

    # 1. Standard ADR with multi-line paragraph and subsequent sections
    (adr_dir / "0002-second.md").write_text(
        "# 0002 Second Decision\n\n"
        "This is paragraph one,\n"
        "  which spans multiple lines\twith tabs.\n\n"
        "This is paragraph two that should be ignored.\n\n"
        "## Status\nAccepted\n",
        encoding="utf-8",
    )

    # 2. ADR with heading only (no paragraph)
    (adr_dir / "0001-first.md").write_text(
        "# 0001 First Decision\n\n"
        "## Status\nProposed\n",
        encoding="utf-8",
    )

    # 3. File with blank lines before heading
    (adr_dir / "0003-third.md").write_text(
        "\n\n# 0003 Third Decision\n\nSingle line paragraph.\n",
        encoding="utf-8",
    )

    # 4. Non-markdown file should be ignored
    (adr_dir / "ignored.txt").write_text("# Not markdown\n\nIgnored text\n", encoding="utf-8")

    items = adr_items(tmp_path)
    assert len(items) == 3

    # Sorted by filename
    assert items[0].ref == "docs/adr/0001-first.md"
    assert items[0].source == "adr"
    assert items[0].section == "exact"
    assert items[0].status == "current"
    assert not items[0].mandatory
    assert items[0].text == "0001 First Decision"

    assert items[1].ref == "docs/adr/0002-second.md"
    assert items[1].source == "adr"
    assert items[1].text == "0002 Second Decision This is paragraph one, which spans multiple lines with tabs."

    assert items[2].ref == "docs/adr/0003-third.md"
    assert items[2].source == "adr"
    assert items[2].text == "0003 Third Decision Single line paragraph."


def test_adr_missing_folder_gives_empty(tmp_path: Path) -> None:
    """Missing docs/adr folder returns empty tuple."""
    empty_dir = tmp_path / "empty_workspace"
    empty_dir.mkdir()
    assert adr_items(empty_dir) == ()


def test_adr_finds_toplevel_from_subfolder(tmp_path: Path) -> None:
    """adr_items resolves git toplevel when invoked from a subfolder."""
    repo_dir = tmp_path / "git_repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)

    adr_dir = repo_dir / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-init.md").write_text("# 0001 Root ADR\n\nRoot paragraph.\n", encoding="utf-8")

    sub_dir = repo_dir / "packages" / "subpackage"
    sub_dir.mkdir(parents=True)

    items = adr_items(sub_dir)
    assert len(items) == 1
    assert items[0].ref == "docs/adr/0001-init.md"
    assert items[0].text == "0001 Root ADR Root paragraph."


def test_repository_clean_tree_says_clean(tmp_path: Path) -> None:
    """Clean git working tree produces one repository item with 'clean' in text."""
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir()
    head_sha = _init_git_repo(repo_dir)

    # Add a mock remote
    subprocess.run(
        ["git", "-C", str(repo_dir), "remote", "add", "origin", "git@github.com:org/my_project.git"],
        check=True,
        capture_output=True,
    )

    items = repository_items(repo_dir)
    assert len(items) == 1
    item = items[0]
    assert item.section == "exact"
    assert item.source == "repository"
    assert item.ref == head_sha
    assert item.status == "current"
    assert not item.mandatory

    lines = item.text.splitlines()
    assert lines[0] == "ssh://git@github.com/org/my_project"
    assert lines[1] == head_sha
    assert lines[2] == "main"
    assert lines[3] == "clean"


def test_repository_item_lists_changed_paths(tmp_path: Path) -> None:
    """Repository item lists sorted changed and untracked paths."""
    repo_dir = tmp_path / "repo_with_changes"
    repo_dir.mkdir()
    head_sha = _init_git_repo(repo_dir)

    # Modify an existing file and add an untracked file
    (repo_dir / "README.md").write_text("# Modified\n", encoding="utf-8")
    (repo_dir / "b_new.txt").write_text("untracked b\n", encoding="utf-8")
    (repo_dir / "a_new.txt").write_text("untracked a\n", encoding="utf-8")

    items = repository_items(repo_dir)
    assert len(items) == 1
    item = items[0]
    assert item.ref == head_sha

    lines = item.text.splitlines()
    assert lines[0] == "repo_with_changes"  # no remote set, defaults to toplevel name
    assert lines[1] == head_sha
    assert lines[2] == "main"
    # Changed paths sorted
    assert lines[3:] == ["README.md", "a_new.txt", "b_new.txt"]


def test_repository_non_git_folder_gives_empty(tmp_path: Path) -> None:
    """A directory outside git returns an empty tuple."""
    non_git = tmp_path / "plain_dir"
    non_git.mkdir()
    assert repository_items(non_git) == ()


def test_project_context_empty_and_frozen() -> None:
    """ProjectContext defaults to empty tuples and forbids extra fields and mutations."""
    ctx = ProjectContext()
    assert ctx.refs == ()
    assert ctx.missions == ()
    assert ctx.history == ()
    assert ctx.research == ()
    assert project_items(ctx) == ()
    assert project_items(None) == ()

    with pytest.raises(ValidationError):
        ProjectContext(unknown_field="fail")  # type: ignore[call-arg]
