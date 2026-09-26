from __future__ import annotations

import json

import httpx
import pytest

from omp_knowledge.learning.generation import (
    MAX_LESSONS,
    MAX_RESPONSE_BYTES,
    GenerationResult,
    GeneratorUnavailable,
    LocalChatGenerator,
    MalformedGeneratorOutput,
    load_generation_prompt,
)
from omp_knowledge.learning.models import Lesson
from omp_work.v1.canonical import canonical_json, text_sha256
from support.stub_generator import StubGenerator

LOOPBACK_BASE_URL = "http://127.0.0.1:18080"


def chat_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "qwen3.8-27b-q5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        },
    )


def make_generator(handler: httpx.MockTransport, *, base_url: str = LOOPBACK_BASE_URL) -> LocalChatGenerator:
    return LocalChatGenerator(base_url, model="qwen3.8-27b-q5", profile="local-qwen", transport=handler)


def lesson_payload() -> dict[str, object]:
    return {
        "title": "Restart the worker before re-running the flaky lane",
        "steps": ["stop the worker", "clear the lease", "re-run the lane"],
        "preconditions": [{"key": "repository", "op": "eq", "value": "org/repo"}],
        "claims": [{"text": "The lease outlived the crash", "receipt_ids": ["receipt-1"]}],
    }


def test_valid_content_yields_lessons_attribution_and_hashes() -> None:
    """A well-formed envelope yields the lessons plus model, profile, and both sha256 values."""
    trace = {"unit_id": "u-1", "events": [{"kind": "exec", "exit_code": 1}]}
    content = canonical_json({"lessons": [lesson_payload()], "no_lesson_reason": None})
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["method"] = request.method
        captured["body"] = request.content.decode("utf-8")
        return chat_response(content)

    generator = make_generator(httpx.MockTransport(handler))
    result = generator.generate(trace)
    generator.close()

    assert isinstance(result, GenerationResult)
    assert captured["path"] == "/v1/chat/completions"
    assert captured["method"] == "POST"

    body = json.loads(str(captured["body"]))
    assert body["model"] == "qwen3.8-27b-q5"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][0]["content"] == load_generation_prompt()
    assert body["messages"][1] == {"role": "user", "content": canonical_json(trace)}

    assert result.model == "qwen3.8-27b-q5"
    assert result.profile == "local-qwen"
    assert result.request_sha256 == text_sha256(str(captured["body"]))
    assert result.response_sha256 == text_sha256(content)
    assert result.no_lesson_reason is None
    assert len(result.lessons) == 1
    assert result.lessons[0].title == "Restart the worker before re-running the flaky lane"
    assert result.lessons[0].claims[0].receipt_ids == ("receipt-1",)


def test_empty_lessons_with_reason_is_a_no_lesson_outcome() -> None:
    """lessons [] plus a no_lesson_reason is a valid no-lesson result, not an error."""
    content = canonical_json({"lessons": [], "no_lesson_reason": "Trace shows no transferable technique."})

    result = make_generator(httpx.MockTransport(lambda _: chat_response(content))).generate({"unit_id": "u"})

    assert result.lessons == ()
    assert result.no_lesson_reason == "Trace shows no transferable technique."


def test_empty_lessons_without_reason_is_malformed() -> None:
    """lessons [] alone is rejected; the seam never returns a silent empty result."""
    content = canonical_json({"lessons": []})

    with pytest.raises(MalformedGeneratorOutput):
        make_generator(httpx.MockTransport(lambda _: chat_response(content))).generate({"unit_id": "u"})


@pytest.mark.parametrize(
    ("label", "response"),
    [
        ("empty content", chat_response("")),
        ("blank content", chat_response("   \n  ")),
        ("non-JSON", chat_response("I could not find any lessons, sorry.")),
        ("schema-invalid lesson", chat_response(canonical_json({"lessons": [{"steps": ["a"]}]}))),
        (
            "too many lessons",
            chat_response(
                canonical_json({"lessons": [lesson_payload() | {"title": f"lesson {i}"} for i in range(MAX_LESSONS + 1)]})
            ),
        ),
        ("no choices", httpx.Response(200, json={"object": "chat.completion", "choices": []})),
        (
            "oversize response",
            chat_response("x" * (MAX_RESPONSE_BYTES + 1)),
        ),
        ("reason with lessons", chat_response(canonical_json({"lessons": [lesson_payload()], "no_lesson_reason": "x"}))),
    ],
)
def test_malformed_generator_output(label: str, response: httpx.Response) -> None:
    """Empty, garbage, schema-invalid, oversize, and over-limit content all surface as malformed output."""
    with pytest.raises(MalformedGeneratorOutput) as excinfo:
        make_generator(httpx.MockTransport(lambda _: response)).generate({"unit_id": "u"})

    assert excinfo.value.error_code == "malformed_generator_output"
    assert excinfo.value.retryable is True


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.Response(503, json={"error": {"message": "model loading"}}),
    ],
)
def test_unavailable_transport_and_server_errors(failure: httpx.Response | Exception) -> None:
    """A connect error, a timeout, and an HTTP 5xx all surface as GeneratorUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(failure, Exception):
            raise failure
        return failure

    with pytest.raises(GeneratorUnavailable) as excinfo:
        make_generator(httpx.MockTransport(handler)).generate({"unit_id": "u"})

    assert excinfo.value.error_code == "generator_unavailable"
    assert excinfo.value.retryable is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.openai.com",
        "https://generator.example.com:18080",
        "http://10.0.0.5:18080",
        "http://[::2]:18080",
    ],
)
def test_non_loopback_base_url_is_rejected(base_url: str) -> None:
    """A non-loopback host is refused at construction so a trace cannot reach a hosted provider."""
    with pytest.raises(ValueError, match="loopback"):
        LocalChatGenerator(base_url, model="m", profile="p")


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:18080", "http://localhost:8080", "http://[::1]:18080"],
)
def test_loopback_base_url_is_accepted(base_url: str) -> None:
    """Loopback spellings are all allowed."""
    generator = LocalChatGenerator(base_url, model="m", profile="p", transport=httpx.MockTransport(lambda _: chat_response("{}")))
    generator.close()


def test_generation_prompt_is_packaged() -> None:
    """The prompt ships as package data and names the JSON envelope contract."""
    prompt = load_generation_prompt()
    assert prompt.strip()
    assert "lessons" in prompt
    assert "no_lesson_reason" in prompt


def test_stub_generator_replays_script_and_records_calls() -> None:
    """The shared stub returns scripted results, raises scripted errors, and records each trace."""
    first = GenerationResult(
        model="m",
        profile="p",
        request_sha256="a" * 64,
        response_sha256="b" * 64,
        lessons=(Lesson(title="t", steps=("s",)),),
    )
    stub = StubGenerator([first, GeneratorUnavailable("boom")])

    assert stub.generate({"unit_id": "u-1"}) is first
    with pytest.raises(GeneratorUnavailable):
        stub.generate({"unit_id": "u-2"})
    # The script runs out: the last entry is reused rather than silently returning None.
    with pytest.raises(GeneratorUnavailable):
        stub.generate({"unit_id": "u-3"})
    assert [call["unit_id"] for call in stub.calls] == ["u-1", "u-2", "u-3"]
