from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omp_knowledge.learning.generation import (
    LocalChatGenerator,
    ensure_repository_precondition,
    extract_trace_repository,
)
from omp_knowledge.learning.models import Lesson, Precondition

LOOPBACK_BASE_URL = "http://127.0.0.1:18080"


def _receipt(repository: str, *, truncated: bool = False, raw: str | None = None) -> dict[str, Any]:
    return {
        "payload_truncated": truncated,
        "payload_text": raw
        if raw is not None
        else json.dumps({"delivery": {"repository": repository}}),
    }


def _chat(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
        },
    )


def _generate(trace: dict[str, Any], content: str):
    generator = LocalChatGenerator(
        LOOPBACK_BASE_URL,
        model="qwen3.8-27b-q5",
        profile="local-qwen",
        transport=httpx.MockTransport(lambda request: _chat(content)),
    )
    try:
        return generator.generate(trace)
    finally:
        generator.close()


def _envelope(lesson: dict[str, Any]) -> str:
    return json.dumps({"lessons": [lesson], "no_lesson_reason": None})


@pytest.mark.parametrize(
    ("trace", "expected"),
    [
        (
            {"payload": {"evidence": {"delivery": {"repository": " org/payload "}}}},
            "org/payload",
        ),
        (
            {
                "work_record": {
                    "close_attempts": [
                        {"repository": " "},
                        {"repository": " org/attempt "},
                    ]
                }
            },
            "org/attempt",
        ),
        (
            {"work_record": {"receipts": [_receipt(" org/receipt ")]}},
            "org/receipt",
        ),
    ],
)
def test_extract_trace_repository_reads_each_shape(trace: dict[str, Any], expected: str) -> None:
    """Each naming shape yields its stripped repository."""
    assert extract_trace_repository(trace) == expected


def test_extract_trace_repository_first_match_wins() -> None:
    """Payload beats close attempts, which beat receipts; blanks are not matches."""
    trace: dict[str, Any] = {
        "repository": "org/top",
        "payload": {"evidence": {"delivery": {"repository": "org/payload"}}},
        "work_record": {
            "repository": "org/record",
            "close_attempts": [
                {"repository": "org/attempt-1"},
                {"repository": "org/attempt-2"},
            ],
            "receipts": [_receipt("org/receipt")],
        },
    }
    assert extract_trace_repository(trace) == "org/payload"

    trace["payload"]["evidence"]["delivery"]["repository"] = "   "
    assert extract_trace_repository(trace) == "org/attempt-1"

    trace["work_record"]["close_attempts"] = [
        None,
        {"repository": ""},
        {"repository": "org/attempt-2"},
    ]
    assert extract_trace_repository(trace) == "org/attempt-2"

    trace["work_record"]["close_attempts"] = [{"repository": None}, {"repository": "  "}]
    assert extract_trace_repository(trace) == "org/receipt"


def test_extract_trace_repository_skips_truncated_and_unparseable_receipts() -> None:
    """A later parseable receipt is used when earlier receipts cannot name a repository."""
    trace = {
        "work_record": {
            "receipts": [
                _receipt("org/truncated", truncated=True),
                _receipt("org/bad", raw="{not-json"),
                _receipt("org/array", raw=json.dumps(["org/array"])),
                {
                    "payload_truncated": False,
                    "payload_text": json.dumps({"delivery": {"repository": "   "}}),
                },
                _receipt("org/kept"),
            ]
        }
    }
    assert extract_trace_repository(trace) == "org/kept"


@pytest.mark.parametrize(
    "trace",
    [
        {},
        {"work_record": None},
        {"payload": {}, "work_record": {}},
        {"repository": "org/top"},
        {"work_record": {"repository": "org/record", "close_attempts": [], "receipts": []}},
        {"payload": {"evidence": {"delivery": {"repository": "  "}}}},
        {"work_record": {"close_attempts": [{"repository": ""}, {}]}},
        {
            "work_record": {
                "receipts": [
                    _receipt("org/truncated", truncated=True),
                    _receipt("org/bad", raw="not json"),
                ]
            }
        },
    ],
)
def test_extract_trace_repository_none_when_unnamed(trace: dict[str, Any]) -> None:
    """A trace that never names a repository through the three shapes yields None."""
    assert extract_trace_repository(trace) is None


def test_ensure_repository_precondition_replaces_repository_axis() -> None:
    """eq R is added, model repository eq and ne are dropped, and project_id stays."""
    lesson = Lesson(
        title="Restart the worker",
        steps=("stop the worker",),
        preconditions=(
            Precondition(key="project_id", op="eq", value="proj-1"),
            Precondition(key="repository", op="eq", value="org/wrong"),
            Precondition(key="project_id", op="ne", value="proj-other"),
            Precondition(key="repository", op="ne", value="org/also-wrong"),
        ),
    )
    updated = ensure_repository_precondition(lesson, "org/repo")

    assert updated.preconditions == (
        Precondition(key="project_id", op="eq", value="proj-1"),
        Precondition(key="project_id", op="ne", value="proj-other"),
        Precondition(key="repository", op="eq", value="org/repo"),
    )
    assert updated.title == lesson.title
    assert lesson.preconditions[1].value == "org/wrong"

    bare = Lesson(title="t", steps=("s",))
    assert ensure_repository_precondition(bare, "org/repo").preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_stamps_repository_from_close_attempt() -> None:
    """A model lesson with no preconditions gains eq for the trace repository."""
    trace = {"work_record": {"close_attempts": [{"repository": "org/repo"}]}}
    result = _generate(trace, _envelope({"title": "Restart the worker", "steps": ["stop the worker"]}))

    assert result.lessons[0].preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_replaces_wrong_model_repository() -> None:
    """A model repository eq is replaced by the repository the trace names."""
    trace = {"work_record": {"close_attempts": [{"repository": "org/repo"}]}}
    content = _envelope(
        {
            "title": "Restart the worker",
            "steps": ["stop the worker"],
            "preconditions": [{"key": "repository", "op": "eq", "value": "org/wrong"}],
        }
    )
    result = _generate(trace, content)

    assert result.lessons[0].preconditions == (
        Precondition(key="repository", op="eq", value="org/repo"),
    )


def test_generate_leaves_preconditions_when_trace_names_no_repository() -> None:
    """Lessons pass through unchanged when the trace names no repository."""
    content = _envelope(
        {
            "title": "Restart the worker",
            "steps": ["stop the worker"],
            "preconditions": [
                {"key": "project_id", "op": "eq", "value": "proj-1"},
                {"key": "repository", "op": "eq", "value": "org/model"},
            ],
        }
    )
    result = _generate({"unit_id": "u-1"}, content)

    assert result.lessons[0].preconditions == (
        Precondition(key="project_id", op="eq", value="proj-1"),
        Precondition(key="repository", op="eq", value="org/model"),
    )
