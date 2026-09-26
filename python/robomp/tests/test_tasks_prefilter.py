"""Tests for Jev issue prefilter wiring in tasks.triage_issue and host_tools.classify_issue."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from robomp import tasks
from robomp.config import Settings
from robomp.db import Database
from robomp.github_client import CommentInfo, GitHubError, IssueInfo, RepoInfo
from robomp.host_tools import HostToolContext, ToolBindings, build
from robomp.jev_client import JevClient
from robomp.sandbox import LocalGitTransport
from tests.jev_stub import JevStub


def _make_loop_in_background() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop: asyncio.AbstractEventLoop, t: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)
    loop.close()


class FakeGitHub:
    """Mock GitHub backend recording API operations."""

    def __init__(self, issue_labels: tuple[str, ...] = ()) -> None:
        self.issue_labels = list(issue_labels)
        self.added_labels: list[tuple[str, int, list[str]]] = []
        self.removed_labels: list[tuple[str, int, str]] = []
        self.comments: list[tuple[str, int, str]] = []
        self.closing_prs: tuple[int, ...] = ()

    async def list_closing_pull_requests(self, repo: str, number: int) -> tuple[int, ...]:
        return self.closing_prs

    async def add_issue_labels(self, repo: str, number: int, labels: list[str]) -> tuple[str, ...]:
        self.added_labels.append((repo, number, list(labels)))
        self.issue_labels.extend(labels)
        return tuple(dict.fromkeys(self.issue_labels))

    async def remove_issue_label(self, repo: str, number: int, label: str) -> None:
        self.removed_labels.append((repo, number, label))
        if label in self.issue_labels:
            self.issue_labels.remove(label)

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        self.comments.append((repo, number, body))
        return CommentInfo(id=len(self.comments), author="robomp-bot", body=body, created_at="2026-09-26T00:00:00Z")

    async def get_issue(self, repo: str, number: int) -> IssueInfo:
        return IssueInfo(
            repo=repo,
            number=number,
            title="Sample issue",
            body="Sample body",
            state="open",
            author="alice",
            labels=tuple(self.issue_labels),
            is_pull_request=False,
        )


def _setup_sandbox_and_task(tmp_path: Path):
    workspace_calls: list[dict[str, Any]] = []
    run_task_calls: list[dict[str, Any]] = []

    def ensure_workspace(**kwargs: Any):
        workspace_calls.append(kwargs)
        sess_dir = tmp_path / "sess"
        sess_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            branch="farm/octo/issue-1",
            session_dir=str(sess_dir),
            repo_dir=repo_dir,
            root=tmp_path,
            artifacts_dir=tmp_path / "artifacts",
        )

    sandbox = SimpleNamespace(
        natives_cache=None,
        ensure_workspace=ensure_workspace,
        remove_workspace=lambda **_k: None,
    )

    async def fake_run_task(*, task_kind: str, inputs: Any, **kwargs: Any) -> None:
        run_task_calls.append({"task_kind": task_kind, "inputs": inputs, **kwargs})

    return sandbox, workspace_calls, run_task_calls, fake_run_task


def _make_payload(repo_name: str = "octo/widget", number: int = 1, title: str = "bug report", body: str = "broken"):
    return {
        "repository": {
            "full_name": repo_name,
            "default_branch": "main",
            "clone_url": f"https://github.com/{repo_name}.git",
            "private": False,
        },
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "user": {"login": "alice"},
            "labels": [],
        },
    }


async def test_flags_off_sequence_identical(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When jev_enabled or prefilter_enabled is False, full session runs without prefilter."""
    settings.jev_enabled = False
    settings.prefilter_enabled = False

    github = FakeGitHub()
    sandbox, ws_calls, run_calls, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    payload = _make_payload()
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=LocalGitTransport(token=None),
        payload=payload,
        delivery_id="del-1",
    )

    assert len(github.added_labels) == 0
    assert len(github.comments) == 0
    assert len(ws_calls) == 1
    assert len(run_calls) == 1
    row = db.get_issue("octo/widget#1")
    assert row is not None
    assert row.state == "reproducing"
    assert db.get_issue_prefilter("octo/widget#1") is None


