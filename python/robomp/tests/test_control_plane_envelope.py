"""D35 unattended safety envelope for agent tasks.

An unattended deployment runs no timer-started agent session: every agent task
kind is refused before any RPC client exists. An attended run is unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from robomp import worker
from robomp.config import Settings

AGENT_TASK_KINDS = (
    "triage_issue",
    "review_pr",
    "handle_comment",
    "handle_pr_conversation",
    "handle_review",
    "handle_release_ci",
)


def _inputs(tmp_path: Path, settings: Settings) -> worker.TaskInputs:
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    session_dir = root / "session"
    session_dir.mkdir()
    repo_dir = root / "repo"
    repo_dir.mkdir()

    workspace = SimpleNamespace(
        root=root,
        session_dir=session_dir,
        repo_dir=repo_dir,
        branch="robomp/issue-1",
    )
    repo = SimpleNamespace(full_name="acme/widgets", owner="acme", name="widgets")
    issue = SimpleNamespace(repo="acme/widgets", number=1, title="bug")

    return worker.TaskInputs(
        settings=settings,
        db=SimpleNamespace(set_event_model=lambda *_: None, get_issue=lambda *_: None),
        github=SimpleNamespace(),
        git_transport=SimpleNamespace(),
        repo=repo,
        issue=issue,
        workspace=workspace,
        delivery_id="d-test",
    )


@pytest.mark.asyncio
async def test_unattended_refuses_every_agent_task_before_session(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    unattended = settings.model_copy(update={"unattended": True})
    sessions: list[object] = []
    monkeypatch.setattr(worker, "RpcClient", lambda **kwargs: sessions.append(kwargs))
    monkeypatch.setattr(
        worker,
        "_run_rpc_blocking",
        lambda *args, **kwargs: sessions.append("rpc"),
    )

    for task_kind in AGENT_TASK_KINDS:
        inputs = _inputs(tmp_path / task_kind, unattended)
        with pytest.raises(worker.UnattendedRefused):
            await worker.run_task(task_kind=task_kind, inputs=inputs)

    assert sessions == []


@pytest.mark.asyncio
async def test_attended_runs_task_as_before(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert settings.unattended is False
    ran: list[str] = []
    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_rpc_blocking",
        lambda *args, task_kind, **kwargs: ran.append(task_kind) or "ok",
    )

    result = await worker.run_task(
        task_kind="triage_issue", inputs=_inputs(tmp_path, settings)
    )

    assert result == "ok"
    assert ran == ["triage_issue"]
