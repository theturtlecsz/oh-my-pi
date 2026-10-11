from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from omp_work.model_proxy_upstream import (
    GEMINI,
    OLLAMA,
    ModelProxyUpstream,
    ModelRoute,
    TransportResponse,
    UpstreamRefused,
    UpstreamUnmeasured,
    prepare,
)
from omp_work.operations.config import OperationsConfig

KEY = "provider-key-SECRET-123"
WORKER_TOKEN = "worker-token-ABC"


class FakeTransport:
    def __init__(self, status=200, body=b"", headers=(), url=None, error=None):
        self.status, self.body, self.headers = status, body, tuple(headers)
        self.url, self.error = url, error
        self.calls: list[tuple[str, dict[str, str], bytes, int]] = []

    def post(self, url, headers, body, *, max_bytes, timeout):
        self.calls.append((url, dict(headers), body, max_bytes))
        if self.error:
            raise self.error
        return TransportResponse(
            self.status, self.headers, self.body, self.url or url
        )


@pytest.fixture
def config(tmp_path: Path) -> OperationsConfig:
    creds = tmp_path / "config" / "credentials"
    creds.mkdir(parents=True, mode=0o700)
    (creds / "gemini-key").write_text(KEY)
    (creds / "gemini-key").chmod(0o600)
    (creds / "ollama-key").write_text(KEY)
    (creds / "ollama-key").chmod(0o600)
    return OperationsConfig(
        config_dir=tmp_path / "config",
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
    )


def gemini_route(**kw) -> ModelRoute:
    base = dict(
        api=GEMINI,
        model="gemini-flash",
        origin="https://generativelanguage.googleapis.com",
        key_file="gemini-key",
        context_tokens=100_000,
        max_output_tokens=1000,
        input_usd=Decimal("0.30"),
        output_usd=Decimal("2.50"),
        cache_usd=Decimal("0.03"),
        price_version="p1",
        thinking_tokens=500,
    )
    base.update(kw)
    return ModelRoute(**base)


def ollama_route(**kw) -> ModelRoute:
    base = dict(
        api=OLLAMA,
        model="gpt-oss:120b",
        origin="https://ollama.com",
        key_file="ollama-key",
        context_tokens=100_000,
        max_output_tokens=1000,
        input_usd=Decimal("1"),
        output_usd=Decimal("4"),
        cache_usd=Decimal("0.5"),
        price_version="o1",
    )
    base.update(kw)
    return ModelRoute(**base)


def body(**extra) -> bytes:
    return json.dumps(
        {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], **extra}
    ).encode()


def ollama_body(**extra) -> bytes:
    return json.dumps(
        {"messages": [{"role": "user", "content": "hi"}], **extra}
    ).encode()


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\r\n\r\n" for c in chunks)


def jsonl(*chunks: dict) -> bytes:
    return b"".join(json.dumps(c).encode() + b"\n" for c in chunks)


GEMINI_TOOL_TURN = sse(
    {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "plan", "thought": True, "thoughtSignature": "SIG1"},
                        {
                            "functionCall": {"name": "ls", "args": {"p": "."}},
                            "thoughtSignature": "SIG2",
                        },
                    ],
                }
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 1},
    },
    {
        "candidates": [{"finishReason": "STOP"}],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 400,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 70,
        },
    },
)

OLLAMA_TOOL_TURN = jsonl(
    {
        "message": {
            "role": "assistant",
            "thinking": "hmm",
            "tool_calls": [{"function": {"name": "ls", "arguments": {"p": "."}}}],
        },
        "done": False,
        "prompt_eval_count": 1000,
        "prompt_eval_cached_count": 400,
        "eval_count": 20,
    },
    {
        "message": {"role": "assistant", "content": ""},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 1000,
        "prompt_eval_cached_count": 400,
        "eval_count": 100,
    },
)


