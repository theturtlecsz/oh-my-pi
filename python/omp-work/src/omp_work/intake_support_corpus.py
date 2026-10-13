"""Hand-labelled claim/passage pairs for the intake support judge.

Judges see ``judge_input`` only; gold, rationale and near-miss tags stay behind
``training_labels``, and only for the train split.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from omp_work.frozen_corpus import FrozenCorpus, freeze, verify
from omp_work.v1.models import StrictModel

Gold = Literal["supports", "contradicts", "neither"]
NearMiss = Literal["topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context"]
Split = Literal["train", "heldout"]

PAIRS_FILE = "pairs.json"
RULES_FILE = "rules.json"
FILES = [("intake-support-pairs-v1", PAIRS_FILE), ("intake-support-rules-v1", RULES_FILE)]
PAIR_COUNT = 60
HELDOUT_COUNT = 30
NEAR_MISS_PER_KIND = 6
NEAR_MISS_PER_SPLIT = 12
SUPPORTS_PER_SPLIT = 18
JUDGE_FIELDS = ("id", "claim_text", "passage", "context")


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


def _validated(pairs: list[Pair], rules: dict) -> list[Pair]:
    if rules.get("version") != 1 or set(rules.get("labels", {})) != {"supports", "contradicts", "neither"}:
        raise ValueError("rules must define the three labels")
    kinds = set(NearMiss.__args__)
    if set(rules.get("near_miss_kinds", {})) != kinds or not rules.get("context_rule"):
        raise ValueError("rules must define the four near-miss kinds and the context rule")
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


def load_pairs(directory: str | Path, manifest: str | Path) -> list[Pair]:
    """Verify the seal, then return the validated pairs; raise ValueError on drift or malformed data."""
    root = Path(directory)
    verify(root, manifest)
    rules = json.loads((root / RULES_FILE).read_bytes())
    document = json.loads((root / PAIRS_FILE).read_bytes())
    if document.get("version") != 1:
        raise ValueError("unsupported corpus version")
    return _validated([Pair.model_validate(row) for row in document["pairs"]], rules)


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
