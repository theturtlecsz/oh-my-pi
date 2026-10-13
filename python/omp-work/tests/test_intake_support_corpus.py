"""The committed intake-support corpus keeps its composition, label isolation and seal."""

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from omp_work import intake_support_corpus as corpus
from omp_work.frozen_corpus import verify

ROOT = Path(__file__).resolve().parents[3] / "evals/intake_support/v1"
INPUTS, MANIFEST = ROOT / "inputs", ROOT / "manifest.json"


@pytest.fixture(scope="module")
def pairs() -> list[corpus.Pair]:
    return corpus.load_pairs(INPUTS, MANIFEST)


def test_composition(pairs) -> None:
    assert len(pairs) == 60
    assert sum(p.split == "heldout" for p in pairs) == 30
    assert Counter(p.near_miss for p in pairs if p.near_miss) == dict.fromkeys(
        ["topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context"], 6)
    for split in ("train", "heldout"):
        assert Counter(p.near_miss is not None for p in pairs if p.split == split)[True] == 12
        assert sum(p.gold == "supports" for p in pairs if p.split == split) == 18


def test_judge_input_hides_labels(pairs) -> None:
    for pair in pairs:
        assert set(corpus.judge_input(pair)) == {"id", "claim_text", "passage", "context"}


def test_only_train_labels_are_released(pairs) -> None:
    labels = corpus.training_labels(pairs, "train")
    assert set(labels) == {p.id for p in pairs if p.split == "train"}
    with pytest.raises(ValueError, match="only train"):
        corpus.training_labels(pairs, "heldout")


def test_manifest_origin_is_a_commit() -> None:
    sealed = verify(INPUTS, MANIFEST)
    assert len(sealed.origin_commit) == 40


@pytest.fixture
def copy(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "inputs"
    shutil.copytree(INPUTS, root)
    shutil.copy(MANIFEST, tmp_path / "manifest.json")
    return root, tmp_path / "manifest.json"


def _edit(root: Path, edit) -> None:
    document = json.loads((root / "pairs.json").read_text())
    edit(document["pairs"])
    (root / "pairs.json").write_text(json.dumps(document, indent=2) + "\n")


def test_changed_input_byte_fails_verify(copy) -> None:
    root, manifest = copy
    (root / "pairs.json").write_bytes((root / "pairs.json").read_bytes().replace(b"Fridays", b"Fridaze", 1))
    with pytest.raises(ValueError, match="drift"):
        corpus.load_pairs(root, manifest)


def test_changed_split_fails_verify(copy) -> None:
    root, manifest = copy
    _edit(root, lambda rows: rows[0].update(split="heldout"))
    with pytest.raises(ValueError, match="drift"):
        corpus.load_pairs(root, manifest)


@pytest.mark.parametrize("edit,message", [
    (lambda rows: rows[0].update(split="heldout"), "one split|held-out|near-misses|supports"),
    (lambda rows: rows[1].update(near_miss="negation"), "near-misses per kind"),
    (lambda rows: rows.pop(), "60 unique"),
])
def test_resealed_invalid_composition_is_rejected(tmp_path: Path, edit, message) -> None:
    root = tmp_path / "inputs"
    shutil.copytree(INPUTS, root)
    _edit(root, edit)
    manifest = tmp_path / "manifest.json"
    corpus.seal(root, "a" * 40, manifest)
    with pytest.raises(ValueError, match=message):
        corpus.load_pairs(root, manifest)