async def test_confident_invalid_answered_without_session(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Confident invalid (>=0.90) adds provisional:invalid, posts template comment, skips workspace/session."""
    settings.jev_enabled = True
    settings.prefilter_enabled = True
    settings.prefilter_threshold = 0.90

    stub = JevStub(primary_probs={"invalid": 0.95, "bug": 0.05})
    client = JevClient(transport=stub.transport, db=db)

    github = FakeGitHub()
    sandbox, ws_calls, run_calls, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    payload = _make_payload(title="unrelated spam", body="buy watches")
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=LocalGitTransport(token=None),
        payload=payload,
        delivery_id="del-2",
        jev_client=client,
    )

    # Exactly one label call with provisional:invalid
    assert len(github.added_labels) == 1
    assert github.added_labels[0] == ("octo/widget", 1, ["provisional:invalid"])

    # Exactly one comment call with prefilter_answer_invalid.md content
    assert len(github.comments) == 1
    repo, num, comment = github.comments[0]
    assert repo == "octo/widget"
    assert num == 1
    assert "provisional automated triage" in comment.lower()
    assert "full review will confirm" in comment.lower()

    # No workspace setup, no task run, no issue row in DB
    assert len(ws_calls) == 0
    assert len(run_calls) == 0
    assert db.get_issue("octo/widget#1") is None

    # Pre-filter row recorded in DB
    pf = db.get_issue_prefilter("octo/widget#1")
    assert pf is not None
    assert pf.route == "answered"
    assert pf.label == "invalid"


async def test_confident_question_and_batch_audit_answered(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Confident question and batch_audit routes also answer provisionally."""
    settings.jev_enabled = True
    settings.prefilter_enabled = True

    # 1. Question
    stub_q = JevStub(primary_probs={"question": 0.92, "bug": 0.08})
    client_q = JevClient(transport=stub_q.transport, db=db)
    github_q = FakeGitHub()
    sandbox_q, ws_q, run_q, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    payload_q = _make_payload(number=10, title="how to run", body="how do I use this?")
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github_q,
        sandbox=sandbox_q,
        git_transport=LocalGitTransport(token=None),
        payload=payload_q,
        delivery_id="del-q",
        jev_client=client_q,
    )
    assert len(github_q.added_labels) == 1
    assert github_q.added_labels[0] == ("octo/widget", 10, ["provisional:question"])
    assert len(github_q.comments) == 1
    assert "question" in github_q.comments[0][2].lower()
    assert len(ws_q) == 0
    assert db.get_issue("octo/widget#10") is None

    # 2. Batch audit
    stub_b = JevStub(batch_audit_prob=0.96)
    client_b = JevClient(transport=stub_b.transport, db=db)
    github_b = FakeGitHub()
    sandbox_b, ws_b, run_b, _ = _setup_sandbox_and_task(tmp_path)

    payload_b = _make_payload(number=11, title="Audit report", body="Found 20 vulnerabilities")
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github_b,
        sandbox=sandbox_b,
        git_transport=LocalGitTransport(token=None),
        payload=payload_b,
        delivery_id="del-b",
        jev_client=client_b,
    )
    assert len(github_b.added_labels) == 1
    assert github_b.added_labels[0] == ("octo/widget", 11, ["provisional:batch-audit"])
    assert len(github_b.comments) == 1
    assert "batch audit" in github_b.comments[0][2].lower()
    assert len(ws_b) == 0
    assert db.get_issue("octo/widget#11") is None


