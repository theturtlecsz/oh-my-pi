"""V2 intake support judge selection, candidate mode, and supporting spans (OMP-631-s05)."""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from omp_work import intake_support_corpus as corpus
from omp_work import intake_support_judge as judge
from omp_work.frozen_corpus import freeze, verify
from omp_work.intake_support import SupportReceipt, verify_receipt
from omp_work.v1.canonical import text_sha256

KINDS = ("topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context", "adjacent_inference")
CLAIM = "The deployment requires two approvals."
PASSAGE_WORDS = 60


def make_passage(words: int = PASSAGE_WORDS, endings: str = ".!") -> str:
    tokens = ["evidence"] * words
    tokens[words // 2 - 1] += endings[:1]
    tokens[-1] += endings[1:]
    return " ".join(tokens)


PASSAGE = make_passage()
CONTEXT = "Production deployment handbook, chapter 4."
MODEL = "deepseek-v4.1-flash:cloud"


def _complete(text: str, input_tokens: int | None = 10, output_tokens: int | None = 2):
    def complete(prompt: str, model_id: str) -> judge.Completion:
        complete.prompts.append(prompt)  # type: ignore[attr-defined]
        complete.models.append(model_id)  # type: ignore[attr-defined]
        return judge.Completion(text, input_tokens, output_tokens)

    complete.prompts = []  # type: ignore[attr-defined]
    complete.models = []  # type: ignore[attr-defined]
    return complete


def _settle():
    seen: list[judge.Completion] = []

    def settle(completion: judge.Completion) -> str:
        seen.append(completion)
        return "usage-v2-1"

    settle.seen = seen  # type: ignore[attr-defined]
    return settle


@pytest.fixture
def synthetic_v2_corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str, corpus.Pair]:
    """Create a temporary sealed v2 corpus and monkeypatch judge.corpus_dir to point to it."""
    root = tmp_path / "inputs"
    root.mkdir()
    rows = []
    for split, count in (("train", 20), ("heldout", 60)):
        for index in range(count):
            group = f"{split}-{index}"
            for positive in (False, True):
                rows.append({
                    "id": f"{group}-{positive}",
                    "group": group,
                    "split": split,
                    "claim_text": CLAIM,
                    "passage": make_passage(60),
                    "context": CONTEXT,
                    "gold": "supports" if positive else ("contradicts" if index % 2 else "neither"),
                    "rationale": "Secret internal rationale: two approvals needed." if positive else "Wrong scope.",
                    "near_miss": None if positive else KINDS[index % len(KINDS)],
                    "source_path": f"docs/{group}.md",
                    "source_revision": "a" * 40,
                    "source_start_line": 1,
                    "source_end_line": 5,
                })
    rules_data = {
        "version": 2,
        "labels": dict.fromkeys(("supports", "contradicts", "neither"), "Definition"),
        "near_miss_kinds": dict.fromkeys(KINDS, "Definition"),
        "context_rule": "Read the passage in context.",
        "rule_sha256": "r" * 64,
        "approval_sha256": "a" * 64,
    }
    (root / "pairs.json").write_text(json.dumps({"version": 2, "pairs": rows}))
    (root / "rules.json").write_text(json.dumps(rules_data))
    manifest = tmp_path / "manifest.json"
    sealed = freeze(root, corpus.files_for(2), "c" * 40, manifest)
    rules_sha256 = next(f.sha256 for f in sealed.files if f.path == "rules.json")

    monkeypatch.setattr(judge, "corpus_dir", lambda version=2: tmp_path)
    first_pair = corpus.V2Pair.model_validate(rows[0])
    return tmp_path, rules_sha256, first_pair


def test_v2_prompt_placeholders_and_properties() -> None:
    path = judge.prompt_path(version=2)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert re.findall(r"\{\{[^}]+\}\}", text) == ["{{claim}}", "{{passage}}", "{{context}}"]
    assert "relation" not in text.lower()
    assert "gold" not in text.lower()
    assert "rationale" not in text.lower()
    assert "near_miss" not in text.lower()
    assert "near-miss" not in text.lower()
    assert "direct entailment" in text.lower()
    assert "supporting span" in text.lower() or "supporting_span" in text.lower()
    assert "adjacent facts alone yield neither" in text.lower()


