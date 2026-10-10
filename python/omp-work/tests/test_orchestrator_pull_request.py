"""Tests for typed orchestrator pull request operations (OMP-518-s03)."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from github_fake import FakeGitHub
from omp_work.orchestrator.candidate_git import _intent_key
from omp_work.orchestrator.github_pulls import GitHubPulls, PullRequestError
from omp_work.orchestrator.pull_request import (
    check_pull,
    merge_pull_request,
    open_pull_request,
    read_pull_request,
    required_checks_state,
)

REPOSITORY = "acme/ledger"
READ_TOKEN = "read-token"
MERGE_TOKEN = "merge-token"


class InMemoryIntentLog:
    """In-memory IntentLog implementation for testing."""

    def __init__(self, *, raise_once: bool = False) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.done_calls = 0
        self._raise_once = raise_once
        self._raised = False

    def record_intent(self, key: str, action: str, target: Any) -> None:
        self.records[key] = {"action": action, "target": target, "done": False}

    def mark_done(self, key: str, ref: str) -> None:
        self.done_calls += 1
        if self._raise_once and not self._raised:
            self._raised = True
            raise RuntimeError("mark_done crashed")
        if key in self.records:
            self.records[key]["done"] = True
            self.records[key]["ref"] = ref

    def open(self, key: str) -> dict[str, Any] | None:
        record = self.records.get(key)
        if record is not None and not record.get("done", False):
            return record
        return None


@dataclass
class World:
    tmp_path: Path
    bare: Path
    src: Path
    base_sha: str
    head_sha: str
    fake: FakeGitHub
    pulls: GitHubPulls
    read_token: Path
    merge_token: Path
    intents: InMemoryIntentLog


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _bare_repo(tmp_path: Path) -> tuple[Path, Path, str, str]:
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(src)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(src), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(src), "config", "user.name", "Test User"], check=True
    )
    (src / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(src), "add", "file.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )
    for branch in ("next", "release", "other"):
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
        ["git", "clone", "--bare", str(src), str(bare)],
        check=True,
        capture_output=True,
    )
    return (
        bare,
        src,
        _git(bare, "rev-parse", "refs/heads/main"),
        _git(bare, "rev-parse", "refs/heads/feature"),
    )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    bare, src, base_sha, head_sha = _bare_repo(tmp_path)
    read_token = tmp_path / "read.token"
    read_token.write_text(f"{READ_TOKEN}\n", encoding="utf-8")
    merge_token = tmp_path / "merge.token"
    merge_token.write_text(f"{MERGE_TOKEN}\n", encoding="utf-8")
    fake = FakeGitHub(bare, REPOSITORY, {READ_TOKEN, MERGE_TOKEN})
    try:
        yield World(
            tmp_path=tmp_path,
            bare=bare,
            src=src,
            base_sha=base_sha,
            head_sha=head_sha,
            fake=fake,
            pulls=GitHubPulls(fake.url, REPOSITORY, read_token),
            read_token=read_token,
            merge_token=merge_token,
            intents=InMemoryIntentLog(),
        )
    finally:
        fake.close()


def test_merge_crash_drop_after_merge_reconciles(world: World) -> None:
    pull = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Feature",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )

    world.fake.drop_after_merge = True
    key = _intent_key("merge", world.head_sha, REPOSITORY, "feature->main")

    with pytest.raises(PullRequestError) as exc_info:
        merge_pull_request(
            world.pulls,
            pull_number=pull.number,
            commit=world.head_sha,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: True,
            credential_path=world.merge_token,
        )
    assert exc_info.value.code == "outcome_unknown"
    assert world.intents.open(key) is not None

    puts_first = [c for c in world.fake.calls if c[0] == "PUT"]
    assert len(puts_first) == 1

    # Re-call reconciles the merge without a second PUT
    merge_sha = merge_pull_request(
        world.pulls,
        pull_number=pull.number,
        commit=world.head_sha,
        head="feature",
        base="main",
        intents=world.intents,
        lease_ok=lambda: True,
        credential_path=world.merge_token,
    )

    puts_second = [c for c in world.fake.calls if c[0] == "PUT"]
    assert len(puts_second) == 1
    assert world.intents.open(key) is None
    assert merge_sha == _git(world.bare, "rev-parse", "refs/heads/main")
    assert world.intents.records[key]["ref"] == merge_sha


def test_open_pull_request_three_times_one_post(world: World) -> None:
    p1 = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Title",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )
    p2 = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Title",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )
    p3 = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Title",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )

    assert p1.number == p2.number == p3.number
    posts = [c for c in world.fake.calls if c[0] == "POST"]
    assert len(posts) == 1

    # Verify intent completed with pull number
    key = _intent_key("open", world.head_sha, REPOSITORY, "feature->main")
    assert world.intents.records[key]["done"] is True
    assert world.intents.records[key]["ref"] == str(p1.number)


def test_open_pull_request_recovers_when_intent_was_open(world: World) -> None:
    key = _intent_key("open", world.head_sha, REPOSITORY, "feature->main")
    world.intents.record_intent(key, "open", f"{REPOSITORY} feature->main")
    assert world.intents.open(key) is not None

    pull = world.pulls.create("feature", "main", "Title", "Body")
    posts_before = len([c for c in world.fake.calls if c[0] == "POST"])

    opened = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Title",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )

    assert opened.number == pull.number
    posts_after = len([c for c in world.fake.calls if c[0] == "POST"])
    assert posts_after == posts_before
    assert world.intents.open(key) is None
    assert world.intents.records[key]["ref"] == str(pull.number)


def test_retarget_gives_target_mismatch(world: World) -> None:
    pull = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Feature",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )

    world.fake.retarget(pull.number, "other")
    main_before = _git(world.bare, "rev-parse", "refs/heads/main")
    other_before = _git(world.bare, "rev-parse", "refs/heads/other")
    calls_before = len(world.fake.calls)

    with pytest.raises(PullRequestError) as exc_open:
        open_pull_request(
            world.pulls,
            commit=world.head_sha,
            head="feature",
            base="main",
            title="Feature",
            body="Body",
            intents=world.intents,
            lease_ok=lambda: True,
        )
    assert exc_open.value.code == "target_mismatch"

    with pytest.raises(PullRequestError) as exc_merge:
        merge_pull_request(
            world.pulls,
            pull_number=pull.number,
            commit=world.head_sha,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: True,
            credential_path=world.merge_token,
        )
    assert exc_merge.value.code == "target_mismatch"

    new_modifying_calls = [
        c for c in world.fake.calls[calls_before:] if c[0] in ("POST", "PUT")
    ]
    assert len(new_modifying_calls) == 0

    assert _git(world.bare, "rev-parse", "refs/heads/main") == main_before
    assert _git(world.bare, "rev-parse", "refs/heads/other") == other_before


def test_credential_required_and_lease_dead_no_intent_or_call(
    world: World,
) -> None:
    calls_before = len(world.fake.calls)

    with pytest.raises(PullRequestError) as exc_cred:
        merge_pull_request(
            world.pulls,
            pull_number=1,
            commit=world.head_sha,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: False,
            credential_path=None,
        )
    assert exc_cred.value.code == "credential_required"

    with pytest.raises(PullRequestError) as exc_lease_merge:
        merge_pull_request(
            world.pulls,
            pull_number=1,
            commit=world.head_sha,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: False,
            credential_path=world.merge_token,
        )
    assert exc_lease_merge.value.code == "lease_dead"

    with pytest.raises(PullRequestError) as exc_lease_open:
        open_pull_request(
            world.pulls,
            commit=world.head_sha,
            head="feature",
            base="main",
            title="Feature",
            body="Body",
            intents=world.intents,
            lease_ok=lambda: False,
        )
    assert exc_lease_open.value.code == "lease_dead"

    assert len(world.intents.records) == 0
    assert len(world.fake.calls) == calls_before


def test_head_mismatch_no_put(world: World) -> None:
    pull = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Feature",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )

    calls_before = len(world.fake.calls)
    wrong_commit = "0" * 40

    with pytest.raises(PullRequestError) as exc:
        merge_pull_request(
            world.pulls,
            pull_number=pull.number,
            commit=wrong_commit,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: True,
            credential_path=world.merge_token,
        )
    assert exc.value.code == "head_mismatch"

    puts = [c for c in world.fake.calls[calls_before:] if c[0] == "PUT"]
    assert len(puts) == 0


def test_bare_hooks_update_logs_and_rejects_main_after_merge_log_empty(
    world: World,
) -> None:
    hook_log = world.tmp_path / "update-hook.log"
    update_hook = world.bare / "hooks" / "update"
    update_hook.parent.mkdir(parents=True, exist_ok=True)
    update_hook.write_text(
        f"""#!/bin/sh
