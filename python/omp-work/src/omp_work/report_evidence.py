"""R2 report-pipeline v1: the claim-support evidence matrix.

Extract the material claims from a drafted report, link each to candidate
passages from the sources it cites, classify every (claim, passage) pair as
``supports`` / ``contradicts`` / ``neither``, and build a replayable matrix the
report and its auditor consume.

Semantic support is a separate step from source-existence metadata checks and
never replaces them. A classifier is handed claim and passage *text* only; a
source's metadata, DOI, author or venue never reaches the prompt, so a
resolved citation can never be mistaken for the passage arguing for the claim.

Classifier probabilities are routing hints for triage, never a confidence
interval ("an LLM confidence score is not a confidence interval", plan §9.3):
they are stored on the matrix but never rendered into a report body.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from omp_work.research_ledger import append_ledger_event
from omp_work.v1.canonical import sha256

MATRIX_ALGORITHM = "omp.report-evidence/v1"
DEFAULT_TOP_K = 3
DEFAULT_BATCH_SIZE = 8
DEFAULT_SUPPORT_THRESHOLD = 0.6

Label = Literal["supports", "contradicts", "neither"]
ClaimStatus = Literal["supported", "contested", "unsupported"]
LABELS: tuple[Label, ...] = ("supports", "contradicts", "neither")

MATERIALITY_RULE = (
    "A sentence is a material claim when it asserts a fact, figure, or causal "
    "statement about the world or the system: it carries a numeric value, a "
    "causal connective, or a declarative assertion verb. Headings, questions, "
    "process markers, empty list items, and purely hedged speculation are "
    "excluded, and every excluded sentence records its reason."
)

STATUS_RULE = (
    "A material claim is supported when at least one passage supports it and "
    "no passage contradicts it, contested when at least one passage "
    "contradicts it, and unsupported when no passage bears on it."
)


class EvidenceMatrixError(ValueError):
    """Refusal raised when a matrix would misrepresent its evidence."""


class ClassifierAnswerError(EvidenceMatrixError):
    """Refusal raised when a classifier answer is not the strict typed shape."""


@dataclass(frozen=True)
class UsageEntry:
    """One classifier call, for usage accounting."""

    input_tokens: int
    output_tokens: int
    model: str
    pair_count: int


@dataclass(frozen=True)
class Claim:
    id: str
    text: str
    char_start: int
    char_end: int
    material: bool
    excluded_reason: str | None = None


@dataclass(frozen=True)
class Passage:
    passage_id: str
    source_id: str
    locator: str
    text: str
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceDoc:
    source_id: str
    passages: tuple[Passage, ...]


@dataclass(frozen=True)
class Classification:
    claim_id: str
    passage_id: str
    label: Label
    probabilities: Mapping[str, float] | None = None


@dataclass
class EvidenceMatrix:
    report_id: str
    claims: tuple[Claim, ...]
    passages: Mapping[str, Passage]
    classifications: tuple[Classification, ...]
    statuses: Mapping[str, ClaimStatus]

    def material_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.material]

    def supporting_passages(self, claim_id: str) -> list[Passage]:
        return [
            self.passages[c.passage_id]
            for c in self.classifications
            if c.claim_id == claim_id and c.label == "supports"
        ]

    def contradicting_passages(self, claim_id: str) -> list[Passage]:
        return [
            self.passages[c.passage_id]
            for c in self.classifications
            if c.claim_id == claim_id and c.label == "contradicts"
        ]

    def claims_with_status(self, status: ClaimStatus) -> list[Claim]:
        return [c for c in self.material_claims() if self.statuses.get(c.id) == status]

    def to_dict(self) -> dict[str, Any]:
        return {
            "algorithm": MATRIX_ALGORITHM,
            "report_id": self.report_id,
            "claims": [
                {
                    "id": c.id,
                    "text": c.text,
                    "char_start": c.char_start,
                    "char_end": c.char_end,
                    "material": c.material,
                    "excluded_reason": c.excluded_reason,
                }
                for c in self.claims
            ],
            "passages": [
                {
                    "passage_id": p.passage_id,
                    "source_id": p.source_id,
                    "locator": p.locator,
                    "text": p.text,
                    "metadata": dict(sorted(p.metadata.items())),
                }
                for p in sorted(self.passages.values(), key=lambda p: p.passage_id)
            ],
            "classifications": [
                {
                    "claim_id": c.claim_id,
                    "passage_id": c.passage_id,
                    "label": c.label,
                    "probabilities": (
                        dict(sorted(c.probabilities.items()))
                        if c.probabilities is not None
                        else None
                    ),
                }
                for c in self.classifications
            ],
            "statuses": dict(sorted(self.statuses.items())),
        }

    @property
    def digest(self) -> str:
        return sha256(self.to_dict())


# ── claim extraction ───────────────────────────────────────────────────────

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]*")
_NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?%?\b")
_CAUSAL_RE = re.compile(
    r"\b(?:because|causes?|caused|led to|leads to|due to|therefore|results? in"
    r"|contributes? to|as a result)\b",
    re.I,
)
_ASSERT_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|shows?|reports?|equals?|reaches?"
    r"|increases?|increase|increased|decreases?|decrease|decreased|requires?"
    r"|contains?|improves?|improved|achieves?|achieved|reduces?|reduced)\b",
    re.I,
)
_EXCLUDED_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^\s{0,3}#{1,6}\s"), "heading"),
    (re.compile(r"\?\s*$"), "question"),
    (re.compile(r"\b(?:TODO|FIXME|XXX|TBD)\b", re.I), "process_marker"),
    (
        re.compile(r"\b(?:might|could|perhaps|possibly|maybe)\b", re.I),
        "hedged_speculation",
    ),
    (re.compile(r"^\s*(?:[-*+]|\d+\.)\s*$"), "empty_list_item"),
)

_CITE_RE = re.compile(r"\[(?:src-[a-z0-9][a-z0-9._-]*)\]|\[\^[a-z0-9][a-z0-9._-]*\]")


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """Split ``text`` into ``(char_start, char_end, sentence)`` spans."""
    spans: list[tuple[int, int, str]] = []
    for match in _SENTENCE_RE.finditer(text):
        segment = match.group(0)
        if segment.strip():
            spans.append((match.start(), match.start() + len(segment), segment))
    return spans


def classify_sentence(sentence: str) -> tuple[bool, str | None]:
    """Apply :data:`MATERIALITY_RULE` to one sentence."""
    for pattern, reason in _EXCLUDED_RULES:
        if pattern.search(sentence):
            return False, reason
    if _NUMBER_RE.search(sentence) or _CAUSAL_RE.search(sentence) or _ASSERT_RE.search(sentence):
        return True, None
    return False, "no_factual_or_causal_assertion"


def extract_claims(draft: str) -> list[Claim]:
    """Extract material claims with stable ids and character locations."""
    claims: list[Claim] = []
    for index, (start, end, sentence) in enumerate(split_sentences(draft), start=1):
        material, reason = classify_sentence(sentence)
        claims.append(
            Claim(
                id=f"c-{index}",
                text=sentence,
                char_start=start,
                char_end=end,
                material=material,
                excluded_reason=reason,
            )
        )
    return claims


def cited_source_ids(claim_text: str) -> list[str]:
    """Source ids a claim cites through ``[src-id]`` / ``[^id]`` markers."""
    ids: list[str] = []
    for marker in _CITE_RE.findall(claim_text):
        inner = marker[2:-1] if marker.startswith("[^") else marker[1:-1]
        if inner not in ids:
            ids.append(inner)
    return ids


def strip_citations(text: str) -> str:
    """Claim text without citation markers, for classification only."""
    return " ".join(_CITE_RE.sub(" ", text).split())


# ── passage candidates ─────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "by", "in", "on", "of", "to", "and", "or", "for",
        "with", "at", "from", "as", "is", "are", "was", "were", "be", "been",
        "that", "this", "these", "those", "it", "its", "our", "their", "we",
        "they", "was", "not", "no", "than", "then", "into", "over", "under",
    }
)


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def lexical_overlap(claim_text: str, passage_text: str) -> float:
    """Fraction of the claim's tokens the passage restates."""
    claim_tokens = _tokens(claim_text)
    if not claim_tokens:
        return 0.0
    return len(claim_tokens & _tokens(passage_text)) / len(claim_tokens)


