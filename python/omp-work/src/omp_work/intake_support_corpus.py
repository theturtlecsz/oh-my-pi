"""Hand-labelled claim/passage pairs for the intake support judge.

Judges see ``judge_input`` only; gold, rationale and near-miss tags stay behind
``training_labels``, and only for the train split.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from omp_work.frozen_corpus import FrozenCorpus, freeze, verify
from omp_work.v1.canonical import validate_execution_path
from omp_work.v1.models import StrictModel

Gold = Literal["supports", "contradicts", "neither"]
NearMiss = Literal["topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context"]
V2NearMiss = Literal[NearMiss, "adjacent_inference"]
Split = Literal["train", "heldout"]

PAIRS_FILE = "pairs.json"
RULES_FILE = "rules.json"
FILES = [("intake-support-pairs-v1", PAIRS_FILE), ("intake-support-rules-v1", RULES_FILE)]
V1_KINDS = {"topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context"}
V2_KINDS = V1_KINDS | {"adjacent_inference"}
PAIR_COUNT = 60
HELDOUT_COUNT = 30
NEAR_MISS_PER_KIND = 6
NEAR_MISS_PER_SPLIT = 12
SUPPORTS_PER_SPLIT = 18
JUDGE_FIELDS = ("id", "claim_text", "passage", "context")


def files_for(version: int) -> list[tuple[str, str]]:
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unsupported corpus version")
    return [(f"intake-support-pairs-v{version}", PAIRS_FILE),
            (f"intake-support-rules-v{version}", RULES_FILE)]


class Pair(StrictModel):
    id: str
    claim_text: str
    passage: str
    context: str
    gold: Gold
    rationale: str
    near_miss: NearMiss | None
    split: Split
    group: str

    @model_validator(mode="after")
    def near_miss_is_not_support(self) -> Pair:
        if self.near_miss is not None and self.gold == "supports":
            raise ValueError(f"{self.id}: near-miss cannot be gold supports")
        return self


class V2Pair(Pair):
    id: str = Field(min_length=1, pattern=r"\S")
    group: str = Field(min_length=1, pattern=r"\S")
    near_miss: V2NearMiss | None
    source_path: str
    source_revision: str = Field(pattern=r"^[0-9a-fA-F]{40}$")
    source_start_line: int = Field(strict=True, ge=1)
    source_end_line: int = Field(strict=True, ge=1)

    @field_validator("source_path")
    @classmethod
    def canonical_source_path(cls, value: str) -> str:
        validate_execution_path(value)
        return value

    @field_validator("passage")
    @classmethod
    def realistic_passage(cls, value: str) -> str:
        if not 60 <= len(value.split()) <= 300:
            raise ValueError("passage must contain 60-300 whitespace words")
        if len(re.findall(r"[.!?](?=\s|$)", value)) < 2:
            raise ValueError("passage must contain at least two sentence endings")
        return value

    @model_validator(mode="after")
    def source_line_range(self) -> V2Pair:
        if self.source_start_line > self.source_end_line:
            raise ValueError("source line range must be inclusive and ordered")
        return self


def _validated_rules(rules: dict, version: int, kinds: set[str]) -> None:
    if type(rules.get("version")) is not int or rules.get("version") != version:
        raise ValueError("rules version must match requested corpus version")
    if set(rules.get("labels", {})) != {"supports", "contradicts", "neither"}:
        raise ValueError("rules must define the three labels")
    if set(rules.get("near_miss_kinds", {})) != kinds or not rules.get("context_rule"):
        raise ValueError(f"rules must define the {len(kinds)} near-miss kinds and the context rule")


def _validated(pairs: list[Pair], rules: dict) -> list[Pair]:
    kinds = V1_KINDS
    _validated_rules(rules, 1, kinds)
    if len(pairs) != PAIR_COUNT or len({p.id for p in pairs}) != len(pairs):
        raise ValueError(f"corpus must hold {PAIR_COUNT} unique pairs")
    if sum(p.split == "heldout" for p in pairs) != HELDOUT_COUNT:
        raise ValueError(f"corpus must hold {HELDOUT_COUNT} held-out pairs")
    near = Counter(p.near_miss for p in pairs if p.near_miss)
    if set(near) != kinds or any(count != NEAR_MISS_PER_KIND for count in near.values()):
        raise ValueError(f"corpus must hold {NEAR_MISS_PER_KIND} near-misses per kind")
    for split in ("train", "heldout"):
        rows = [p for p in pairs if p.split == split]
        if sum(p.near_miss is not None for p in rows) != NEAR_MISS_PER_SPLIT:
            raise ValueError(f"{split} must hold {NEAR_MISS_PER_SPLIT} near-misses")
        if sum(p.gold == "supports" for p in rows) != SUPPORTS_PER_SPLIT:
            raise ValueError(f"{split} must hold {SUPPORTS_PER_SPLIT} supports")
    if any(p.gold != "supports" and p.near_miss is None for p in pairs):
        raise ValueError("every non-supports pair must be a tagged near-miss")
    splits: dict[str, set[str]] = {}
    for p in pairs:
        splits.setdefault(p.group, set()).add(p.split)
    if any(len(s) > 1 for s in splits.values()):
        raise ValueError("a group must stay in one split")
    return pairs


def _validated_v2(pairs: list[V2Pair], rules: dict) -> list[Pair]:
    _validated_rules(rules, 2, V2_KINDS)
    if len({p.id for p in pairs}) != len(pairs):
        raise ValueError("corpus must hold unique pair ids")
    groups: dict[str, list[V2Pair]] = {}
    sources: dict[str, set[str]] = {}
    for pair in pairs:
        if pair.gold != "supports" and pair.near_miss is None:
            raise ValueError("every non-supports pair must be a tagged near-miss")
        groups.setdefault(pair.group, []).append(pair)
        sources.setdefault(pair.source_path, set()).add(pair.split)
    if any(len(splits) > 1 for splits in sources.values()):
        raise ValueError("a source must stay in one split")
    for rows in groups.values():
        if len({p.split for p in rows}) != 1:
            raise ValueError("a group must stay in one split")
        if len(rows) != 2 or sum(p.gold == "supports" for p in rows) != 1:
            raise ValueError("each group must contain exactly one negative and one untagged positive")
    for split, minimum in (("train", 20), ("heldout", 60)):
        negatives = [p for p in pairs if p.split == split and p.gold != "supports"]
        if len(negatives) < minimum:
            raise ValueError(f"{split} must hold at least {minimum} negatives")
        if {p.near_miss for p in negatives} != V2_KINDS:
            raise ValueError(f"{split} must hold all five near-miss kinds")
    return list(pairs)


def load_pairs(directory: str | Path, manifest: str | Path, version: int = 1) -> list[Pair]:
    """Verify the seal, then return the validated pairs; raise ValueError on drift or malformed data."""
    root = Path(directory)
    files_for(version)
    verify(root, manifest)
    rules = json.loads((root / RULES_FILE).read_bytes())
    document = json.loads((root / PAIRS_FILE).read_bytes())
    if type(document.get("version")) is not int or document.get("version") != version:
        raise ValueError("document version must match requested corpus version")
    if version == 1:
        return _validated([Pair.model_validate(row) for row in document["pairs"]], rules)
    return _validated_v2([V2Pair.model_validate(row) for row in document["pairs"]], rules)


def seal(directory: str | Path, origin_commit: str, manifest: str | Path) -> FrozenCorpus:
    return freeze(directory, FILES, origin_commit, manifest)


def judge_input(pair: Pair) -> dict[str, str]:
    """The only view of a pair a judge may receive."""
    return {field: getattr(pair, field) for field in JUDGE_FIELDS}


def training_labels(pairs: list[Pair], split: Split) -> dict[str, Gold]:
    """Gold labels by pair id; held-out labels are never handed out."""
    if split != "train":
        raise ValueError("only train labels are available")
    return {p.id: p.gold for p in pairs if p.split == "train"}