ref="$1"
old="$2"
new="$3"
if [ "$ref" = "refs/heads/main" ]; then
    echo "$ref $old $new" >> {shlex.quote(str(hook_log))}
    exit 1
fi
exit 0
""",
        encoding="utf-8",
    )
    update_hook.chmod(0o755)

    # Prove that a direct git push to refs/heads/main executes the hook and is rejected
    rejected = subprocess.run(
        ["git", "-C", str(world.src), "push", str(world.bare), "feature:main"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode != 0
    assert hook_log.exists()
    assert "refs/heads/main" in hook_log.read_text(encoding="utf-8")

    # Clear hook log before pull request merge
    hook_log.write_text("", encoding="utf-8")

    pull = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Feature",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )
    merge_sha = merge_pull_request(
        world.pulls,
        pull_number=pull.number,
        commit=world.head_sha,
        head="feature",
        base="main",
        intents=world.intents,
        lease_ok=lambda: True,
        credential_path=world.merge_token,
    )

    assert _git(world.bare, "rev-parse", "refs/heads/main") == merge_sha
    assert hook_log.read_text(encoding="utf-8") == ""


def test_required_checks_state_behavior(world: World) -> None:
    pull = world.pulls.create("feature", "main", "Feature", "Body")

    # Empty required list raises required_checks_missing
    with pytest.raises(PullRequestError) as exc_missing:
        required_checks_state(world.pulls, pull, [])
    assert exc_missing.value.code == "required_checks_missing"

    # Not reported yet is pending
    assert required_checks_state(world.pulls, pull, ["ci"]) == "pending"

    # In progress is pending
    world.fake.set_check(pull.head_sha, "ci", "in_progress", None)
    assert required_checks_state(world.pulls, pull, ["ci"]) == "pending"

    # Completed with success, neutral, and skipped passes
    world.fake.set_check(pull.head_sha, "ci", "completed", "success")
    world.fake.set_check(pull.head_sha, "lint", "completed", "neutral")
    world.fake.set_check(pull.head_sha, "deploy", "completed", "skipped")
    assert (
        required_checks_state(world.pulls, pull, ["ci", "lint", "deploy"]) == "passed"
    )

    # Failed check causes failed state
    world.fake.set_check(pull.head_sha, "test", "completed", "failure")
    assert required_checks_state(world.pulls, pull, ["ci", "test"]) == "failed"

    # Failed takes precedence over pending
    assert required_checks_state(world.pulls, pull, ["test", "unknown"]) == "failed"


def test_read_pull_request_and_check_pull(world: World) -> None:
    created = world.pulls.create("feature", "main", "Feature", "Body")

    read = read_pull_request(
        world.pulls,
        created.number,
        head="feature",
        base="main",
        commit=world.head_sha,
    )
    assert read.number == created.number
    assert read.head_ref == "feature"
    assert read.base_ref == "main"
    assert read.head_sha == world.head_sha

    with pytest.raises(PullRequestError) as exc_head:
        check_pull(
            read,
            head="wrong",
            base="main",
            commit=world.head_sha,
        )
    assert exc_head.value.code == "target_mismatch"

    with pytest.raises(PullRequestError) as exc_base:
        check_pull(
            read,
            head="feature",
            base="wrong",
            commit=world.head_sha,
        )
    assert exc_base.value.code == "target_mismatch"

    with pytest.raises(PullRequestError) as exc_sha:
        check_pull(
            read,
            head="feature",
            base="main",
            commit="1" * 40,
        )
    assert exc_sha.value.code == "head_mismatch"


def test_merge_pull_request_closed_raises(world: World) -> None:
    created = world.pulls.create("feature", "main", "Feature", "Body")
    world.fake._pulls[created.number]["state"] = "closed"

    with pytest.raises(PullRequestError) as exc:
        merge_pull_request(
            world.pulls,
            pull_number=created.number,
            commit=world.head_sha,
            head="feature",
            base="main",
            intents=world.intents,
            lease_ok=lambda: True,
            credential_path=world.merge_token,
        )
    assert exc.value.code == "pull_request_closed"


def test_open_pull_request_concurrent_422_finds_and_returns(world: World) -> None:
    # First create pull 1 externally
    existing = world.pulls.create("feature", "main", "Existing", "Body")

    # Monkeypatch find to return None on the first call to simulate a race where
    # find() missed it but create() encountered 422 because it was just opened
    real_find = world.pulls.find
    calls = 0

    def mock_find(head: str) -> int | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_find(head)

    world.pulls.find = mock_find  # type: ignore[method-assign]

    pull = open_pull_request(
        world.pulls,
        commit=world.head_sha,
        head="feature",
        base="main",
        title="Feature",
        body="Body",
        intents=world.intents,
        lease_ok=lambda: True,
    )
    assert pull.number == existing.number
    key = _intent_key("open", world.head_sha, REPOSITORY, "feature->main")
    assert world.intents.records[key]["done"] is True
    assert world.intents.records[key]["ref"] == str(existing.number)