async def test_low_confidence_runs_full_session(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Low confidence (below threshold) takes route='session' and runs full session."""
    settings.jev_enabled = True
    settings.prefilter_enabled = True
    settings.prefilter_threshold = 0.90

    stub = JevStub(primary_probs={"invalid": 0.85, "bug": 0.15})
    client = JevClient(transport=stub.transport, db=db)

    github = FakeGitHub()
    sandbox, ws_calls, run_calls, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    payload = _make_payload(number=2)
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=LocalGitTransport(token=None),
        payload=payload,
        delivery_id="del-3",
        jev_client=client,
    )

    assert len(github.added_labels) == 0
    assert len(github.comments) == 0
    assert len(ws_calls) == 1
    assert len(run_calls) == 1
    assert db.get_issue("octo/widget#2") is not None
    pf = db.get_issue_prefilter("octo/widget#2")
    assert pf is not None
    assert pf.route == "session"


async def test_jev_500_or_timeout_fails_open_to_full_session(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Jev 500 or timeout fails open to full session."""
    settings.jev_enabled = True
    settings.prefilter_enabled = True

    # 500 error
    stub_500 = JevStub(mode="500")
    client_500 = JevClient(transport=stub_500.transport, db=db)
    github_500 = FakeGitHub()
    sandbox_500, ws_500, run_500, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github_500,
        sandbox=sandbox_500,
        git_transport=LocalGitTransport(token=None),
        payload=_make_payload(number=3),
        delivery_id="del-500",
        jev_client=client_500,
    )
    assert len(github_500.added_labels) == 0
    assert len(ws_500) == 1
    assert len(run_500) == 1
    assert db.get_issue("octo/widget#3") is not None

    # Timeout
    stub_timeout = JevStub(mode="timeout")
    client_timeout = JevClient(transport=stub_timeout.transport, db=db)
    github_to = FakeGitHub()
    sandbox_to, ws_to, run_to, fake_run_task_to = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task_to)

    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github_to,
        sandbox=sandbox_to,
        git_transport=LocalGitTransport(token=None),
        payload=_make_payload(number=4),
        delivery_id="del-to",
        jev_client=client_timeout,
    )
    assert len(github_to.added_labels) == 0
    assert len(ws_to) == 1
    assert len(run_to) == 1
    assert db.get_issue("octo/widget#4") is not None