def _numbers(text: str) -> set[str]:
    return set(_NUMBER_RE.findall(text))


def select_passages(
    *,
    claims: Sequence[Claim],
    sources: Mapping[str, SourceDoc],
    top_k: int = DEFAULT_TOP_K,
    all_sources: bool = False,
) -> dict[str, list[Passage]]:
    """Top-k candidate passages per claim from its cited sources."""
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    selected: dict[str, list[Passage]] = {}
    for claim in claims:
        if not claim.material:
            continue
        cited = [
            source_id
            for source_id in cited_source_ids(claim.text)
            if source_id in sources
        ]
        if all_sources or not cited:
            pool = list(sources.values())
        else:
            pool = [sources[source_id] for source_id in cited]
        ranked: list[tuple[float, int, Passage]] = []
        order = 0
        for doc in pool:
            for passage in doc.passages:
                if passage.source_id != doc.source_id:
                    raise EvidenceMatrixError(
                        f"passage {passage.passage_id!r} claims source "
                        f"{passage.source_id!r} but sits under {doc.source_id!r}"
                    )
                ranked.append(
                    (
                        lexical_overlap(strip_citations(claim.text), passage.text),
                        order,
                        passage,
                    )
                )
                order += 1
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected[claim.id] = [passage for _, _, passage in ranked[:top_k]]
    return selected


