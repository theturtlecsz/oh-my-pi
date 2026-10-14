"""Label-blind intake support judge, pin, and test hook (OMP-604-s04)."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from omp_work.frozen_corpus import verify
from omp_work.intake_support import verify_receipt
from omp_work.intake_support_judge import (
    BUDGET_EXCEEDED,
    ECHO_MODEL_ID,
    JUDGE_UNPARSABLE,
    MIXED_TOKEN_COUNTS,
    MODEL_GRANT_REQUIRED,
    Completion,
    IntakeJudgeRefusal,
    JudgeHook,
    complete_from_env,
    current_hook,
    echo_receipt,
    judge_link,
    prompt_path,
    use_judge_hook,
)
from omp_work.v1.canonical import text_sha256

ROOT = Path(__file__).resolve().parents[3] / "evals" / "intake_support" / "v1"
INPUTS = ROOT / "inputs"
MANIFEST = ROOT / "manifest.json"
PIN = ROOT / "tuning-pin.json"
CLAIM = "the bridge span carries six lanes"
PASSAGE = "東京 quote"
CONTEXT = "The quote is a hypothetical in the draft, not a finding."
MODEL = "deepseek-v4.1-flash:cloud"


def _complete(text: str, input_tokens: int | None, output_tokens: int | None):
    def complete(prompt: str, model_id: str) -> Completion:
        complete.prompts.append(prompt)  # type: ignore[attr-defined]
        complete.models.append(model_id)  # type: ignore[attr-defined]
        return Completion(text, input_tokens, output_tokens)

    complete.prompts = []  # type: ignore[attr-defined]
    complete.models = []  # type: ignore[attr-defined]
    return complete


def _settle():
    seen: list[Completion] = []

    def settle(completion: Completion) -> str:
        seen.append(completion)
        return "usage-1"

    settle.seen = seen  # type: ignore[attr-defined]
    return settle


def _link(
    complete,
    settle,
    *,
    model_id: str = MODEL,
    remaining_usd: Decimal | int | float = Decimal("1.50"),
    claim_text: str = CLAIM,
    passage: str = PASSAGE,
    context: str = CONTEXT,
):
    return judge_link(
        claim_text=claim_text,
        passage=passage,
        context=context,
        model_id=model_id,
        remaining_usd=remaining_usd,
        complete=complete,
        settle=settle,
    )


def test_prompt_has_only_claim_passage_context_placeholders() -> None:
    text = prompt_path().read_text(encoding="utf-8")
    assert "relation" not in text.lower()
    assert re.findall(r"\{\{[^}]+\}\}", text) == ["{{claim}}", "{{passage}}", "{{context}}"]


def test_pin_matches_sealed_corpus_and_prompt_bytes() -> None:
    sealed = verify(INPUTS, MANIFEST)
    pin = json.loads(PIN.read_bytes())
    assert pin["tree_sha256"] == sealed.tree_sha256
    assert pin["manifest_sha256"] == sealed.manifest_sha256
    assert pin["prompt_sha256"] == hashlib.sha256(prompt_path().read_bytes()).hexdigest()


def test_signature_takes_no_relation() -> None:
    params = inspect.signature(judge_link).parameters
    assert "relation" not in params
    assert "caller_relation" not in params
    assert all(param.kind is inspect.Parameter.KEYWORD_ONLY for param in params.values())


@pytest.mark.parametrize(
    "model_id, remaining",
    [("", Decimal("1")), ("model", 0), ("model", Decimal("0")), ("model", -1), ("model", 0.0)],
)
def test_empty_model_or_spent_budget_refuses_before_complete(model_id: str, remaining: object) -> None:
    def complete(prompt: str, model: str) -> Completion:
        raise AssertionError("complete called")

    def settle(completion: Completion) -> str:
        raise AssertionError("settle called")

    code = MODEL_GRANT_REQUIRED if model_id == "" else BUDGET_EXCEEDED
    with pytest.raises(IntakeJudgeRefusal, match=code) as raised:
        _link(complete, settle, model_id=model_id, remaining_usd=remaining)  # type: ignore[arg-type]
    assert raised.value.code == code


def test_settle_runs_once_when_the_reply_is_unparsable() -> None:
    complete = _complete("not json at all", 4, 1)
    settle = _settle()
    with pytest.raises(IntakeJudgeRefusal, match=JUDGE_UNPARSABLE):
        _link(complete, settle)
    assert len(settle.seen) == 1
    assert settle.seen[0].text == "not json at all"
    assert len(complete.prompts) == 1


def test_mixed_counts_refuse_after_settle() -> None:
    settle = _settle()
    with pytest.raises(IntakeJudgeRefusal, match=MIXED_TOKEN_COUNTS):
        _link(_complete('{"verdict": "supports"}', 3, None), settle)
    assert len(settle.seen) == 1


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "supports"}',
        '```json\n{"verdict": "contradicts"}\n```',
        '```\n{"verdict": "neither"}\n```',
        '{"verdict": "abstain"}',
        '{"verdict": "timeout"}',
        '{"verdict": "evidence-unavailable"}',
    ],
)
def test_verdict_json_allows_an_optional_fence(text: str) -> None:
    receipt = _link(_complete(text, 2, 1), _settle())
    assert receipt.verdict in text
    assert receipt.measurement == "measured"
    assert (receipt.input_tokens, receipt.output_tokens, receipt.usage_id) == (2, 1, "usage-1")


def test_unknown_counts_stay_null() -> None:
    receipt = _link(_complete('{"verdict": "neither"}', None, None), lambda _completion: None)
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None and receipt.output_tokens is None
    assert receipt.usage_id is None


def test_measured_zero_is_not_unknown() -> None:
    receipt = _link(_complete('{"verdict": "supports"}', 0, 0), lambda _completion: "usage-0")
    assert receipt.measurement == "measured"
    assert (receipt.input_tokens, receipt.output_tokens) == (0, 0)


def test_rendered_prompt_carries_the_three_inputs_and_no_relation() -> None:
    complete = _complete('{"verdict": "supports"}', 1, 1)
    _link(complete, _settle())
    prompt = complete.prompts[0]
    assert CLAIM in prompt and PASSAGE in prompt and CONTEXT in prompt
    assert "relation" not in prompt.lower()
    assert complete.models == [MODEL]


def test_tampered_receipt_inputs_fail_verify() -> None:
    receipt = _link(_complete('{"verdict": "supports"}', 8, 2), _settle())
    sealed = verify(INPUTS, MANIFEST)
    rules = next(item.sha256 for item in sealed.files if item.path == "rules.json")
    prompt_hash = hashlib.sha256(prompt_path().read_bytes()).hexdigest()
    assert receipt.claim_text_sha256 == text_sha256(CLAIM)
    assert receipt.passage_sha256 == text_sha256(PASSAGE)
    assert receipt.context_sha256 == text_sha256(CONTEXT)
    assert receipt.prompt_sha256 == prompt_hash
    assert receipt.prompt_version == prompt_hash
    assert receipt.rule_version == rules
    assert receipt.model_id == MODEL
    assert receipt.model_version == MODEL
    assert (receipt.source_span.start, receipt.source_span.end) == (0, len(PASSAGE.encode()))
    assert receipt.source_revision == text_sha256(CONTEXT)
    kwargs = {
        "claim_text": CLAIM,
        "quote": PASSAGE,
        "model_id": MODEL,
        "model_version": receipt.model_version,
        "prompt_version": receipt.prompt_version,
        "rule_version": receipt.rule_version,
        "source_span": receipt.source_span,
        "source_revision": receipt.source_revision,
    }
    assert verify_receipt(receipt, **kwargs)
    tampered = [
        {"claim_text": CLAIM + " extra"},
        {"quote": PASSAGE + " extra"},
        {"model_id": "other-model"},
        {"model_version": receipt.model_version + "-other"},
        {"prompt_version": "p-other"},
        {"rule_version": "r-other"},
        {"source_span": {"start": 1, "end": receipt.source_span.end}},
        {"source_revision": receipt.source_revision + "x"},
    ]
    for over in tampered:
        assert verify_receipt(receipt, **{**kwargs, **over}) is False


def test_unset_url_refuses_before_mock_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMP_INTAKE_JUDGE_URL", raising=False)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("httpx.Client opened")

    monkeypatch.setattr(httpx, "Client", explode)
    transport = httpx.MockTransport(lambda _request: (_ for _ in ()).throw(AssertionError("socket")))
    with pytest.raises(IntakeJudgeRefusal, match=MODEL_GRANT_REQUIRED) as raised:
        complete_from_env("prompt", MODEL, transport=transport)
    assert raised.value.code == MODEL_GRANT_REQUIRED


def test_blank_url_refuses_before_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "  ")
    transport = httpx.MockTransport(lambda _request: (_ for _ in ()).throw(AssertionError("socket")))
    with pytest.raises(IntakeJudgeRefusal, match=MODEL_GRANT_REQUIRED):
        complete_from_env("prompt", MODEL, transport=transport)


def test_complete_from_env_posts_prompt_and_reads_optional_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "http://judge.test/complete")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["body"] = body
        seen["url"] = str(request.url)
        if body["prompt"] == "PROMPT":
            return httpx.Response(
                200,
                json={"text": '{"verdict": "supports"}', "input_tokens": 5, "output_tokens": 1},
            )
        return httpx.Response(200, json={"text": '{"verdict": "abstain"}'})

    transport = httpx.MockTransport(handler)
    measured = complete_from_env("PROMPT", MODEL, transport=transport)
    assert seen["url"] == "http://judge.test/complete"
    assert seen["body"] == {"prompt": "PROMPT", "model": MODEL}
    assert (measured.text, measured.input_tokens, measured.output_tokens) == (
        '{"verdict": "supports"}', 5, 1,
    )
    unknown = complete_from_env("other", "other-model", transport=transport)
    assert unknown.input_tokens is None and unknown.output_tokens is None
    assert seen["body"] == {"prompt": "other", "model": "other-model"}


def test_env_completion_can_settle_a_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "http://judge.test/complete")
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"text": '{"verdict": "neither"}', "input_tokens": 6, "output_tokens": 1},
        )

    settle = _settle()
    receipt = _link(
        lambda prompt, model_id: complete_from_env(
            prompt, model_id, transport=httpx.MockTransport(handler),
        ),
        settle,
    )
    assert receipt.verdict == "neither"
    assert receipt.measurement == "measured"
    assert receipt.claim_text_sha256 == text_sha256(CLAIM)
    assert len(settle.seen) == 1
    assert sent and CLAIM in str(sent[0]["prompt"]) and "relation" not in str(sent[0]["prompt"]).lower()


def test_echo_receipt_copies_the_caller_and_records_unknown_usage() -> None:
    receipt = echo_receipt(
        claim_text=CLAIM, passage=PASSAGE, context=CONTEXT, caller_relation="contradicts",
    )
    assert receipt.verdict == "contradicts"
    assert receipt.model_id == ECHO_MODEL_ID
    assert receipt.model_version == ECHO_MODEL_ID
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None and receipt.output_tokens is None and receipt.usage_id is None
    assert verify_receipt(
        receipt,
        CLAIM,
        PASSAGE,
        ECHO_MODEL_ID,
        receipt.model_version,
        receipt.prompt_version,
        receipt.rule_version,
        receipt.source_span,
        receipt.source_revision,
    )
    assert verify_receipt(
        receipt,
        CLAIM + "x",
        PASSAGE,
        ECHO_MODEL_ID,
        receipt.model_version,
        receipt.prompt_version,
        receipt.rule_version,
        receipt.source_span,
        receipt.source_revision,
    ) is False


def test_src_does_not_install_a_hook_and_nested_hooks_restore() -> None:
    source = Path(judge_link.__code__.co_filename).read_text(encoding="utf-8")
    assert "ContextVar" not in source
    assert current_hook() is None
    outer = JudgeHook(echo_caller=True)
    inner = JudgeHook(echo_caller=False)
    with use_judge_hook(outer):
        assert current_hook() is outer
        with pytest.raises(RuntimeError, match="boom"):
            with use_judge_hook(inner):
                assert current_hook() is inner
                raise RuntimeError("boom")
        assert current_hook() is outer
    assert current_hook() is None


def test_judge_link_does_not_dispatch_through_the_hook() -> None:
    def hook_complete(_prompt: str, _model_id: str) -> Completion:
        raise AssertionError("hook complete")

    with use_judge_hook(JudgeHook(complete=hook_complete, echo_caller=True)):
        receipt = _link(_complete('{"verdict": "neither"}', None, None), lambda _completion: None)
    assert receipt.verdict == "neither"
    assert receipt.model_id == MODEL
    assert current_hook() is None