def test_v2_candidate_mode_valid_verbatim_supporting_span(synthetic_v2_corpus) -> None:
    tmp_path, rules_sha256, _pair = synthetic_v2_corpus
    valid_span = "evidence. evidence"
    assert valid_span in PASSAGE
    reply = json.dumps({"verdict": "supports", "supporting_span": valid_span})
    complete = _complete(reply, 15, 3)
    settle = _settle()

    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=Decimal("2.00"),
        complete=complete,
        settle=settle,
        version=2,
        candidate=True,
    )

    assert receipt.verdict == "supports"
    assert receipt.measurement == "measured"
    assert receipt.input_tokens == 15
    assert receipt.output_tokens == 3
    assert receipt.usage_id == "usage-v2-1"
    assert receipt.rule_version == rules_sha256
    prompt_hash = hashlib.sha256(judge.prompt_path(version=2).read_bytes()).hexdigest()
    assert receipt.prompt_version == prompt_hash
    assert receipt.prompt_sha256 == prompt_hash
    assert receipt.claim_text_sha256 == text_sha256(CLAIM)
    assert receipt.passage_sha256 == text_sha256(PASSAGE)
    assert receipt.context_sha256 == text_sha256(CONTEXT)
    assert receipt.source_revision == text_sha256(CONTEXT)
    assert receipt.source_span.start == 0
    assert receipt.source_span.end == len(PASSAGE.encode("utf-8"))

    assert verify_receipt(
        receipt,
        claim_text=CLAIM,
        quote=PASSAGE,
        model_id=MODEL,
        model_version=MODEL,
        prompt_version=prompt_hash,
        rule_version=rules_sha256,
        source_span=receipt.source_span,
        source_revision=receipt.source_revision,
    )


def test_v2_candidate_mode_missing_supporting_span_becomes_neither(synthetic_v2_corpus) -> None:
    _tmp_path, rules_sha256, _pair = synthetic_v2_corpus
    # Model claims supports but omits supporting_span key
    reply = json.dumps({"verdict": "supports"})
    complete = _complete(reply, 12, 2)
    settle = _settle()

    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=Decimal("2.00"),
        complete=complete,
        settle=settle,
        version=2,
        candidate=True,
    )

    assert receipt.verdict == "neither"
    assert receipt.measurement == "measured"
    assert (receipt.input_tokens, receipt.output_tokens) == (12, 2)
    assert len(settle.seen) == 1
    assert receipt.rule_version == rules_sha256


def test_v2_candidate_mode_non_verbatim_supporting_span_becomes_neither(synthetic_v2_corpus) -> None:
    _tmp_path, _rules_sha256, _pair = synthetic_v2_corpus
    # Model claims supports but supporting_span is not a substring of passage
    non_verbatim = "this text does not appear in the evidence passage at all"
    assert non_verbatim not in PASSAGE
    reply = json.dumps({"verdict": "supports", "supporting_span": non_verbatim})
    complete = _complete(reply, 14, 4)
    settle = _settle()

    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=Decimal("2.00"),
        complete=complete,
        settle=settle,
        version=2,
        candidate=True,
    )

    assert receipt.verdict == "neither"
    assert receipt.measurement == "measured"
    assert len(settle.seen) == 1


@pytest.mark.parametrize("bad_span", ["", None, 123, []])
def test_v2_candidate_mode_empty_or_non_string_span_becomes_neither(
    synthetic_v2_corpus, bad_span: object,
) -> None:
    _tmp_path, _rules_sha256, _pair = synthetic_v2_corpus
    reply = json.dumps({"verdict": "supports", "supporting_span": bad_span})
    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=Decimal("1.00"),
        complete=_complete(reply),
        settle=_settle(),
        version=2,
        candidate=True,
    )
    assert receipt.verdict == "neither"


@pytest.mark.parametrize(
    "text,expected_verdict",
    [
        ('{"verdict": "contradicts"}', "contradicts"),
        ('{"verdict": "neither"}', "neither"),
        ('{"verdict": "abstain"}', "abstain"),
        ('{"verdict": "evidence-unavailable"}', "evidence-unavailable"),
        ('```json\n{"verdict": "contradicts"}\n```', "contradicts"),
        ('{"verdict": "neither", "supporting_span": null}', "neither"),
    ],
)
def test_v2_candidate_mode_other_verdicts_work_without_span(
    synthetic_v2_corpus, text: str, expected_verdict: str,
) -> None:
    _tmp_path, rules_sha256, _pair = synthetic_v2_corpus
    complete = _complete(text)
    settle = _settle()
    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=1,
        complete=complete,
        settle=settle,
        version=2,
        candidate=True,
    )
    assert receipt.verdict == expected_verdict
    assert receipt.rule_version == rules_sha256


