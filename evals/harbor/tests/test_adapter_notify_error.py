"""A notify with notifyType error before the first agent turn is the trial result."""

from __future__ import annotations

from pathlib import Path

from test_adapter_pre_turn_error import _drive, _prompt_count

WORK_ID = "00000000-0000-4000-8000-0000000000bb"
FETCH_REFUSAL = (
    "Cannot begin execution: fetch origin main failed: "
    "fatal: 'origin' does not appear to be a git repository"
)


def _notify(message: str, notify_type: str) -> dict[str, str]:
    return {
        "type": "extension_ui_request",
        "id": "ui-note",
        "method": "notify",
        "message": message,
        "notifyType": notify_type,
    }


def test_error_notify_before_turn_ends_the_trial(tmp_path: Path) -> None:
    outcome, elapsed, loaded, record = _drive(
        tmp_path,
        timeout_s=30,
        events=[_notify(FETCH_REFUSAL, "error")],
        work_state={"work_id": WORK_ID, "state": "running"},
    )
    assert outcome == "harness_error"
    assert elapsed < 2
    assert loaded.read_json("outcome.json") == {
        "outcome": "harness_error",
        "reason": FETCH_REFUSAL,
        "prompts_sent": 1,
    }
    assert _prompt_count(record) == 1
    frames = loaded.read_jsonl("rpc-transcript.jsonl")
    assert any(
        row["direction"] == "in"
        and row["frame"].get("type") == "extension_ui_request"
        and row["frame"].get("notifyType") == "error"
        and row["frame"].get("message") == FETCH_REFUSAL
        for row in frames
    )


def test_info_notify_before_turn_does_not_abort(tmp_path: Path) -> None:
    outcome, elapsed, loaded, _record = _drive(
        tmp_path,
        timeout_s=3,
        events=[_notify("session ready", "info")],
        work_state={"work_id": WORK_ID, "state": "completed"},
    )
    assert outcome == "completed"
    assert elapsed < 2
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}


def test_error_notify_after_agent_start_does_not_abort(tmp_path: Path) -> None:
    outcome, elapsed, loaded, _record = _drive(
        tmp_path,
        timeout_s=3,
        events=[{"type": "agent_start"}, _notify("late", "error")],
        work_state={"work_id": WORK_ID, "state": "completed"},
    )
    assert outcome == "completed"
    assert elapsed < 2
    assert loaded.read_json("outcome.json") == {"outcome": "completed", "reason": "terminal", "prompts_sent": 1}
