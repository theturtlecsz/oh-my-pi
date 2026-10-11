"""Model proxy provider transport.

Trusted route config names the provider API, model, HTTPS origin, key-file
reference, limits and USD rates. The worker supplies only the native request
body: a worker URL, key, price or auth header is refused or dropped. The
provider key is read through OperationsConfig.read_secret and attached in
``invoke`` only. No redirect is followed and no request is retried.
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Protocol
from urllib.parse import urlsplit

from omp_work.egress_policy import blocked_address
from omp_work.operations.config import OperationsConfig

GEMINI = "google-generative-ai"
OLLAMA = "ollama-chat"
APIS = (GEMINI, OLLAMA)

_MILLION = Decimal(1_000_000)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SECRET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_BAD_KEY_CHARS = re.compile(r"[\x00-\x20\x7f]")

_GEMINI_TOP = {
    "contents",
    "systemInstruction",
    "tools",
    "toolConfig",
    "generationConfig",
    "safetySettings",
}
_GEMINI_GENERATION = {
    "maxOutputTokens",
    "temperature",
    "topP",
    "topK",
    "stopSequences",
    "candidateCount",
    "responseMimeType",
    "responseSchema",
    "thinkingConfig",
    "seed",
}
_GEMINI_THINKING = {"thinkingBudget", "includeThoughts"}
_OLLAMA_TOP = {"model", "messages", "tools", "think", "format", "options", "stream"}
_OLLAMA_OPTIONS = {
    "num_predict",
    "num_ctx",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "seed",
    "stop",
    "repeat_penalty",
}


class UpstreamError(Exception):
    """Provider transport failure. The message never carries a key."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UpstreamRefused(UpstreamError):
    """Refused before anything was sent: charge nothing."""


class UpstreamUnmeasured(UpstreamError):
    """Request may have reached the provider but usage is not measurable.

    The caller charges the admitted bound (``Prepared.max_usd``/``max_tokens``).
    """


@dataclass(frozen=True)
class ModelRoute:
    """Trusted route config. Rates are USD per million tokens."""

    api: str
    model: str
    origin: str
    key_file: str
    context_tokens: int
    max_output_tokens: int
    input_usd: Decimal
    output_usd: Decimal
    cache_usd: Decimal
    price_version: str
    thinking_tokens: int = 0
    max_response_bytes: int = 4 * 1024 * 1024
    timeout_s: float = 120.0


@dataclass(frozen=True)
class Prepared:
    route: ModelRoute
    url: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    max_input_tokens: int
    max_output_tokens: int
    max_usd: Decimal

    @property
    def max_tokens(self) -> int:
        return self.max_input_tokens + self.max_output_tokens


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    url: str


class Transport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        max_bytes: int,
        timeout: float,
    ) -> TransportResponse:
        """POST once, never follow a redirect, read at most max_bytes + 1."""


class HttpsTransport:
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        *,
        max_bytes: int,
        timeout: float,
    ) -> TransportResponse:
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        conn = http.client.HTTPSConnection(
            parts.hostname or "", parts.port or 443, timeout=timeout
        )
        try:
            conn.request("POST", target, body=body, headers=dict(headers))
            response = conn.getresponse()
            data = response.read(max_bytes + 1)
            return TransportResponse(
                response.status, tuple(response.getheaders()), data, url
            )
        finally:
            conn.close()


@dataclass(frozen=True)
class UpstreamResult:
    body: bytes
    content_type: str
    input_tokens: int
    output_tokens: int
    cache_tokens: int
    usd: Decimal
    price_version: str


def _nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _pos_int(value: object) -> bool:
    return _nonneg_int(value) and value > 0  # type: ignore[operator]