@pytest.mark.parametrize(
    "malformed_text",
    [
        "not json at all",
        '{"verdict": "unsupported_verdict_name"}',
        '{"verdict": "supports", "supporting_span": "evidence", "extra_key": "unwanted"}',
        '{"verdict": "neither", "extra_key": 1}',
        '{"verdict": 100}',
        '{"supporting_span": "evidence"}',
        '{"verdict": "neither", "supporting_span": 999}',
        '["supports"]',
    ],
)
def test_v2_candidate_mode_malformed_verdicts_refuse_after_settle(
    synthetic_v2_corpus, malformed_text: str,
) -> None:
    _tmp_path, _rules_sha256, _pair = synthetic_v2_corpus
    settle = _settle()
    with pytest.raises(judge.IntakeJudgeRefusal) as exc:
        judge.judge_link(
            claim_text=CLAIM,
            passage=PASSAGE,
            context=CONTEXT,
            model_id=MODEL,
            remaining_usd=1,
            complete=_complete(malformed_text),
            settle=settle,
            version=2,
            candidate=True,
        )
    assert exc.value.code == judge.JUDGE_UNPARSABLE
    assert len(settle.seen) == 1
    assert settle.seen[0].text == malformed_text


def test_v2_rendered_inputs_exclude_gold_rationale_and_near_miss(synthetic_v2_corpus) -> None:
    _tmp_path, _rules_sha256, pair = synthetic_v2_corpus
    # The pair has gold label, rationale, and near_miss
    assert pair.rationale and pair.near_miss
    complete = _complete(json.dumps({"verdict": "neither"}))
    settle = _settle()

    judge.judge_link(
        claim_text=pair.claim_text,
        passage=pair.passage,
        context=pair.context,
        model_id=MODEL,
        remaining_usd=1,
        complete=complete,
        settle=settle,
        version=2,
        candidate=True,
    )

    assert len(complete.prompts) == 1
    prompt = complete.prompts[0]
    assert pair.claim_text in prompt
    assert pair.passage in prompt
    assert pair.context in prompt

    # Gold, rationale, and near-miss tags must NEVER be sent to the model
    assert pair.rationale not in prompt
    assert pair.near_miss not in prompt
    assert "gold" not in prompt.lower()
    assert "rationale" not in prompt.lower()
    assert "near_miss" not in prompt.lower()
    assert "near-miss" not in prompt.lower()
    assert "relation" not in prompt.lower()


def test_v2_candidate_mode_timeout_handling(synthetic_v2_corpus) -> None:
    _tmp_path, rules_sha256, _pair = synthetic_v2_corpus

    def complete_timeout(prompt: str, model_id: str) -> judge.Completion:
        raise httpx.ReadTimeout("inference timed out")

    settle = _settle()
    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=1,
        complete=complete_timeout,
        settle=settle,
        version=2,
        candidate=True,
    )

    assert receipt.verdict == "timeout"
    assert receipt.measurement == "unknown"
    assert receipt.input_tokens is None and receipt.output_tokens is None
    assert receipt.usage_id == "usage-v2-1"
    assert len(settle.seen) == 1
    assert json.loads(settle.seen[0].text) == {"verdict": "timeout"}
    assert receipt.claim_text_sha256 == text_sha256(CLAIM)
    assert receipt.passage_sha256 == text_sha256(PASSAGE)
    assert receipt.context_sha256 == receipt.source_revision == text_sha256(CONTEXT)
    assert receipt.rule_version == rules_sha256
    prompt_hash = hashlib.sha256(judge.prompt_path(version=2).read_bytes()).hexdigest()
    assert receipt.prompt_version == receipt.prompt_sha256 == prompt_hash


def test_v2_receipt_rule_binding_fails_if_tampered(synthetic_v2_corpus) -> None:
    _tmp_path, rules_sha256, _pair = synthetic_v2_corpus
    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=1,
        complete=_complete(json.dumps({"verdict": "neither"})),
        settle=_settle(),
        version=2,
        candidate=True,
    )
    assert receipt.rule_version == rules_sha256
    prompt_hash = hashlib.sha256(judge.prompt_path(version=2).read_bytes()).hexdigest()
    kwargs = {
        "claim_text": CLAIM,
        "quote": PASSAGE,
        "model_id": MODEL,
        "model_version": MODEL,
        "prompt_version": prompt_hash,
        "rule_version": rules_sha256,
        "source_span": receipt.source_span,
        "source_revision": receipt.source_revision,
    }
    assert verify_receipt(receipt, **kwargs)
    assert verify_receipt(receipt, **{**kwargs, "rule_version": "0" * 64}) is False


