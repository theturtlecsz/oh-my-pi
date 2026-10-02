"""GitHub pull client against FakeGitHub (OMP-518-s02)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from github_fake import FakeGitHub
from omp_work.orchestrator.github_pulls import GitHubPulls, PullRequestError

REPOSITORY = "acme/ledger"
READ_TOKEN = "read-token"
MERGE_TOKEN = "merge-token"


@dataclass
class World:
    tmp_path: Path
    bare: Path
    base_sha: str
    head_sha: str
    fake: FakeGitHub
    pulls: GitHubPulls
    read_token: Path
    merge_token: Path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _bare_repo(tmp_path: Path) -> tuple[Path, str, str]:
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(src)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(src), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", str(src), "config", "user.name", "Test User"], check=True
    )
    (src / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-m", "base"], check=True, capture_output=True
    )
    for branch in ("next", "release"):
        subprocess.run(["git", "-C", str(src), "branch", branch, "main"], check=True)
    subprocess.run(
        ["git", "-C", str(src), "checkout", "-b", "feature"],
        check=True,
        capture_output=True,
    )
    (src / "file.txt").write_text("feature\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(src), "commit", "-am", "feature"],
        check=True,
        capture_output=True,
    )
    bare = tmp_path / "repo.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(bare)], check=True, capture_output=True
    )
    return (
        bare,
        _git(bare, "rev-parse", "refs/heads/main"),
        _git(bare, "rev-parse", "refs/heads/feature"),
    )


def _parents(repo: Path, sha: str) -> tuple[str, str]:
    first, second = _git(repo, "rev-parse", f"{sha}^1", f"{sha}^2").splitlines()
    return first, second


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    bare, base_sha, head_sha = _bare_repo(tmp_path)
    read_token = tmp_path / "read.token"
    read_token.write_text(f"{READ_TOKEN}\n", encoding="utf-8")
    merge_token = tmp_path / "merge.token"
    merge_token.write_text(f"{MERGE_TOKEN}\n", encoding="utf-8")
    fake = FakeGitHub(bare, REPOSITORY, {READ_TOKEN, MERGE_TOKEN})
    try:
        yield World(
            tmp_path=tmp_path,
            bare=bare,
            base_sha=base_sha,
            head_sha=head_sha,
            fake=fake,
            pulls=GitHubPulls(fake.url, REPOSITORY, read_token),
            read_token=read_token,
            merge_token=merge_token,
        )
    finally:
        fake.close()


def test_merge_moves_base_and_stale_sha_does_not(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Land feature", "Ship the branch.")
    assert pull.head_sha == world.head_sha
    assert pull.base_ref == "main"
    assert pull.merged is False

    with pytest.raises(PullRequestError) as raised:
        world.pulls.merge(pull.number, "0" * 40, world.merge_token)
    assert raised.value.code == "merge_refused"
    assert isinstance(raised.value, RuntimeError)
    assert _git(world.bare, "rev-parse", "refs/heads/main") == world.base_sha

    merge_sha = world.pulls.merge(pull.number, pull.head_sha, world.merge_token)
    assert _git(world.bare, "rev-parse", "refs/heads/main") == merge_sha
    assert _parents(world.bare, merge_sha) == (world.base_sha, world.head_sha)

    merged = world.pulls.read(pull.number)
    assert merged.state == "closed"
    assert merged.merged is True
    assert merged.merge_commit_sha == merge_sha

    with pytest.raises(PullRequestError) as closed:
        world.pulls.merge(pull.number, pull.head_sha, world.merge_token)
    assert closed.value.code == "merge_refused"
    assert _git(world.bare, "rev-parse", "refs/heads/main") == merge_sha


def test_duplicate_create_is_unprocessable_and_leaves_one_pull(world: World) -> None:
    first = world.pulls.create("feature", "main", "One", "first")
    with pytest.raises(PullRequestError) as raised:
        world.pulls.create("feature", "main", "Two", "second")
    assert raised.value.code == "unprocessable"
    assert world.pulls.read(first.number).number == first.number
    with pytest.raises(PullRequestError) as missing:
        world.pulls.read(first.number + 1)
    assert missing.value.code == "not_found"


def test_newest_check_run_wins_and_running_is_none(world: World) -> None:
    sha = world.head_sha
    world.fake.set_check(sha, "ci", "completed", "failure")
    world.fake.set_check(sha, "ci", "completed", "success")
    world.fake.set_check(sha, "lint", "completed", "success")
    world.fake.set_check(sha, "lint", "in_progress", None)
    world.fake.set_check("other", "ci", "completed", "failure")

    assert world.pulls.check_conclusions("no-such") == {}
    assert world.pulls.check_conclusions(sha) == {"ci": "success", "lint": None}

    method, path, token = world.fake.calls[-1]
    assert method == "GET"
    assert path == f"/repos/{REPOSITORY}/commits/{sha}/check-runs?per_page=100"
    assert token == READ_TOKEN


def test_dropped_merge_reply_is_outcome_unknown_after_base_moves(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Land feature", "body")
    world.fake.drop_after_merge = True
    client = GitHubPulls(world.fake.url, REPOSITORY, world.read_token, timeout=2)

    with pytest.raises(PullRequestError) as raised:
        client.merge(pull.number, pull.head_sha, world.merge_token)
    assert raised.value.code == "outcome_unknown"
    moved = _git(world.bare, "rev-parse", "refs/heads/main")
    assert moved != world.base_sha
    assert _parents(world.bare, moved) == (world.base_sha, world.head_sha)
    assert world.fake.drop_after_merge is False


def test_merge_sends_token_path_and_empty_token_makes_no_call(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Land feature", "body")
    assert world.fake.calls[-1][2] == READ_TOKEN

    before = len(world.fake.calls)
    empty = world.tmp_path / "empty.token"
    empty.write_text("", encoding="utf-8")
    blank = world.tmp_path / "blank.token"
    blank.write_text(" \n\t", encoding="utf-8")
    for path in (empty, blank, world.tmp_path / "missing.token"):
        with pytest.raises(PullRequestError) as raised:
            world.pulls.merge(pull.number, pull.head_sha, path)
        assert raised.value.code == "credential_missing"
    assert len(world.fake.calls) == before
    assert _git(world.bare, "rev-parse", "refs/heads/main") == world.base_sha

    merge_sha = world.pulls.merge(pull.number, pull.head_sha, world.merge_token)
    method, path, token = world.fake.calls[-1]
    assert method == "PUT"
    assert path == f"/repos/{REPOSITORY}/pulls/{pull.number}/merge"
    assert token == MERGE_TOKEN
    assert token != READ_TOKEN
    assert _git(world.bare, "rev-parse", "refs/heads/main") == merge_sha


def test_other_repository_is_not_found_and_retarget_shows_on_read(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Land feature", "body")
    other = GitHubPulls(world.fake.url, "other/repo", world.read_token)
    with pytest.raises(PullRequestError) as raised:
        other.read(pull.number)
    assert raised.value.code == "not_found"

    world.fake.retarget(pull.number, "release")
    seen = world.pulls.read(pull.number)
    assert seen.base_ref == "release"
    assert seen.number == pull.number
    assert seen.head_ref == "feature"
    assert seen.head_sha == world.head_sha


def test_find_returns_highest_number_without_a_base_filter(world: World) -> None:
    first = world.pulls.create("feature", "main", "One", "a")
    second = world.pulls.create("feature", "next", "Two", "b")
    third = world.pulls.create("feature", "release", "Three", "c")
    assert third.number > second.number > first.number
    assert world.pulls.find("feature") == third.number
    assert world.pulls.find("absent") is None

    method, path, token = world.fake.calls[-1]
    assert method == "GET"
    assert path == f"/repos/{REPOSITORY}/pulls?state=all&head=acme:absent"
    assert "base=" not in path
    assert token == READ_TOKEN
    feature_gets = [
        call[1]
        for call in world.fake.calls
        if call[0] == "GET" and call[1].startswith(f"/repos/{REPOSITORY}/pulls?")
    ]
    assert f"/repos/{REPOSITORY}/pulls?state=all&head=acme:feature" in feature_gets


def test_unknown_token_is_github_refused(world: World) -> None:
    bad = world.tmp_path / "bad.token"
    bad.write_text("not-a-token\n", encoding="utf-8")
    client = GitHubPulls(world.fake.url, REPOSITORY, bad)
    with pytest.raises(PullRequestError) as raised:
        client.read(1)
    assert raised.value.code == "github_refused"
    assert world.fake.calls[-1] == (
        "GET",
        f"/repos/{REPOSITORY}/pulls/1",
        "not-a-token",
    )


def test_merge_does_not_run_hooks(world: World) -> None:
    hooks = world.tmp_path / "hooks"
    hooks.mkdir()
    marker = world.tmp_path / "hook-ran"
    hook = hooks / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n", encoding="utf-8")
    hook.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(world.bare), "config", "core.hooksPath", str(hooks)],
        check=True,
    )
    pull = world.pulls.create("feature", "main", "Land feature", "body")
    world.pulls.merge(pull.number, pull.head_sha, world.merge_token)
    assert not marker.exists()
    assert _git(world.bare, "rev-parse", "refs/heads/main") != world.base_sha


def test_pull_request_is_frozen(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Land feature", "body")
    with pytest.raises(AttributeError):
        pull.number = 9  # type: ignore[misc]