def validate_route(route: ModelRoute) -> None:
    if route.api not in APIS:
        raise UpstreamRefused("unsupported_api")
    if not isinstance(route.model, str) or not _MODEL_ID.fullmatch(route.model):
        raise UpstreamRefused("invalid_model")
    if not isinstance(route.key_file, str) or not _SECRET_NAME.fullmatch(
        route.key_file
    ):
        raise UpstreamRefused("invalid_key_file")
    if not route.price_version or not isinstance(route.price_version, str):
        raise UpstreamRefused("missing_price_version")
    if not (_pos_int(route.context_tokens) and _pos_int(route.max_output_tokens)):
        raise UpstreamRefused("invalid_limits")
    if not _nonneg_int(route.thinking_tokens) or not _pos_int(
        route.max_response_bytes
    ):
        raise UpstreamRefused("invalid_limits")
    if route.max_output_tokens + route.thinking_tokens > route.context_tokens:
        raise UpstreamRefused("invalid_limits")
    for rate in (route.input_usd, route.output_usd, route.cache_usd):
        if not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0:
            raise UpstreamRefused("invalid_rate")
    if not route.timeout_s > 0:
        raise UpstreamRefused("invalid_timeout")
    _check_origin(route.origin)


def _check_origin(origin: str) -> None:
    try:
        parts = urlsplit(origin)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as error:
        raise UpstreamRefused("invalid_origin") from error
    if (
        parts.scheme != "https"
        or not host
        or "@" in parts.netloc
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or origin.endswith("?")
        or origin.endswith("#")
    ):
        raise UpstreamRefused("invalid_origin")
    if re.fullmatch(r"[\d.]+|[0-9a-fA-F:]+", host) and blocked_address(host):
        raise UpstreamRefused("foreign_destination")


def _origin_base(origin: str) -> str:
    return origin.rstrip("/")


