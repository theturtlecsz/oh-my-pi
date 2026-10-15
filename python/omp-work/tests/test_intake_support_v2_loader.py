"""V2 loading enforces provenance, split isolation and matched near-miss groups."""

import hashlib
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from omp_work import intake_support_corpus as corpus
from omp_work.frozen_corpus import freeze

KINDS = ("topic_wrong_claim", "negation", "scope_mismatch", "quote_out_of_context", "adjacent_inference")
V1_INPUTS = Path(__file__).resolve().parents[3] / "evals/intake_support/v1/inputs"


def passage(words: int, endings: str = ".!") -> str:
    tokens = ["evidence"] * words
    tokens[words // 2 - 1] += endings[:1]
    tokens[-1] += endings[1:]
    return " ".join(tokens)


@pytest.fixture
def documents() -> tuple[dict, dict]:
    rows = []
    for split, count in (("train", 20), ("heldout", 60)):
        for index in range(count):
            group = f"{split}-{index}"
            for positive in (False, True):
                rows.append({
                    "id": f"{group}-{positive}", "group": group, "split": split,
                    "claim_text": "The deployment requires two approvals.",
                    "passage": passage(60), "context": "Deployment guide.",
                    "gold": "supports" if positive else ("contradicts" if index % 2 else "neither"),
                    "rationale": "The passage establishes the approval requirement." if positive else "Wrong scope.",
                    "near_miss": None if positive else KINDS[index % len(KINDS)],
                    "source_path": f"docs/{group}.md", "source_revision": "a" * 40,
                    "source_start_line": 1, "source_end_line": 5,
                })
    return {"version": 2, "pairs": rows}, {
        "version": 2, "labels": dict.fromkeys(("supports", "contradicts", "neither"), "Definition"),
        "near_miss_kinds": dict.fromkeys(KINDS, "Definition"),
        "context_rule": "Read the passage in context.",
        "rule_sha256": None, "approval_sha256": None,
    }


def prepared(tmp_path: Path, documents: tuple[dict, dict]) -> tuple[Path, Path]:
    root = tmp_path / "inputs"
    root.mkdir()
    for name, document in zip(("pairs.json", "rules.json"), documents, strict=True):
        (root / name).write_text(json.dumps(document))
    rule_doc = tmp_path / "rule.md"
    rule_doc.write_text("Approved synthetic policy.\n```json\n" + json.dumps(documents[1]) + "\n```\n")
    (tmp_path / "approval.json").write_text(json.dumps({
        "version": 2, "rule_sha256": hashlib.sha256(rule_doc.read_bytes()).hexdigest(),
        "approval_sha256": None,
    }))
    return root, tmp_path / "manifest.json"


def paths(root: Path) -> dict[str, Path]:
    return {"rule_doc": root.parent / "rule.md", "approval": root.parent / "approval.json"}


def load(root: Path, manifest: Path, version: int = 1) -> list[corpus.Pair]:
    return corpus.load_pairs(root, manifest, version=version, **paths(root))


def frozen(tmp_path: Path, documents: tuple[dict, dict], version: int = 2) -> tuple[Path, Path]:
    root, manifest = prepared(tmp_path, documents)
    rules = json.loads((root / "rules.json").read_bytes())
    rules["approval_sha256"] = hashlib.sha256(paths(root)["approval"].read_bytes()).hexdigest()
    rules["rule_sha256"] = hashlib.sha256(paths(root)["rule_doc"].read_bytes()).hexdigest()
    (root / "rules.json").write_text(json.dumps(rules))
    # Deliberately bypass public seal: invalid fixtures must reach loader validation.
    freeze(root, corpus.files_for(version), "b" * 40, manifest)
    return root, manifest


def test_valid_splits_keep_judge_and_training_views_isolated(tmp_path, documents) -> None:
    pairs = load(*frozen(tmp_path, documents), version=2)
    assert all(isinstance(pair, corpus.Pair) for pair in pairs)
    assert Counter(p.split for p in pairs if p.gold != "supports") == {"train": 20, "heldout": 60}
    for split in ("train", "heldout"):
        assert {p.near_miss for p in pairs if p.split == split and p.gold != "supports"} == set(KINDS)
    for pair in pairs:
        assert corpus.judge_input(pair) == {
            "id": pair.id, "claim_text": pair.claim_text, "passage": pair.passage, "context": pair.context,
        }
    assert corpus.training_labels(pairs, "train") == {p.id: p.gold for p in pairs if p.split == "train"}
    with pytest.raises(ValueError, match="only train"):
        corpus.training_labels(pairs, "heldout")


def test_versioned_file_ids() -> None:
    assert corpus.files_for(1) == corpus.FILES
    assert corpus.files_for(2) == [
        ("intake-support-pairs-v2", "pairs.json"), ("intake-support-rules-v2", "rules.json"),
    ]


def test_negative_counts_are_minimums(tmp_path, documents) -> None:
    rows = documents[0]["pairs"]
    for original in rows[:2] + rows[40:42]:
        extra = dict(original)
        extra.update(id=f"{original['id']}-extra", group=f"{original['group']}-extra")
        rows.append(extra)
    pairs = load(*frozen(tmp_path, documents), version=2)
    assert Counter(p.split for p in pairs if p.gold != "supports") == {"train": 21, "heldout": 61}


@pytest.mark.parametrize("version", [0, 3, "2", True, 2.0])
def test_unsupported_versions(tmp_path, version) -> None:
    with pytest.raises(ValueError, match="unsupported corpus version"):
        corpus.files_for(version)
    with pytest.raises(ValueError, match="unsupported corpus version"):
        corpus.seal(tmp_path / "missing", "a" * 40, tmp_path / "missing.json", version=version)
    with pytest.raises(ValueError, match="unsupported corpus version"):
        corpus.load_pairs(tmp_path / "missing", tmp_path / "missing.json", version=version)


@pytest.mark.parametrize("document_index", [0, 1])
@pytest.mark.parametrize("version", [1, 3, True, 2.0, None])
def test_document_version_must_match_request(tmp_path, documents, document_index, version) -> None:
    documents[document_index]["version"] = version
    with pytest.raises(ValueError, match="version must match"):
        load(*frozen(tmp_path, documents), version=2)


def test_v2_requires_explicit_opt_in(tmp_path, documents) -> None:
    with pytest.raises(ValueError, match="document version must match"):
        load(*frozen(tmp_path, documents))


@pytest.mark.parametrize("edit,message", [
    (lambda rows: rows.__delitem__(slice(0, 2)), "train must hold at least 20 negatives"),
    (lambda rows: rows.__delitem__(slice(40, 42)), "heldout must hold at least 60 negatives"),
    (lambda rows: rows[1].update(id=rows[0]["id"]), "unique pair ids"),
    (lambda rows: rows[0].update(id=""), "id"),
    (lambda rows: rows[0].update(id="  \t"), "id"),
    (lambda rows: rows[0].update(group=""), "group"),
    (lambda rows: rows[0].update(group="  \t"), "group"),
    (lambda rows: rows[0].update(near_miss=None), "tagged near-miss"),
    (lambda rows: rows[0].update(near_miss="invented"), "near_miss"),
    (lambda rows: rows[1].update(near_miss="adjacent_inference"), "cannot be gold supports"),
    (lambda rows: rows[0].update(gold="supports", near_miss=None), "exactly one negative"),
    (lambda rows: rows[1].update(gold="neither", near_miss="negation"), "exactly one negative"),
    (lambda rows: rows.pop(1), "exactly one negative"),
    (lambda rows: rows[40].update(group=rows[0]["group"]), "group must stay in one split"),
])
def test_resealed_invalid_composition(tmp_path, documents, edit, message) -> None:
    edit(documents[0]["pairs"])
    root, manifest = frozen(tmp_path, documents)
    original = (root / "rules.json").read_bytes()
    failed_manifest = tmp_path / "failed-manifest.json"
    with pytest.raises(ValueError, match=message):
        corpus.seal(root, "a" * 40, failed_manifest, version=2, **paths(root))
    assert not failed_manifest.exists()
    assert (root / "rules.json").read_bytes() == original
    with pytest.raises(ValueError, match=message):
        load(root, manifest, version=2)


@pytest.mark.parametrize("split", ["train", "heldout"])
def test_each_split_requires_all_five_kinds(tmp_path, documents, split) -> None:
    for row in documents[0]["pairs"]:
        if row["split"] == split and row["near_miss"] == "adjacent_inference":
            row["near_miss"] = "negation"
    with pytest.raises(ValueError, match=f"{split} must hold all five"):
        load(*frozen(tmp_path, documents), version=2)


@pytest.mark.parametrize("field,value", [
    ("source_path", ""), ("source_path", "./docs/x.md"),
    ("source_path", "docs/./x.md"), ("source_path", "docs/../x.md"),
    ("source_path", "docs//x.md"), ("source_path", "/docs/x.md"),
    ("source_path", "docs\\x.md"), ("source_path", "C:/docs/x.md"),
    ("source_revision", "a" * 39), ("source_revision", "a" * 41),
    ("source_revision", "g" * 40),
    ("source_start_line", 0), ("source_end_line", 0),
    ("source_start_line", 6), ("source_end_line", -1),
    ("source_start_line", True), ("source_end_line", False),
    ("source_start_line", 1.0), ("source_end_line", "5"),
])
def test_resealed_invalid_source_metadata(tmp_path, documents, field, value) -> None:
    documents[0]["pairs"][0][field] = value
    with pytest.raises(ValueError, match="source_|plan path"):
        load(*frozen(tmp_path, documents), version=2)


@pytest.mark.parametrize("field", ["source_path", "source_revision", "source_start_line", "source_end_line"])
def test_source_metadata_required(tmp_path, documents, field) -> None:
    del documents[0]["pairs"][0][field]
    with pytest.raises(ValueError, match=field):
        load(*frozen(tmp_path, documents), version=2)


@pytest.mark.parametrize("heldout_path", ["docs/x.md", "./docs/x.md"])
@pytest.mark.parametrize("different_location", [False, True])
def test_source_identity_cannot_cross_splits(tmp_path, documents, heldout_path, different_location) -> None:
    rows = documents[0]["pairs"]
    rows[0]["source_path"] = "docs/x.md"
    rows[40]["source_path"] = heldout_path
    if different_location:
        rows[40].update(source_revision="b" * 40, source_start_line=100, source_end_line=200)
    with pytest.raises(ValueError, match="source must stay in one split|plan path"):
        load(*frozen(tmp_path, documents), version=2)


def test_sources_may_repeat_within_split_and_lines_are_inclusive(tmp_path, documents) -> None:
    rows = documents[0]["pairs"]
    rows[2].update(source_path=rows[0]["source_path"], source_revision="b" * 40,
                   source_start_line=5, source_end_line=5)
    pairs = load(*frozen(tmp_path, documents), version=2)
    assert pairs[2].source_start_line == pairs[2].source_end_line == 5
    assert pairs[2].source_path == pairs[0].source_path


@pytest.mark.parametrize("text", [passage(59), passage(301), passage(60, "."),
                                      passage(60, ""), "word.!word " * 60])
def test_resealed_invalid_passage_boundaries(tmp_path, documents, text) -> None:
    documents[0]["pairs"][0]["passage"] = text
    with pytest.raises(ValueError, match="passage must contain"):
        load(*frozen(tmp_path, documents), version=2)


@pytest.mark.parametrize("words,endings", [(60, ".!"), (300, "!?"), (60, "?.")])
def test_inclusive_passage_boundaries_and_terminal_punctuation(tmp_path, documents, words, endings) -> None:
    text = passage(words, endings).replace(" ", "\t\n")
    documents[0]["pairs"][0]["passage"] = text
    assert load(*frozen(tmp_path, documents), version=2)[0].passage == text


@pytest.mark.parametrize("edit,message", [
    (lambda rules: rules["labels"].pop("neither"), "three labels"),
    (lambda rules: rules["near_miss_kinds"].pop("adjacent_inference"), "5 near-miss kinds"),
    (lambda rules: rules["near_miss_kinds"].update(invented="Unknown"), "5 near-miss kinds"),
    (lambda rules: rules.update(context_rule=""), "context rule"),
])
def test_resealed_invalid_rules(tmp_path, documents, edit, message) -> None:
    edit(documents[1])
    with pytest.raises(ValueError, match=message):
        load(*frozen(tmp_path, documents), version=2)


@pytest.mark.parametrize("change_rules", [False, True])
def test_v1_rejects_adjacent_inference(tmp_path, change_rules) -> None:
    root = tmp_path / "inputs"
    shutil.copytree(V1_INPUTS, root)
    if change_rules:
        path = root / "rules.json"
        document = json.loads(path.read_text())
        document["near_miss_kinds"]["adjacent_inference"] = "Adjacent fact does not entail claim."
    else:
        path = root / "pairs.json"
        document = json.loads(path.read_text())
        next(row for row in document["pairs"] if row["near_miss"])["near_miss"] = "adjacent_inference"
    path.write_text(json.dumps(document))
    manifest = tmp_path / "manifest.json"
    freeze(root, corpus.files_for(1), "a" * 40, manifest)
    with pytest.raises(ValueError, match="near_miss|near-miss kinds"):
        corpus.load_pairs(root, manifest)


def test_public_seal_binds_policy_and_identical_reseal(tmp_path, documents) -> None:
    root, manifest = prepared(tmp_path, documents)
    sealed = corpus.seal(root, "a" * 40, manifest, version=2, **paths(root))
    assert {(file.id, file.path) for file in sealed.files} == set(corpus.files_for(2))
    rules_bytes = (root / "rules.json").read_bytes()
    rules = json.loads(rules_bytes)
    assert rules["rule_sha256"] == hashlib.sha256(paths(root)["rule_doc"].read_bytes()).hexdigest()
    assert rules["approval_sha256"] == hashlib.sha256(paths(root)["approval"].read_bytes()).hexdigest()
    assert len(load(root, manifest, version=2)) == 160
    assert corpus.seal(root, "a" * 40, manifest, version=2, **paths(root)) == sealed
    assert (root / "rules.json").read_bytes() == rules_bytes


@pytest.mark.parametrize("field", ["rule_sha256", "approval_sha256"])
@pytest.mark.parametrize("value", [None, "0" * 64])
def test_resealed_missing_or_stale_binding_rejects_load(tmp_path, documents, field, value) -> None:
    root, _manifest = frozen(tmp_path, documents)
    rules = json.loads((root / "rules.json").read_bytes())
    rules[field] = value
    (root / "rules.json").write_text(json.dumps(rules))
    freeze(root, corpus.files_for(2), "b" * 40, tmp_path / "resealed.json")
    with pytest.raises(ValueError, match=f"{field} binding drift"):
        load(root, tmp_path / "resealed.json", version=2)


@pytest.mark.parametrize("edit", [
    lambda rules: rules.update(context_rule="Different policy"),
    lambda rules: rules["labels"].update(supports="Different definition"),
    lambda rules: rules.update(extra="Unapproved"),
    lambda rules: rules.pop("rule_sha256"),
    lambda rules: rules.pop("approval_sha256"),
    lambda rules: rules.update(rule_sha256="0" * 64),
    lambda rules: rules.update(approval_sha256="0" * 64),
])
def test_unapproved_rules_rejected_without_writes(tmp_path, documents, edit) -> None:
    root, manifest = prepared(tmp_path, documents)
    rules_path = root / "rules.json"
    rules = json.loads(rules_path.read_bytes())
    edit(rules)
    rules_path.write_text(json.dumps(rules))
    original = rules_path.read_bytes()
    with pytest.raises(ValueError, match="approved policy|binding drift"):
        corpus.seal(root, "a" * 40, manifest, version=2, **paths(root))
    assert not manifest.exists()
    assert rules_path.read_bytes() == original
    freeze(root, corpus.files_for(2), "a" * 40, manifest)
    with pytest.raises(ValueError, match="approved policy|binding drift"):
        load(root, manifest, version=2)


@pytest.mark.parametrize("change", ["doc", "approved_doc", "approval_bytes", "digest", "missing", "malformed"])
def test_external_approval_drift_rejects_seal_and_load(tmp_path, documents, change) -> None:
    root, manifest = prepared(tmp_path, documents)
    corpus.seal(root, "a" * 40, manifest, version=2, **paths(root))
    rule_doc, approval = paths(root).values()
    if change in ("doc", "approved_doc"):
        rule_doc.write_bytes(rule_doc.read_bytes() + b"Changed prose.\n")
        if change == "approved_doc":
            record = json.loads(approval.read_bytes())
            record["rule_sha256"] = hashlib.sha256(rule_doc.read_bytes()).hexdigest()
            approval.write_text(json.dumps(record))
    elif change == "approval_bytes":
        approval.write_bytes(approval.read_bytes() + b"\n")
    elif change == "digest":
        record = json.loads(approval.read_bytes())
        record["rule_sha256"] = "0" * 64
        approval.write_text(json.dumps(record))
    elif change == "missing":
        approval.unlink()
    else:
        approval.write_text("{")
    original = (root / "rules.json").read_bytes()
    new_manifest = tmp_path / "new-manifest.json"
    with pytest.raises((ValueError, FileNotFoundError)):
        corpus.seal(root, "a" * 40, new_manifest, version=2, **paths(root))
    assert not new_manifest.exists()
    assert (root / "rules.json").read_bytes() == original
    with pytest.raises((ValueError, FileNotFoundError)):
        load(root, manifest, version=2)


@pytest.mark.parametrize("alias", ["./docs/x.md", "docs/../x.md"])
def test_aliases_reject_seal_before_binding(tmp_path, documents, alias) -> None:
    documents[0]["pairs"][0]["source_path"] = alias
    root, manifest = prepared(tmp_path, documents)
    original = (root / "rules.json").read_bytes()
    with pytest.raises(ValueError, match="plan path"):
        corpus.seal(root, "a" * 40, manifest, version=2, **paths(root))
    assert not manifest.exists()
    assert (root / "rules.json").read_bytes() == original


def test_v1_raw_seal_and_load_never_require_approval(tmp_path) -> None:
    root = tmp_path / "inputs"
    shutil.copytree(V1_INPUTS, root)
    manifest = tmp_path / "manifest.json"
    original = (root / "rules.json").read_bytes()
    missing = {"rule_doc": tmp_path / "absent.md", "approval": tmp_path / "absent.json"}
    sealed = corpus.seal(root, "a" * 40, manifest, **missing)
    assert {(file.id, file.path) for file in sealed.files} == set(corpus.files_for(1))
    assert len(corpus.load_pairs(root, manifest, **missing)) == 60
    assert (root / "rules.json").read_bytes() == original


def test_v2_rejects_manifest_with_v1_ids(tmp_path, documents) -> None:
    root, _manifest = frozen(tmp_path, documents)
    manifest = tmp_path / "wrong-ids.json"
    freeze(root, corpus.files_for(1), "a" * 40, manifest)
    with pytest.raises(ValueError, match="manifest file ids"):
        load(root, manifest, version=2)