def test_gemini_tool_turn_counts_usage_once_and_keeps_bytes(config):
    transport = FakeTransport(body=GEMINI_TOOL_TURN)
    up = ModelProxyUpstream(config, transport)
    prepared = prepare(gemini_route(), body(), worker_headers={"Authorization": WORKER_TOKEN})
    result = up.invoke(prepared)
    url, headers, sent, _ = transport.calls[0]
    assert url == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-flash:streamGenerateContent?alt=sse"
    )
    assert headers["x-goog-api-key"] == KEY
    assert WORKER_TOKEN not in json.dumps(headers) + sent.decode()
    assert result.body == GEMINI_TOOL_TURN
    assert b"SIG1" in result.body and b"SIG2" in result.body and b"functionCall" in result.body
    # last snapshot only: input 1000-400, output 30+70 (thoughts), cache 400
    assert (result.input_tokens, result.output_tokens, result.cache_tokens) == (600, 100, 400)
    assert result.usd == (Decimal("0.30") * 600 + Decimal("2.50") * 100 + Decimal("0.03") * 400) / 1_000_000
    assert result.price_version == "p1"
    assert result.content_type == "text/event-stream"


def test_ollama_tool_turn_input_is_prompt_minus_cached(config):
    transport = FakeTransport(body=OLLAMA_TOOL_TURN, headers=[("Content-Type", "application/x-ndjson")])
    up = ModelProxyUpstream(config, transport)
    result = up.invoke(prepare(ollama_route(), ollama_body()))
    url, headers, sent, _ = transport.calls[0]
    assert url == "https://ollama.com/api/chat"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(sent)["stream"] is True
    assert b"tool_calls" in result.body
    assert (result.input_tokens, result.output_tokens, result.cache_tokens) == (600, 100, 400)
    assert result.usd == (Decimal(600) * 1 + 100 * 4 + Decimal("0.5") * 400) / 1_000_000


def test_gemini_bound_includes_thinking_and_caps_options():
    route = gemini_route()
    prepared = prepare(route, body())
    sent = json.loads(prepared.body)
    assert sent["generationConfig"]["maxOutputTokens"] == 1000
    assert sent["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 500
    assert prepared.max_output_tokens == 1500
    assert prepared.max_input_tokens == route.context_tokens
    expected = (
        Decimal("0.30") * prepared.max_input_tokens + Decimal("2.50") * 1500
    ) / 1_000_000
    assert prepared.max_usd == expected
    for cfg in (
        {"maxOutputTokens": 1001},
        {"candidateCount": 2},
        {"thinkingConfig": {"thinkingBudget": 501}},
        {"thinkingConfig": {"thinkingBudget": -1}},
        {"cachedContent": "x"},
    ):
        with pytest.raises(UpstreamRefused):
            prepare(route, body(generationConfig=cfg))


@pytest.mark.parametrize("api", [GEMINI, OLLAMA])
def test_admission_reserves_context_at_higher_input_or_cache_rate(api):
    route = (gemini_route if api == GEMINI else ollama_route)(cache_usd=Decimal("9"))
    prepared = prepare(route, body() if api == GEMINI else ollama_body())
    assert prepared.max_input_tokens == route.context_tokens
    assert prepared.max_usd == (
        Decimal("9") * route.context_tokens
        + route.output_usd * prepared.max_output_tokens
    ) / 1_000_000


@pytest.mark.parametrize(
    "extra",
    [
        {"contents": [{"parts": [{"fileData": {"fileUri": "https://files.example/large.pdf"}}]}]},
        {"contents": [{"parts": [{"inlineData": {"mimeType": "image/png", "data": "AAAA"}}]}]},
        {"systemInstruction": {"parts": [{"fileData": {"fileUri": "https://files.example/doc"}}]}},
        {"tools": [{"googleSearch": {}}]},
        {"contents": [{"parts": [{"functionResponse": {"name": "read", "response": {}, "parts": [{"inlineData": {"data": "AAAA"}}]}}]}]},
    ],
)
def test_gemini_refuses_inputs_expanding_beyond_serialized_context(extra):
    with pytest.raises(UpstreamRefused, match="unbounded_option"):
        prepare(gemini_route(), body(**extra))


def test_gemini_rejects_oversized_input_instead_of_clamping_admission():
    with pytest.raises(UpstreamRefused, match="context_bound_exceeded"):
        prepare(
            gemini_route(context_tokens=2000),
            body(contents=[{"parts": [{"text": "x" * 2001}]}]),
        )


def test_gemini_bounded_function_history_preserves_signatures_and_results():
    contents = [
        {"role": "model", "parts": [{"functionCall": {"name": "read", "args": {}}, "thoughtSignature": "SIG"}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "read", "response": {"text": "result"}}}]},
    ]
    prepared = prepare(
        gemini_route(),
        body(contents=contents, tools=[{"functionDeclarations": [{"name": "read"}]}]),
    )
    assert json.loads(prepared.body)["contents"] == contents