async def test_second_triage_runs_session_and_classify_removes_provisional(
    db: Database, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A repeat triage for the same key runs the full session; classify_issue then removes provisional:invalid."""
    settings.jev_enabled = True
    settings.prefilter_enabled = True
    settings.prefilter_threshold = 0.90

    stub = JevStub(primary_probs={"invalid": 0.95, "bug": 0.05})
    client = JevClient(transport=stub.transport, db=db)

    github = FakeGitHub()
    sandbox, ws_calls, run_calls, fake_run_task = _setup_sandbox_and_task(tmp_path)
    monkeypatch.setattr(tasks, "run_task", fake_run_task)

    payload = _make_payload(number=5)

    # First triage: confident invalid -> answered
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=LocalGitTransport(token=None),
        payload=payload,
        delivery_id="del-first",
        jev_client=client,
    )
    assert len(github.added_labels) == 1
    assert github.added_labels[0][2] == ["provisional:invalid"]
    assert len(github.comments) == 1
    assert len(ws_calls) == 0
    assert db.get_issue("octo/widget#5") is None
    assert db.get_issue_prefilter("octo/widget#5") is not None

    # Second triage for the same key: repeat trigger runs full session
    await tasks.triage_issue(
        settings=settings,
        db=db,
        github=github,
        sandbox=sandbox,
        git_transport=LocalGitTransport(token=None),
        payload=payload,
        delivery_id="del-second",
        jev_client=client,
    )
    assert len(ws_calls) == 1
    assert len(run_calls) == 1
    assert db.get_issue("octo/widget#5") is not None

    # Now verify classify_issue in host_tools removes provisional:invalid and applies final labels
    loop, t = _make_loop_in_background()
    try:
        workspace = sandbox.ensure_workspace()
        issue_info = IssueInfo(
            repo="octo/widget",
            number=5,
            title="bug report",
            body="broken",
            state="open",
            author="alice",
            labels=("provisional:invalid",),
            is_pull_request=False,
        )
        repo_info = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://github.com/octo/widget.git",
            private=False,
        )
        bindings = ToolBindings(
            db=db,
            github=github,
            git_transport=LocalGitTransport(token=None),
            repo=repo_info,
            issue=issue_info,
            workspace=workspace,
            loop=loop,
            author_name="robomp-bot",
            author_email="bot@example.com",
        )

        tools = build(bindings)
        classify_tool = next(t for t in tools if t.name == "classify_issue")
        ctx = HostToolContext(tool_call_id="tc-1", _cancel_event=None, _send_update=lambda _: None)

        res = classify_tool.execute(
            {"primary": "bug", "priority": "prio:p1", "rationale": "verified reproducible bug"},
            ctx,
        )
        assert "classified as bug" in res

        # Check that remove_issue_label was called for provisional:invalid
        assert ("octo/widget", 5, "provisional:invalid") in github.removed_labels

        # Check that final labels were applied
        final_added = [call for call in github.added_labels if "bug" in call[2]]
        assert len(final_added) >= 1
        assert "bug" in final_added[-1][2]
        assert "prio:p1" in final_added[-1][2]
        assert "triaged" in final_added[-1][2]

        # Check database classification
        row = db.get_issue("octo/widget#5")
        assert row is not None
        assert row.classification == "bug"
    finally:
        _stop_loop(loop, t)


def test_classify_issue_removal_failure_logged_not_raised(db: Database, tmp_path: Path):
    """When remove_issue_label fails, classify_issue logs the failure and does not raise."""

    class FailingGitHub(FakeGitHub):
        async def remove_issue_label(self, repo: str, number: int, label: str) -> None:
            raise GitHubError(500, "internal error removing label")

    loop, t = _make_loop_in_background()
    try:
        db.upsert_issue(key="octo/widget#6", repo="octo/widget", number=6, state="reproducing")
        db.record_issue_prefilter(
            key="octo/widget#6",
            route="answered",
            label="invalid",
            probabilities_json="{}",
            request_id=None,
        )

        github = FailingGitHub(issue_labels=("provisional:invalid",))
        sess_dir = tmp_path / "sess"
        sess_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        workspace = SimpleNamespace(
            branch="farm/octo/issue-6",
            session_dir=str(sess_dir),
            repo_dir=repo_dir,
            root=tmp_path,
            artifacts_dir=tmp_path / "artifacts",
        )
        issue_info = IssueInfo(
            repo="octo/widget",
            number=6,
            title="Sample",
            body="Body",
            state="open",
            author="alice",
            labels=("provisional:invalid",),
            is_pull_request=False,
        )
        repo_info = RepoInfo(
            full_name="octo/widget",
            default_branch="main",
            clone_url="https://github.com/octo/widget.git",
            private=False,
        )
        bindings = ToolBindings(
            db=db,
            github=github,
            git_transport=LocalGitTransport(token=None),
            repo=repo_info,
            issue=issue_info,
            workspace=workspace,
            loop=loop,
            author_name="robomp-bot",
            author_email="bot@example.com",
        )

        tools = build(bindings)
        classify_tool = next(t for t in tools if t.name == "classify_issue")
        ctx = HostToolContext(tool_call_id="tc-2", _cancel_event=None, _send_update=lambda _: None)

        # Must not raise even though remove_issue_label raises GitHubError(500)
        res = classify_tool.execute(
            {"primary": "bug", "priority": "prio:p2", "rationale": "bug fix needed"},
            ctx,
        )
        assert "classified as bug" in res
        assert db.get_issue("octo/widget#6").classification == "bug"
    finally:
        _stop_loop(loop, t)