# ── classifier interface ───────────────────────────────────────────────────

ClaimPassagePair = tuple[Claim, Passage]


class ClaimSupportClassifier(Protocol):
    """Classifies (claim, passage) pairs; records one usage entry per call."""

    usages: list[UsageEntry]

    def classify(self, pairs: Sequence[ClaimPassagePair]) -> list[Classification]:
        ...


def classify_pairs(
    pairs: Sequence[ClaimPassagePair], classifier: ClaimSupportClassifier
) -> list[Classification]:
    """Classify every pair, rejecting answers outside the pair set."""
    expected = {(claim.id, passage.passage_id) for claim, passage in pairs}
    results = classifier.classify(pairs)
    seen: set[tuple[str, str]] = set()
    for result in results:
        key = (result.claim_id, result.passage_id)
        if key not in expected:
            raise EvidenceMatrixError(
                f"classifier answered an unrequested pair {key[0]}/{key[1]}"
            )
        if key in seen:
            raise EvidenceMatrixError(
                f"classifier answered pair {key[0]}/{key[1]} twice"
            )
        seen.add(key)
        if result.label not in LABELS:
            raise EvidenceMatrixError(f"unknown label {result.label!r}")
    return list(results)


@dataclass
class StubClassifier:
    """Deterministic lexical stub: the first implementation of the interface.

    Same claim and passage figures at high lexical overlap -> ``supports``;
    high overlap with a claim figure the passage does not restate ->
    ``contradicts``; low overlap -> ``neither``. No model is called, so its
    usage entry records the call with zero tokens.
    """

    threshold: float = DEFAULT_SUPPORT_THRESHOLD
    model: str = "stub-lexical"
    usages: list[UsageEntry] = field(default_factory=list)

    def label_for(self, claim: Claim, passage: Passage) -> Label:
        claim_text = strip_citations(claim.text)
        overlap = lexical_overlap(claim_text, passage.text)
        if overlap < self.threshold:
            return "neither"
        claim_numbers = _numbers(claim_text)
        if claim_numbers and not claim_numbers <= _numbers(passage.text):
            return "contradicts"
        return "supports"

    def classify(self, pairs: Sequence[ClaimPassagePair]) -> list[Classification]:
        results = [
            Classification(
                claim_id=claim.id,
                passage_id=passage.passage_id,
                label=self.label_for(claim, passage),
            )
            for claim, passage in pairs
        ]
        self.usages.append(
            UsageEntry(
                input_tokens=0,
                output_tokens=0,
                model=self.model,
                pair_count=len(pairs),
            )
        )
        return results


