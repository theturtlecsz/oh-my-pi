from __future__ import annotations

from importlib.resources import files
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from omp_work.v1.canonical import canonical_json, text_sha256

from ..errors import KnowledgeError
from .models import Lesson, StrictModel

MAX_LESSONS = 8
MAX_RESPONSE_BYTES = 64 * 1024
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GENERATION_PROMPT_RESOURCE = "generation_prompt.md"


class GeneratorError(KnowledgeError):
    """Base error for the trace-to-lesson generator seam."""

    retryable = True

    @property
    def error_code(self) -> str:
        return self.code


class GeneratorUnavailable(GeneratorError):
    """The local generator could not be reached or returned a server error."""

    code = "generator_unavailable"
    status_code = 503

    def __init__(self, message: str = "generator_unavailable: local generator not reachable") -> None:
        super().__init__(message)


class MalformedGeneratorOutput(GeneratorError):
    """The local generator answered, but its content is not usable."""

    code = "malformed_generator_output"
    status_code = 502

    def __init__(
        self, message: str = "malformed_generator_output: local generator returned unusable content"
    ) -> None:
        super().__init__(message)


class GenerationResult(StrictModel):
    """A generator run: the attributed lessons (or the reason none were found)
    plus the hashes of the exact request and response content.
    """

    model: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    response_sha256: str = Field(pattern=_SHA256_PATTERN)
    lessons: tuple[Lesson, ...] = Field(default=(), max_length=MAX_LESSONS)
    no_lesson_reason: str | None = None

    @field_validator("model", "profile")
    @classmethod
    def _validate_attribution(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _validate_outcome(self) -> GenerationResult:
        if self.lessons:
            if self.no_lesson_reason is not None:
                raise ValueError("no_lesson_reason must be omitted when lessons are present")
            return self
        if self.no_lesson_reason is None or not self.no_lesson_reason.strip():
            raise ValueError("lessons must not be empty unless no_lesson_reason is provided")
        return self


class _GeneratorPayload(StrictModel):
    lessons: tuple[Lesson, ...] = ()
    no_lesson_reason: str | None = None


class _ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str


class _ChatChoice(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _ChatMessage


class _ChatCompletion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    choices: tuple[_ChatChoice, ...] = Field(min_length=1)


class LessonGenerator(Protocol):
    """Abstract seam for turning one execution trace into lessons."""

    def generate(self, trace: dict[str, Any]) -> GenerationResult:
        ...


def load_generation_prompt() -> str:
    """Read the packaged trace-to-lesson prompt."""
    return (files("omp_knowledge.learning") / _GENERATION_PROMPT_RESOURCE).read_text(encoding="utf-8")


class LocalChatGenerator:
    """OpenAI-compatible local chat client for lesson generation.

    ``base_url`` must point at a loopback host so a trace can never be silently
    sent to a hosted provider. The request body carries the packaged prompt plus
    the canonical trace JSON; the assistant content is parsed as the
    ``{"lessons": [...], "no_lesson_reason": ...}`` envelope.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        profile: str,
        timeout: float = 60,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        host = urlsplit(base_url).hostname
        if host is None or host.lower() not in LOOPBACK_HOSTS:
            raise ValueError(
                f"loopback host required (127.0.0.1, localhost, ::1); refusing base_url {base_url!r}"
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._profile = profile
        self._timeout = timeout
        self._prompt = load_generation_prompt()
        self._client = httpx.Client(timeout=timeout, transport=transport, trust_env=False)

    def generate(self, trace: dict[str, Any]) -> GenerationResult:
        body_text = canonical_json(self._request_body(trace))
        request_sha256 = text_sha256(body_text)

        try:
            response = self._client.post(
                f"{self._base_url}/v1/chat/completions",
                content=body_text.encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise GeneratorUnavailable(
                f"generator_unavailable: timed out after {self._timeout}s"
            ) from exc
        except httpx.TransportError as exc:
            raise GeneratorUnavailable(f"generator_unavailable: transport failure: {exc}") from exc

        if response.is_error:
            raise GeneratorUnavailable(
                f"generator_unavailable: local generator returned HTTP {response.status_code}"
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise MalformedGeneratorOutput(
                f"malformed_generator_output: response exceeds {MAX_RESPONSE_BYTES} bytes"
            )

        content = self._assistant_content(response)
        response_sha256 = text_sha256(content)

        try:
            payload = _GeneratorPayload.model_validate_json(content)
            return GenerationResult(
                model=self._model,
                profile=self._profile,
                request_sha256=request_sha256,
                response_sha256=response_sha256,
                lessons=payload.lessons,
                no_lesson_reason=payload.no_lesson_reason,
            )
        except ValidationError as exc:
            raise MalformedGeneratorOutput(
                "malformed_generator_output: content is not a valid lesson envelope"
            ) from exc

    def close(self) -> None:
        self._client.close()

    def _request_body(self, trace: dict[str, Any]) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": self._prompt},
                {"role": "user", "content": canonical_json(trace)},
            ],
            "temperature": 0,
            "stream": False,
        }

    def _assistant_content(self, response: httpx.Response) -> str:
        try:
            completion = _ChatCompletion.model_validate(response.json())
        except (ValidationError, ValueError) as exc:
            raise MalformedGeneratorOutput(
                "malformed_generator_output: chat completion envelope is not usable"
            ) from exc
        content = completion.choices[0].message.content
        if not content.strip():
            raise MalformedGeneratorOutput(
                "malformed_generator_output: assistant content is empty"
            )
        return content


__all__ = [
    "GenerationResult",
    "GeneratorError",
    "GeneratorUnavailable",
    "LOOPBACK_HOSTS",
    "LessonGenerator",
    "LocalChatGenerator",
    "MAX_LESSONS",
    "MAX_RESPONSE_BYTES",
    "MalformedGeneratorOutput",
    "load_generation_prompt",
]