def test_v2_ordinary_mode_requires_matching_pin_and_refuses_mismatch_before_complete(
    synthetic_v2_corpus,
) -> None:
    tmp_path, rules_sha256, _pair = synthetic_v2_corpus
    completed = []

    def complete(prompt: str, model_id: str) -> judge.Completion:
        completed.append(prompt)
        return judge.Completion('{"verdict": "neither"}', 1, 1)

    settle = _settle()

    # 1. No tuning-pin.json present: refuses with PIN_MISMATCH before complete
    with pytest.raises(judge.IntakeJudgeRefusal) as exc:
        judge.judge_link(
            claim_text=CLAIM,
            passage=PASSAGE,
            context=CONTEXT,
            model_id=MODEL,
            remaining_usd=1,
            complete=complete,
            settle=settle,
            version=2,
            candidate=False,
        )
    assert exc.value.code == judge.PIN_MISMATCH
    assert completed == []

    # 2. Mismatching tuning-pin.json: refuses before complete
    sealed = verify(tmp_path / "inputs", tmp_path / "manifest.json")
    prompt_hash = hashlib.sha256(judge.prompt_path(version=2).read_bytes()).hexdigest()
    bad_pin = {
        "tree_sha256": sealed.tree_sha256,
        "manifest_sha256": sealed.manifest_sha256,
        "prompt_sha256": "0" * 64,  # Mismatch
    }
    pin_file = tmp_path / "tuning-pin.json"
    pin_file.write_text(json.dumps(bad_pin))

    with pytest.raises(judge.IntakeJudgeRefusal) as exc:
        judge.judge_link(
            claim_text=CLAIM,
            passage=PASSAGE,
            context=CONTEXT,
            model_id=MODEL,
            remaining_usd=1,
            complete=complete,
            settle=settle,
            version=2,
            candidate=False,
        )
    assert exc.value.code == judge.PIN_MISMATCH
    assert completed == []

    # 3. Matching tuning-pin.json: succeeds!
    good_pin = {
        "tree_sha256": sealed.tree_sha256,
        "manifest_sha256": sealed.manifest_sha256,
        "prompt_sha256": prompt_hash,
    }
    pin_file.write_text(json.dumps(good_pin))

    receipt = judge.judge_link(
        claim_text=CLAIM,
        passage=PASSAGE,
        context=CONTEXT,
        model_id=MODEL,
        remaining_usd=1,
        complete=complete,
        settle=settle,
        version=2,
        candidate=False,
    )
    assert receipt.verdict == "neither"
    assert len(completed) == 1
    assert receipt.rule_version == rules_sha256


def test_candidate_mode_with_v1_refuses(synthetic_v2_corpus) -> None:
    _tmp_path, _rules_sha256, _pair = synthetic_v2_corpus
    completed = []

    def complete(prompt: str, model_id: str) -> judge.Completion:
        completed.append(prompt)
        return judge.Completion('{"verdict": "neither"}', 1, 1)

    with pytest.raises(judge.IntakeJudgeRefusal) as exc:
        judge.judge_link(
            claim_text=CLAIM,
            passage=PASSAGE,
            context=CONTEXT,
            model_id=MODEL,
            remaining_usd=1,
            complete=complete,
            settle=_settle(),
            version=1,
            candidate=True,
        )
    assert exc.value.code == judge.PIN_MISMATCH
    assert completed == []


def test_v2_candidate_mode_drifted_inputs_refuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "pairs.json").write_text(json.dumps({"version": 2, "pairs": []}))
    (root / "rules.json").write_text(json.dumps({"version": 2}))
    manifest = tmp_path / "manifest.json"
    freeze(root, corpus.files_for(2), "d" * 40, manifest)

    # Tamper with file after freeze
    (root / "pairs.json").write_text(json.dumps({"version": 2, "pairs": [{"tampered": True}]}))

    monkeypatch.setattr(judge, "corpus_dir", lambda version=2: tmp_path)

    with pytest.raises(judge.IntakeJudgeRefusal) as exc:
        judge.judge_link(
            claim_text=CLAIM,
            passage=PASSAGE,
            context=CONTEXT,
            model_id=MODEL,
            remaining_usd=1,
            complete=_complete('{"verdict": "neither"}'),
            settle=_settle(),
            version=2,
            candidate=True,
        )
    assert exc.value.code == judge.PIN_MISMATCH
