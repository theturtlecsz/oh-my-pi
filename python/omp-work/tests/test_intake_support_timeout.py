"""Actual transport timeouts become bound receipts, never measured support."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from omp_work import intake_support_judge as judge
from omp_work.frozen_corpus import verify
from omp_work.v1.canonical import text_sha256

CLAIM = "The bridge carries six lanes."
PASSAGE = "東京 bridge carries two lanes."
CONTEXT = "Published bridge inspection."
MODEL = "deepseek-v4.1-flash:cloud"
CORPUS = Path(__file__).resolve().parents[3] / "evals" / "intake_support" / "v1"


def test_env_completion_preserves_timeout_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "https://judge.invalid/complete")
    timeout = httpx.ReadTimeout("response timed out")

    def handler(request: httpx.Request) -> httpx.Response:
        raise timeout

    with pytest.raises(httpx.ReadTimeout) as raised:
        judge.complete_from_env("claim passage context", MODEL, transport=httpx.MockTransport(handler))
    assert raised.value is timeout


def test_read_timeout_becomes_bound_receipt_with_one_unknown_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "https://judge.invalid/complete")
    requests: list[httpx.Request] = []
    settled: list[judge.Completion] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("response timed out", request=request)

    def complete(prompt: str, model_id: str) -> judge.Completion:
        return judge.complete_from_env(prompt, model_id, transport=httpx.MockTransport(handler))

    def settle(completion: judge.Completion) -> str:
        settled.append(completion)
        return "usage-timeout"

    receipt = judge.judge_link(
        claim_text=CLAIM, passage=PASSAGE, context=CONTEXT,
        model_id=MODEL, remaining_usd=1, complete=complete, settle=settle,
    )

    assert len(requests) == 1
    assert receipt.verdict == "timeout"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None and receipt.output_tokens is None
    assert receipt.usage_id == "usage-timeout"
    assert len(settled) == 1
    assert settled[0].input_tokens is None and settled[0].output_tokens is None
    assert receipt.claim_text_sha256 == text_sha256(CLAIM)
    assert receipt.passage_sha256 == text_sha256(PASSAGE)
    assert receipt.context_sha256 == receipt.source_revision == text_sha256(CONTEXT)
    assert (receipt.source_span.start, receipt.source_span.end) == (0, len(PASSAGE.encode()))
    assert receipt.model_id == receipt.model_version == MODEL
    prompt_hash = hashlib.sha256(judge.prompt_path().read_bytes()).hexdigest()
    assert receipt.prompt_version == receipt.prompt_sha256 == prompt_hash
    sealed = verify(CORPUS / "inputs", CORPUS / "manifest.json")
    assert receipt.rule_version == next(item.sha256 for item in sealed.files if item.path == "rules.json")


def test_non_timeout_transport_error_still_refuses_without_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMP_INTAKE_JUDGE_URL", "https://judge.invalid/complete")
    settled: list[judge.Completion] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(judge.IntakeJudgeRefusal, match=judge.JUDGE_TRANSPORT):
        judge.judge_link(
            claim_text=CLAIM, passage=PASSAGE, context=CONTEXT,
            model_id=MODEL, remaining_usd=1,
            complete=lambda prompt, model_id: judge.complete_from_env(
                prompt, model_id, transport=httpx.MockTransport(handler),
            ),
            settle=settled.append,
        )
    assert settled == []


@pytest.mark.parametrize("reason", [judge.MODEL_GRANT_REQUIRED, judge.BUDGET_EXCEEDED, judge.PIN_MISMATCH])
def test_admission_and_pin_refuse_before_timed_out_completion(
    monkeypatch: pytest.MonkeyPatch, reason: str,
) -> None:
    if reason == judge.PIN_MISMATCH:
        monkeypatch.setattr(judge, "prompt_sha256", lambda: "changed-prompt")

    def complete(prompt: str, model_id: str) -> judge.Completion:
        raise AssertionError("completion must not run before admission and pin validation")

    def settle(completion: judge.Completion) -> str:
        raise AssertionError("unadmitted request must not settle")

    with pytest.raises(judge.IntakeJudgeRefusal, match=reason):
        judge.judge_link(
            claim_text=CLAIM, passage=PASSAGE, context=CONTEXT,
            model_id="" if reason == judge.MODEL_GRANT_REQUIRED else MODEL,
            remaining_usd=0 if reason == judge.BUDGET_EXCEEDED else 1,
            complete=complete, settle=settle,
        )