def test_ollama_bound_and_option_refusals():
    prepared = prepare(ollama_route(), ollama_body())
    sent = json.loads(prepared.body)
    assert sent["options"]["num_predict"] == 1000 and prepared.max_output_tokens == 1000
    for extra in (
        {"options": {"num_predict": -1}},
        {"options": {"num_predict": 1001}},
        {"options": {"num_ctx": 200_000}},
        {"options": {"num_gpu": 1}},
        {"model": "other"},
        {"stream": False},
        {"keep_alive": -1},
    ):
        with pytest.raises(UpstreamRefused):
            prepare(ollama_route(), ollama_body(**extra))


@pytest.mark.parametrize("field", ["baseUrl", "url", "apiKey", "key", "price", "model"])
def test_worker_supplied_destination_key_price_refused(field):
    with pytest.raises(UpstreamRefused):
        prepare(gemini_route(), body(**{field: "https://evil.example"}))


@pytest.mark.parametrize(
    "origin",
    [
        "http://generativelanguage.googleapis.com",
        "https://u:p@generativelanguage.googleapis.com",
        "https://generativelanguage.googleapis.com/v1",
        "https://generativelanguage.googleapis.com?x=1",
        "https://169.254.169.254",
    ],
)
def test_foreign_route_origin_refused(origin):
    with pytest.raises(UpstreamRefused):
        prepare(gemini_route(origin=origin), body())


def test_redirect_and_foreign_final_url_refused_not_followed(config):
    prepared = prepare(gemini_route(), body())
    redirect = FakeTransport(status=302, headers=[("Location", "https://evil.example/")])
    with pytest.raises(UpstreamUnmeasured):
        ModelProxyUpstream(config, redirect).invoke(prepared)
    assert len(redirect.calls) == 1
    foreign = FakeTransport(body=GEMINI_TOOL_TURN, url="https://evil.example/x")
    with pytest.raises(UpstreamUnmeasured):
        ModelProxyUpstream(config, foreign).invoke(prepared)


def test_oversized_response_refused(config):
    prepared = prepare(gemini_route(max_response_bytes=100), body())
    transport = FakeTransport(body=GEMINI_TOOL_TURN)
    with pytest.raises(UpstreamUnmeasured):
        ModelProxyUpstream(config, transport).invoke(prepared)
    assert transport.calls[0][3] == 100


@pytest.mark.parametrize(
    "api,payload",
    [
        (GEMINI, sse({"candidates": []})),
        (GEMINI, sse({"usageMetadata": {"candidatesTokenCount": 3}})),
        (GEMINI, sse({"usageMetadata": {"promptTokenCount": "9"}})),
        (GEMINI, sse({"usageMetadata": {"promptTokenCount": 5, "cachedContentTokenCount": 9}})),
        (OLLAMA, jsonl({"message": {"content": "x"}, "done": False})),
        (OLLAMA, jsonl({"done": True, "eval_count": 3})),
        (OLLAMA, jsonl({"done": True, "prompt_eval_count": 2, "prompt_eval_cached_count": 5, "eval_count": 1})),
        (OLLAMA, b"not json\n"),
    ],
)
def test_missing_or_malformed_usage_is_unmeasured(config, api, payload):
    if api == GEMINI:
        prepared = prepare(gemini_route(), body())
    else:
        prepared = prepare(ollama_route(), ollama_body())
    with pytest.raises(UpstreamUnmeasured):
        ModelProxyUpstream(config, FakeTransport(body=payload)).invoke(prepared)
    assert prepared.max_usd > 0  # caller charges this admitted bound


