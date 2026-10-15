"""Extraction of the v2 labelling-rule JSON block, and malformed-input boundaries."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from omp_work.intake_support_rules import load, rule_block, seal, verify

DOC = Path(__file__).resolve().parents[3] / "docs" / "intake-support-v2-labelling-rule.md"
KINDS = {
    "topic_wrong_claim",
    "negation",
    "scope_mismatch",
    "quote_out_of_context",
    "adjacent_inference",
}


def _rules() -> dict:
    return {
        "version": 2,
        "labels": {
            "supports": "direct entailment",
            "contradicts": "the opposite",
            "neither": "does not establish the claim or its opposite",
        },
        "near_miss_kinds": {kind: kind for kind in sorted(KINDS)},
        "context_rule": "Context defeats quotes.",
        "rule_sha256": None,
        "approval_sha256": None,
    }


def _write_doc(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _fenced(path: Path, body: str, *, prologue: str = "Preamble.\n\n") -> Path:
    return _write_doc(path, f"{prologue}```json\n{body}\n```\n")


def _approval(directory: Path, document: Path, *, extra: bytes = b"") -> Path:
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    payload = (
        '{"approval_sha256":null,"rule_sha256":"' + digest + '","version":2}'
    ).encode() + extra
    target = directory / f"approval-{len(extra)}.json"
    target.write_bytes(payload)
    return target


def test_committed_rule_block_is_the_v2_object() -> None:
    found = rule_block(DOC)
    loaded = load(DOC)
    assert set(found) == {
        "version",
        "labels",
        "near_miss_kinds",
        "context_rule",
        "rule_sha256",
        "approval_sha256",
    }
    assert found["version"] == loaded.version == 2
    assert set(found["labels"]) == {"supports", "contradicts", "neither"}
    assert set(found["near_miss_kinds"]) == KINDS
    assert isinstance(found["context_rule"], str) and found["context_rule"]
    assert found["rule_sha256"] is loaded.rule_sha256 is None
    assert found["approval_sha256"] is loaded.approval_sha256 is None
    for text in (*found["labels"].values(), *found["near_miss_kinds"].values()):
        assert isinstance(text, str) and text


def test_rule_block_returns_the_object(tmp_path: Path) -> None:
    path = _fenced(tmp_path / "rule.md", ' \n{"kept": true}\n')
    assert rule_block(path) == {"kept": True}


def test_other_fences_are_not_json_blocks(tmp_path: Path) -> None:
    text = '```\n{"version": 2}\n```\n\n```text\n{"version": 2}\n```\n\n```JSON\n{"version": 2}\n```\n'
    with pytest.raises(ValueError, match="no JSON block"):
        rule_block(_write_doc(tmp_path / "rule.md", text))


def test_json_fence_among_other_fences(tmp_path: Path) -> None:
    text = "```text\nignore\n```\n\n~~~json\n{\"kept\": 1}\n~~~\n"
    assert rule_block(_write_doc(tmp_path / "rule.md", text)) == {"kept": 1}


def test_absent_json_block(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no JSON block"):
        rule_block(_write_doc(tmp_path / "rule.md", "Prose only.\n"))


def test_multiple_json_blocks(tmp_path: Path) -> None:
    text = "```json\n{\"a\": 1}\n```\n\n```json\n[\n```\n"
    with pytest.raises(ValueError, match="multiple JSON blocks"):
        rule_block(_write_doc(tmp_path / "rule.md", text))


def test_unclosed_fence(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unclosed code fence"):
        rule_block(_write_doc(tmp_path / "rule.md", "```json\n{}\n"))


@pytest.mark.parametrize("body", ["", "{", '{"a":', '{"a": 1,}', "[1, 2"])
def test_malformed_json(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="malformed JSON"):
        rule_block(_fenced(tmp_path / "rule.md", body))


@pytest.mark.parametrize("body", ["[]", "null", "1", "1.5", '"text"', "true", "false"])
def test_non_object_json(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError, match="JSON block is not an object"):
        rule_block(_fenced(tmp_path / "rule.md", body))


def test_load_and_seal_call_rule_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omp_work.intake_support_rules as rules

    seen: list[Path] = []
    original = rules.rule_block

    def spy(path: str | Path) -> dict:
        seen.append(Path(path))
        return original(path)

    monkeypatch.setattr(rules, "rule_block", spy)
    doc = _fenced(tmp_path / "rule.md", json.dumps(_rules()))
    approval = _approval(tmp_path, doc)
    rules.load(doc)
    rules.seal(doc, approval, tmp_path / "sealed.json")
    assert seen == [doc, doc]


def test_load_rejects_rules_that_are_not_v2(tmp_path: Path) -> None:
    cases = [
        {**_rules(), "rule_sha256": "ab" * 32},
        {**_rules(), "approval_sha256": "cd" * 32},
        {key: value for key, value in _rules().items() if key != "context_rule"},
        {**_rules(), "extra": True},
        {**_rules(), "version": 1},
    ]
    kinds = _rules()
    del kinds["near_miss_kinds"]["adjacent_inference"]
    cases.append(kinds)
    for obj in cases:
        with pytest.raises(ValidationError):
            load(_fenced(tmp_path / "rule.md", json.dumps(obj)))


def test_seal_rejects_a_document_rule_block_rejects(tmp_path: Path) -> None:
    doc = _write_doc(tmp_path / "rule.md", "no block\n")
    with pytest.raises(ValueError, match="no JSON block"):
        seal(doc, tmp_path / "missing.json", tmp_path / "sealed.json")
    assert not (tmp_path / "sealed.json").exists()


def test_seal_hashes_exact_document_and_approval_bytes(tmp_path: Path) -> None:
    doc = _fenced(tmp_path / "rule.md", json.dumps(_rules()))
    approval = _approval(tmp_path, doc)
    sealed = seal(doc, approval, tmp_path / "sealed.json")
    assert sealed.rule_sha256 == hashlib.sha256(doc.read_bytes()).hexdigest()
    assert sealed.approval_sha256 == hashlib.sha256(approval.read_bytes()).hexdigest()
    assert verify(doc, approval, tmp_path / "sealed.json") == sealed

    spaced = _approval(tmp_path, doc, extra=b"\n")
    resealed = seal(doc, spaced, tmp_path / "sealed-spaced.json")
    assert resealed.rule_sha256 == sealed.rule_sha256
    assert resealed.approval_sha256 != sealed.approval_sha256
    with pytest.raises(ValueError, match="approval digest drift"):
        verify(doc, spaced, tmp_path / "sealed.json")


def test_seal_rejects_an_approval_whose_rule_digest_does_not_match(tmp_path: Path) -> None:
    doc = _fenced(tmp_path / "rule.md", json.dumps(_rules()))
    approval = _approval(tmp_path, doc)
    edited = _fenced(tmp_path / "edited.md", json.dumps(_rules()), prologue="Edited preamble.\n\n")
    with pytest.raises(ValueError, match="exact document bytes"):
        seal(edited, approval, tmp_path / "sealed.json")
    assert not (tmp_path / "sealed.json").exists()


def test_committed_document_seals_from_its_exact_bytes(tmp_path: Path) -> None:
    approval = _approval(tmp_path, DOC)
    sealed = seal(DOC, approval, tmp_path / "sealed.json")
    assert sealed.rule_sha256 == hashlib.sha256(DOC.read_bytes()).hexdigest()
    assert sealed.approval_sha256 == hashlib.sha256(approval.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"{", "approval JSON is malformed"),
        (b"[]", "approval JSON must be an object"),
        (b'{"version":2,"rule_sha256":null,"approval_sha256":null}', "validation"),
    ],
)
def test_seal_rejects_a_bad_approval(tmp_path: Path, payload: bytes, message: str) -> None:
    doc = _fenced(tmp_path / "rule.md", json.dumps(_rules()))
    approval = tmp_path / "approval.json"
    approval.write_bytes(payload)
    expect = ValidationError if message == "validation" else ValueError
    match = None if message == "validation" else message
    with pytest.raises(expect, match=match):
        seal(doc, approval, tmp_path / "sealed.json")