@dataclass(frozen=True)
class Completion:
    """One chat-model completion, with optional explicit token usage."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str = "chat-model"


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _load_prompt_template() -> str:
    return (
        resources.files("omp_work")
        .joinpath("prompts/claim_support_classifier.md")
        .read_text(encoding="utf-8")
    )


def render_classifier_prompt(pairs: Sequence[ClaimPassagePair]) -> str:
    """Render the static classifier prompt. Passage metadata is never sent."""
    lines: list[str] = []
    for index, (claim, passage) in enumerate(pairs, start=1):
        lines.append(f"{index}. CLAIM: {strip_citations(claim.text)}")
        lines.append(
            f"   PASSAGE ({passage.source_id} {passage.locator}): {passage.text}"
        )
        lines.append("")
    body = "\n".join(lines).rstrip()
    return _load_prompt_template().replace("{{pairs}}", body)


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


def parse_classifier_answer(
    text: str, pairs: Sequence[ClaimPassagePair]
) -> list[Classification]:
    """Parse one strict typed answer covering every pair exactly once."""
    cleaned = _FENCE_RE.sub("", text.strip())
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ClassifierAnswerError(f"classifier answer is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("answers"), list):
        raise ClassifierAnswerError("classifier answer must be {'answers': [...]}")
    seen: set[int] = set()
    results: list[Classification] = []
    for item in payload["answers"]:
        if not isinstance(item, dict):
            raise ClassifierAnswerError("each answer must be an object")
        pair = item.get("pair")
        if not isinstance(pair, int) or isinstance(pair, bool):
            raise ClassifierAnswerError("answer 'pair' must be an integer")
        if pair < 1 or pair > len(pairs):
            raise ClassifierAnswerError(f"answer pair {pair} is out of range")
        if pair in seen:
            raise ClassifierAnswerError(f"answer pair {pair} is duplicated")
        seen.add(pair)
        label = item.get("label")
        if label not in LABELS:
            raise ClassifierAnswerError(f"answer {pair} has off-list label {label!r}")
        probabilities = item.get("probabilities")
        parsed_probs: dict[str, float] | None = None
        if probabilities is not None:
            if not isinstance(probabilities, dict):
                raise ClassifierAnswerError("probabilities must be an object")
            parsed_probs = {}
            for key, value in probabilities.items():
                if key not in LABELS:
                    raise ClassifierAnswerError(f"probability key {key!r} is off-list")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ClassifierAnswerError("probability must be a number")
                if not 0.0 <= float(value) <= 1.0:
                    raise ClassifierAnswerError("probability must be in [0, 1]")
                parsed_probs[key] = float(value)
        claim, passage = pairs[pair - 1]
        results.append(
            Classification(
                claim_id=claim.id,
                passage_id=passage.passage_id,
                label=label,
                probabilities=parsed_probs,
            )
        )
    if len(seen) != len(pairs):
        missing = sorted(set(range(1, len(pairs) + 1)) - seen)
        raise ClassifierAnswerError(f"classifier answer omitted pairs {missing}")
    return results


@dataclass
class ChatModelClassifier:
    """First classifier implementation: batched strict-typed chat answers.

    ``complete`` is injected so an implementer slice runs against a stub and
    the owner slice runs against a real model with owner credentials.
    """

    complete: Any
    batch_size: int = DEFAULT_BATCH_SIZE
    usages: list[UsageEntry] = field(default_factory=list)

    def classify(self, pairs: Sequence[ClaimPassagePair]) -> list[Classification]:
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        results: list[Classification] = []
        pair_list = list(pairs)
        for start in range(0, len(pair_list), self.batch_size):
            batch = pair_list[start : start + self.batch_size]
            prompt = render_classifier_prompt(batch)
            completion = self.complete(prompt)
            if not isinstance(completion, Completion):
                raise ClassifierAnswerError("complete() must return a Completion")
            results.extend(parse_classifier_answer(completion.text, batch))
            self.usages.append(
                UsageEntry(
                    input_tokens=(
                        completion.input_tokens
                        if completion.input_tokens is not None
                        else _estimate_tokens(prompt)
                    ),
                    output_tokens=(
                        completion.output_tokens
                        if completion.output_tokens is not None
                        else _estimate_tokens(completion.text)
                    ),
                    model=completion.model,
                    pair_count=len(batch),
                )
            )
        return results


def record_usage(
    ledger_path: Path | str,
    usages: Sequence[UsageEntry],
    *,
    conversation_id: str,
    role: str = "claim_support_classifier",
    step_start: int = 1,
) -> list[dict[str, Any]]:
    """Append classifier calls to the central usage ledger."""
    events: list[dict[str, Any]] = []
    for offset, usage in enumerate(usages):
        event = {
            "conversation_id": conversation_id,
            "step_index": step_start + offset,
            "role": role,
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "at": time.time(),
            "kind": "claim_support_classify",
            "pair_count": usage.pair_count,
        }
        append_ledger_event(ledger_path, event)
        events.append(event)
    return events


# ── matrix construction ────────────────────────────────────────────────────


def status_from_labels(labels: Sequence[Label]) -> ClaimStatus:
    """Apply :data:`STATUS_RULE`."""
    if "contradicts" in labels:
        return "contested"
    if "supports" in labels:
        return "supported"
    return "unsupported"


def build_evidence_matrix(
    *,
    report_id: str,
    draft: str,
    sources: Mapping[str, SourceDoc],
    classifier: ClaimSupportClassifier,
    top_k: int = DEFAULT_TOP_K,
    all_sources: bool = False,
) -> EvidenceMatrix:
    """Extract claims, link candidates, classify pairs, and derive statuses."""
    claims = extract_claims(draft)
    selected = select_passages(
        claims=claims, sources=sources, top_k=top_k, all_sources=all_sources
    )
    pairs: list[ClaimPassagePair] = []
    for claim in claims:
        for passage in selected.get(claim.id, []):
            pairs.append((claim, passage))
    classifications = classify_pairs(pairs, classifier)

    passages: dict[str, Passage] = {}
    for claim in claims:
        for passage in selected.get(claim.id, []):
            passages[passage.passage_id] = passage
    for result in classifications:
        if result.label == "supports":
            passage = passages.get(result.passage_id)
            if passage is None or not passage.text.strip():
                raise EvidenceMatrixError(
                    "a claim cannot be marked supported without at least one "
                    f"stored passage text (claim {result.claim_id}, passage "
                    f"{result.passage_id})"
                )

    labels_by_claim: dict[str, list[Label]] = {}
    for result in classifications:
        labels_by_claim.setdefault(result.claim_id, []).append(result.label)
    statuses = {
        claim.id: status_from_labels(labels_by_claim.get(claim.id, []))
        for claim in claims
        if claim.material
    }
    return EvidenceMatrix(
        report_id=report_id,
        claims=tuple(claims),
        passages=passages,
        classifications=tuple(classifications),
        statuses=statuses,
    )


# ── render, report integration, auditor ────────────────────────────────────


def _passage_link(passage: Passage) -> str:
    return f"[{passage.source_id} {passage.locator}](#{passage.passage_id})"


def render_matrix_table(matrix: EvidenceMatrix) -> str:
    """Render the matrix as a table with links and status flags.

    Probabilities never appear: they are routing hints, not report content.
    """
    lines = [
        "| Claim | Status | Supporting passages | Contradicting passages |",
        "| --- | --- | --- | --- |",
    ]
    for claim in matrix.material_claims():
        status = matrix.statuses.get(claim.id, "unsupported")
        flag = f"**{status}**" if status in ("contested", "unsupported") else status
        support = ", ".join(
            _passage_link(p) for p in matrix.supporting_passages(claim.id)
        )
        contradict = ", ".join(
            _passage_link(p) for p in matrix.contradicting_passages(claim.id)
        )
        lines.append(
            f"| {strip_citations(claim.text)} | {flag} | {support or '—'} | "
            f"{contradict or '—'} |"
        )
    return "\n".join(lines)


def annotate_report(draft: str, matrix: EvidenceMatrix) -> str:
    """Link each material claim inline and flag contested/unsupported ones."""
    insertions: list[tuple[int, str]] = []
    for claim in matrix.material_claims():
        if draft[claim.char_start : claim.char_end] != claim.text:
            raise EvidenceMatrixError(
                f"claim {claim.id} location does not match the draft"
            )
        status = matrix.statuses.get(claim.id, "unsupported")
        if status == "supported":
            links = ", ".join(
                _passage_link(p) for p in matrix.supporting_passages(claim.id)
            )
            marker = f" [evidence: {links}]"
        else:
            marker = f" **[{status.upper()}]**"
        insertions.append((claim.char_end, marker))
    annotated = draft
    for offset, marker in sorted(insertions, reverse=True):
        annotated = annotated[:offset] + marker + annotated[offset:]
    return annotated


def auditor_view(matrix: EvidenceMatrix) -> dict[str, Any]:
    """The matrix view the independent auditor step reads."""
    unlinked = [
        claim.id
        for claim in matrix.material_claims()
        if not any(
            result.claim_id == claim.id for result in matrix.classifications
        )
    ]
    return {
        "report_id": matrix.report_id,
        "matrix_digest": matrix.digest,
        "contested": [c.id for c in matrix.claims_with_status("contested")],
        "unsupported": [c.id for c in matrix.claims_with_status("unsupported")],
        "unlinked": unlinked,
        "semantic_support_note": (
            "Semantic support review only; source-existence metadata checks "
            "remain separate and are not replaced by this matrix."
        ),
    }


# ── persistence and replay ─────────────────────────────────────────────────


def store_matrix(path: Path | str, matrix: EvidenceMatrix) -> str:
    """Write the matrix beside the report and return its digest."""
    payload = matrix.to_dict()
    digest = sha256(payload)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps({**payload, "digest": digest}, indent=2) + "\n", encoding="utf-8"
    )
    return digest


def load_matrix(path: Path | str) -> EvidenceMatrix:
    """Replay a stored matrix, verifying its digest and derived statuses."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise EvidenceMatrixError("stored matrix must be a JSON object")
    digest = raw.pop("digest", None)
    if raw.get("algorithm") != MATRIX_ALGORITHM:
        raise EvidenceMatrixError("stored matrix has an unknown algorithm")
    if digest != sha256(raw):
        raise EvidenceMatrixError("stored matrix digest mismatch")
    claims = tuple(
        Claim(
            id=item["id"],
            text=item["text"],
            char_start=item["char_start"],
            char_end=item["char_end"],
            material=item["material"],
            excluded_reason=item["excluded_reason"],
        )
        for item in raw["claims"]
    )
    passages = {
        item["passage_id"]: Passage(
            passage_id=item["passage_id"],
            source_id=item["source_id"],
            locator=item["locator"],
            text=item["text"],
            metadata=item["metadata"],
        )
        for item in raw["passages"]
    }
    classifications = tuple(
        Classification(
            claim_id=item["claim_id"],
            passage_id=item["passage_id"],
            label=item["label"],
            probabilities=item["probabilities"],
        )
        for item in raw["classifications"]
    )
    labels_by_claim: dict[str, list[Label]] = {}
    for result in classifications:
        labels_by_claim.setdefault(result.claim_id, []).append(result.label)
    statuses = {
        claim.id: status_from_labels(labels_by_claim.get(claim.id, []))
        for claim in claims
        if claim.material
    }
    if dict(raw["statuses"]) != statuses:
        raise EvidenceMatrixError("stored matrix statuses do not replay")
    return EvidenceMatrix(
        report_id=raw["report_id"],
        claims=claims,
        passages=passages,
        classifications=classifications,
        statuses=statuses,
    )