def _object(value: object, reason: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UpstreamRefused(reason)
    return value


def _loads(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise UpstreamRefused("malformed_request") from error
    return _object(parsed, "malformed_request")


def _capped(
    section: dict[str, Any], key: str, limit: int, reason: str
) -> int:
    """Return the requested cap, or limit when absent. Refuse a cap above limit."""
    if key not in section:
        return limit
    value = section[key]
    if not _pos_int(value) or value > limit:
        raise UpstreamRefused(reason)
    return value


def _refuse_unknown(section: dict[str, Any], allowed: set[str], reason: str) -> None:
    extra = set(section) - allowed
    if extra:
        raise UpstreamRefused(reason)


def _prepare_gemini(route: ModelRoute, request: dict[str, Any]) -> tuple[dict, int]:
    _refuse_unknown(request, _GEMINI_TOP, "disallowed_field")
    if not isinstance(request.get("contents"), list) or not request["contents"]:
        raise UpstreamRefused("malformed_request")
    # Gemini has no request-level input context cap. Only accept self-contained
    # text/function turns: media and server tools can expand a short request
    # into an input larger than the configured admission context.
    contents = list(request["contents"])
    if "systemInstruction" in request:
        contents.append(request["systemInstruction"])
    for content in contents:
        content = _object(content, "malformed_request")
        _refuse_unknown(content, {"role", "parts"}, "disallowed_field")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise UpstreamRefused("malformed_request")
        for part in parts:
            part = _object(part, "malformed_request")
            _refuse_unknown(
                part,
                {"text", "functionCall", "functionResponse", "thought", "thoughtSignature"},
                "unbounded_option",
            )
            if "text" in part and not isinstance(part["text"], str):
                raise UpstreamRefused("malformed_request")
            if "functionResponse" in part:
                response = _object(part["functionResponse"], "malformed_request")
                _refuse_unknown(response, {"id", "name", "response"}, "unbounded_option")
    tools = request.get("tools", [])
    if not isinstance(tools, list):
        raise UpstreamRefused("malformed_request")
    for tool in tools:
        _refuse_unknown(
            _object(tool, "malformed_request"), {"functionDeclarations"}, "unbounded_option"
        )
    config = _object(request.get("generationConfig", {}), "malformed_request")
    _refuse_unknown(config, _GEMINI_GENERATION, "disallowed_option")
    if config.get("candidateCount", 1) != 1:
        raise UpstreamRefused("unbounded_option")
    config["maxOutputTokens"] = _capped(
        config, "maxOutputTokens", route.max_output_tokens, "output_bound_exceeded"
    )
    thinking = _object(config.get("thinkingConfig", {}), "malformed_request")
    _refuse_unknown(thinking, _GEMINI_THINKING, "disallowed_option")
    if "includeThoughts" in thinking and not isinstance(
        thinking["includeThoughts"], bool
    ):
        raise UpstreamRefused("malformed_request")
    if "thinkingBudget" in thinking:
        budget = thinking["thinkingBudget"]
        # -1 asks for a dynamic budget, which defeats the configured bound.
        if not _nonneg_int(budget) or budget > route.thinking_tokens:
            raise UpstreamRefused("thinking_bound_exceeded")
    else:
        thinking["thinkingBudget"] = route.thinking_tokens
    config["thinkingConfig"] = thinking
    request["generationConfig"] = config
    return request, route.max_output_tokens + route.thinking_tokens


def _prepare_ollama(route: ModelRoute, request: dict[str, Any]) -> tuple[dict, int]:
    _refuse_unknown(request, _OLLAMA_TOP, "disallowed_field")
    if request.get("model", route.model) != route.model:
        raise UpstreamRefused("model_mismatch")
    if request.get("stream", True) is not True:
        raise UpstreamRefused("disallowed_option")
    if not isinstance(request.get("messages"), list) or not request["messages"]:
        raise UpstreamRefused("malformed_request")
    options = _object(request.get("options", {}), "malformed_request")
    _refuse_unknown(options, _OLLAMA_OPTIONS, "disallowed_option")
    # num_predict also covers thinking tokens on Ollama; -1/-2 mean unlimited.
    predict = _capped(
        options, "num_predict", route.max_output_tokens, "output_bound_exceeded"
    )
    options["num_predict"] = predict
    options["num_ctx"] = _capped(
        options, "num_ctx", route.context_tokens, "context_bound_exceeded"
    )
    request["options"] = options
    request["model"] = route.model
    request["stream"] = True
    return request, predict


def prepare(
    route: ModelRoute,
    body: bytes,
    *,
    worker_headers: Mapping[str, str] | None = None,
) -> Prepared:
    """Validate a worker request into a provider request plus admission bound.

    ``worker_headers`` is accepted only so callers can show it is discarded:
    no worker header, token or key ever reaches the provider request.
    """
    validate_route(route)
    request = _loads(body)
    if route.api == GEMINI:
        request, output_bound = _prepare_gemini(route, request)
        url = (
            f"{_origin_base(route.origin)}/v1beta/models/{route.model}"
            ":streamGenerateContent?alt=sse"
        )
    else:
        request, output_bound = _prepare_ollama(route, request)
        url = f"{_origin_base(route.origin)}/api/chat"
    payload = json.dumps(request, separators=(",", ":")).encode("utf-8")
    # Reserve the entire configured input context, including cached input.
    # Serialized size cannot estimate provider tokens (especially for media).
    # Gemini lacks num_ctx; conservatively reject text exceeding this byte
    # budget instead of silently clamping an oversized input reservation.
    if route.api == GEMINI and len(payload) > route.context_tokens:
        raise UpstreamRefused("context_bound_exceeded")
    input_bound = route.context_tokens
    rate = max(route.input_usd, route.cache_usd)
    usd = (rate * input_bound + route.output_usd * output_bound) / _MILLION
    return Prepared(
        route=route,
        url=url,
        headers=(("Content-Type", "application/json"),),
        body=payload,
        max_input_tokens=input_bound,
        max_output_tokens=output_bound,
        max_usd=usd,
    )


def _usd(route: ModelRoute, input_t: int, output_t: int, cache_t: int) -> Decimal:
    return (
        route.input_usd * input_t
        + route.output_usd * output_t
        + route.cache_usd * cache_t
    ) / _MILLION


def _json_lines(data: bytes, *, sse: bool) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    malformed = False
    try:
        text = data.decode("utf-8")
        for line in text.splitlines():
            line = line.strip()
            if sse:
                if not line.startswith("data:"):
                    continue
                line = line[5:].strip()
            if not line or line == "[DONE]":
                continue
            chunk = json.loads(line)
            if not isinstance(chunk, dict):
                malformed = True
                break
            chunks.append(chunk)
    except (ValueError, UnicodeDecodeError):
        malformed = True
    # Raise outside the handler: decoder errors retain the entire response,
    # which may include credentials echoed by the provider.
    if malformed:
        raise UpstreamUnmeasured("malformed_response")
    return chunks


def _gemini_usage(data: bytes) -> tuple[int, int, int]:
    chunks = _json_lines(data, sse=True)
    finished = False
    for chunk in chunks:
        if "error" in chunk:
            raise UpstreamUnmeasured("provider_error")
        candidates = chunk.get("candidates", [])
        if not isinstance(candidates, list) or any(
            not isinstance(candidate, dict) for candidate in candidates
        ):
            raise UpstreamUnmeasured("malformed_response")
        finished = finished or any(
            isinstance(candidate.get("finishReason"), str) and candidate["finishReason"]
            for candidate in candidates
        )
    # Snapshots are cumulative. Only a final snapshot after completion can
    # measure the whole turn; earlier usage cannot cover subsequent output.
    usage = chunks[-1].get("usageMetadata") if chunks else None
    if not isinstance(usage, dict):
        raise UpstreamUnmeasured("missing_usage")
    if not finished:
        raise UpstreamUnmeasured("incomplete_response")
    prompt = usage.get("promptTokenCount")
    candidates = usage.get("candidatesTokenCount")
    cached = usage.get("cachedContentTokenCount", 0)
    thoughts = usage.get("thoughtsTokenCount", 0)
    tool_prompt = usage.get("toolUsePromptTokenCount", 0)
    for value in (prompt, candidates, cached, thoughts, tool_prompt):
        if not _nonneg_int(value):
            raise UpstreamUnmeasured("malformed_usage")
    if cached > prompt:
        raise UpstreamUnmeasured("malformed_usage")
    return prompt - cached + tool_prompt, candidates + thoughts, cached


def _ollama_usage(data: bytes) -> tuple[int, int, int]:
    chunks = _json_lines(data, sse=False)
    if any("error" in chunk for chunk in chunks):
        raise UpstreamUnmeasured("provider_error")
    final = chunks[-1] if chunks else None
    if final is None or final.get("done") is not True:
        raise UpstreamUnmeasured("missing_usage")
    prompt = final.get("prompt_eval_count")
    output = final.get("eval_count")
    cached = final.get("prompt_eval_cached_count", 0)
    for value in (prompt, output, cached):
        if not _nonneg_int(value):
            raise UpstreamUnmeasured("malformed_usage")
    if cached > prompt:
        raise UpstreamUnmeasured("malformed_usage")
    # prompt_eval_count is the total prompt including cached tokens.
    return prompt - cached, output, cached


class ModelProxyUpstream:
    def __init__(
        self, config: OperationsConfig, transport: Transport | None = None
    ) -> None:
        self._config = config
        self._transport: Transport = transport or HttpsTransport()

    def invoke(self, prepared: Prepared) -> UpstreamResult:
        route = prepared.route
        key: str | None = None
        try:
            key = self._config.read_secret(route.key_file)
        except ValueError:
            # A credential decoding error can retain the secret bytes too.
            pass
        if key is None or _BAD_KEY_CHARS.search(key):
            raise UpstreamRefused("key_unavailable")
        headers = dict(prepared.headers)
        if route.api == GEMINI:
            headers["x-goog-api-key"] = key
        else:
            headers["Authorization"] = f"Bearer {key}"
        failure: str | None = None
        try:
            response = self._transport.post(
                prepared.url,
                headers,
                prepared.body,
                max_bytes=route.max_response_bytes,
                timeout=route.timeout_s,
            )
        except Exception as error:
            # Transport errors may echo request headers; keep the class only
            # and raise outside the handler so no traceback chain holds them.
            failure = type(error).__name__
        if failure is not None:
            raise UpstreamUnmeasured(f"transport_failed:{failure}")
        if response.url != prepared.url:
            raise UpstreamUnmeasured("foreign_destination")
        if 300 <= response.status < 400:
            raise UpstreamUnmeasured("redirect_refused")
        if response.status != 200:
            raise UpstreamUnmeasured(f"provider_status:{response.status}")
        if len(response.body) > route.max_response_bytes:
            raise UpstreamUnmeasured("response_too_large")
        if route.api == GEMINI:
            input_t, output_t, cache_t = _gemini_usage(response.body)
        else:
            input_t, output_t, cache_t = _ollama_usage(response.body)
        content_type = next(
            (v for k, v in response.headers if k.lower() == "content-type"),
            "text/event-stream" if route.api == GEMINI else "application/x-ndjson",
        )
        return UpstreamResult(
            body=response.body,
            content_type=content_type,
            input_tokens=input_t,
            output_tokens=output_t,
            cache_tokens=cache_t,
            usd=_usd(route, input_t, output_t, cache_t),
            price_version=route.price_version,
        )