def test_errors_never_carry_key(config):
    prepared = prepare(gemini_route(), body())
    leaky = FakeTransport(error=RuntimeError(f"boom key={KEY}"))
    with pytest.raises(UpstreamUnmeasured) as info:
        ModelProxyUpstream(config, leaky).invoke(prepared)
    assert KEY not in str(info.value) and KEY not in repr(info.value.__cause__)
    assert KEY not in repr(info.value.__context__)
    status = FakeTransport(status=500, body=KEY.encode())
    with pytest.raises(UpstreamUnmeasured) as info:
        ModelProxyUpstream(config, status).invoke(prepared)
    assert KEY not in str(info.value)


@pytest.mark.parametrize("api", [GEMINI, OLLAMA])
@pytest.mark.parametrize("payload", [KEY.encode() + b"\xff", b'{"key":"' + KEY.encode() + b'", invalid}'])
def test_malformed_provider_response_drops_secret_bearing_decoder_errors(config, api, payload):
    prepared = prepare(
        gemini_route() if api == GEMINI else ollama_route(),
        body() if api == GEMINI else ollama_body(),
    )
    if api == GEMINI:
        payload = b"data: " + payload + b"\n\n"
    with pytest.raises(UpstreamUnmeasured, match="malformed_response") as info:
        ModelProxyUpstream(config, FakeTransport(body=payload)).invoke(prepared)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert KEY not in str(info.value)


@pytest.mark.parametrize(
    "payload",
    [
        sse(
            {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 1}},
            {"candidates": [{"content": {"parts": [{"text": "later output"}]}, "finishReason": "STOP"}]},
        ),
        sse({"candidates": [{"content": {"parts": [{"text": "output"}]}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 5}}),
        sse({"candidates": [{"content": {"parts": [{"text": "truncated"}]}}], "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}}),
    ],
)
def test_gemini_stale_incomplete_or_partial_usage_requires_bound_charge(config, payload):
    prepared = prepare(gemini_route(), body())
    with pytest.raises(UpstreamUnmeasured):
        ModelProxyUpstream(config, FakeTransport(body=payload)).invoke(prepared)


def test_gemini_accepts_final_usage_only_chunk_after_completion(config):
    payload = sse(
        {"candidates": [{"content": {"parts": [{"text": "done"}]}, "finishReason": "STOP"}]},
        {"usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2}},
    )
    result = ModelProxyUpstream(config, FakeTransport(body=payload)).invoke(
        prepare(gemini_route(), body())
    )
    assert (result.input_tokens, result.output_tokens) == (5, 2)


def test_missing_key_file_refused_without_sending(config):
    prepared = prepare(gemini_route(key_file="absent"), body())
    transport = FakeTransport(body=GEMINI_TOOL_TURN)
    with pytest.raises(UpstreamRefused):
        ModelProxyUpstream(config, transport).invoke(prepared)
    assert transport.calls == []


def test_invalid_credential_encoding_refused_without_leaking_or_sending(config):
    config.secret_path("gemini-key").write_bytes(KEY.encode() + b"\xff")
    transport = FakeTransport(body=GEMINI_TOOL_TURN)
    with pytest.raises(UpstreamRefused, match="key_unavailable") as info:
        ModelProxyUpstream(config, transport).invoke(prepare(gemini_route(), body()))
    assert info.value.__cause__ is None and info.value.__context__ is None
    assert transport.calls == []
