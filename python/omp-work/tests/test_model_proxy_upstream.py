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


def test_missing_key_file_refused_without_sending(config):
    prepared = prepare(gemini_route(key_file="absent"), body())
    transport = FakeTransport(body=GEMINI_TOOL_TURN)
    with pytest.raises(UpstreamRefused):
        ModelProxyUpstream(config, transport).invoke(prepared)
    assert transport.calls == []
