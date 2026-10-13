"""Frozen replay inputs reject byte, membership, identity and manifest drift."""

import hashlib
import json
from pathlib import Path

import pytest

from omp_work import frozen_corpus
from omp_work.frozen_corpus import freeze, tree_sha256, verify

COMMIT = "a" * 40
FILES = [("first", "a.json"), ("second", "nested/b.json")]


@pytest.fixture
def corpus(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "inputs"
    (root / "nested").mkdir(parents=True)
    (root / "a.json").write_bytes(b'{"answer":1}\n')
    (root / "nested/b.json").write_bytes(b'{"answer":2}')
    return root, tmp_path / "manifest.json"


def test_harbor_wire_digest_and_enumeration_order(corpus, monkeypatch) -> None:
    root, _ = corpus
    expected = hashlib.sha256()
    for name in ("a.json", "nested/b.json"):
        data = (root / name).read_bytes()
        expected.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    original_walk = frozen_corpus.os.walk

    def reversed_walk(*args, **kwargs):
        return reversed([(path, list(reversed(dirs)), list(reversed(names)))
                         for path, dirs, names in original_walk(*args, **kwargs)])

    monkeypatch.setattr(frozen_corpus.os, "walk", reversed_walk)
    assert tree_sha256(root) == expected.hexdigest()


def test_seal_is_order_independent_and_reusable(corpus) -> None:
    root, manifest = corpus
    sealed = freeze(root, FILES, COMMIT, manifest)
    assert verify(root, manifest) == sealed
    assert freeze(root, list(reversed(FILES)), COMMIT, manifest) == sealed
    assert sealed.files[0].sha256 == hashlib.sha256((root / "a.json").read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="artifact_unavailable"):
        freeze(root, FILES, "b" * 40, manifest)
    assert verify(root, manifest) == sealed


@pytest.mark.parametrize("change", ["bytes", "rename", "delete", "add", "add_non_json"])
def test_changed_inputs_invalidate_seal(corpus, change) -> None:
    root, manifest = corpus
    freeze(root, FILES, COMMIT, manifest)
    if change == "bytes":
        (root / "a.json").write_bytes(b'{ "answer": 1 }\n')
    elif change == "rename":
        (root / "a.json").rename(root / "renamed.json")
    elif change == "delete":
        (root / "a.json").unlink()
    else:
        (root / ("extra.json" if change == "add" else "extra.txt")).write_text("{}")
    with pytest.raises(ValueError, match="drift"):
        verify(root, manifest)


@pytest.mark.parametrize("files, message", [
    ([("same", "a.json"), ("same", "nested/b.json")], "duplicate ids"),
    ([("one", "a.json"), ("two", "a.json")], "duplicate paths"),
    ([("one", "missing.json")], "file set drift"),
    ([("one", "a.json")], "file set drift"),
    ([], "at least 1"),
])
def test_bad_listing_never_creates_manifest(corpus, files, message) -> None:
    root, manifest = corpus
    with pytest.raises(ValueError, match=message):
        freeze(root, files, COMMIT, manifest)
    assert not manifest.exists()


@pytest.mark.parametrize("path", ["../outside.json", "/outside.json", "nested/../../x.json",
                                  "./a.json", "nested//b.json", "nested\\b.json", "a.txt"])
def test_path_escapes_and_aliases_rejected(corpus, path) -> None:
    root, manifest = corpus
    with pytest.raises(ValueError, match="canonical relative JSON"):
        freeze(root, [("escape", path)], COMMIT, manifest)
    assert not manifest.exists()


@pytest.mark.parametrize("directory_link", [False, True])
def test_symlink_inputs_rejected(corpus, directory_link, tmp_path) -> None:
    root, manifest = corpus
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "case.json").write_text("{}")
    if directory_link:
        (root / "link").symlink_to(outside, target_is_directory=True)
    else:
        (root / "a.json").unlink()
        (root / "a.json").symlink_to(outside / "case.json")
    with pytest.raises(ValueError, match="symlinks"):
        freeze(root, FILES, COMMIT, manifest)


@pytest.mark.parametrize("field, value", [("origin_commit", "b" * 40), ("version", 2),
                                         ("manifest_sha256", "0" * 64)])
def test_manifest_tampering_rejected(corpus, field, value) -> None:
    root, manifest = corpus
    freeze(root, FILES, COMMIT, manifest)
    document = json.loads(manifest.read_bytes())
    document[field] = value
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        verify(root, manifest)


def test_manifest_cannot_be_part_of_hashed_inputs(corpus) -> None:
    root, _ = corpus
    with pytest.raises(ValueError, match="outside input directory"):
        freeze(root, FILES, COMMIT, root / "manifest.json")
    with pytest.raises(ValueError, match="outside input directory"):
        verify(root, root / "manifest.json")


def test_invalid_json_and_origin_refused(corpus) -> None:
    root, manifest = corpus
    with pytest.raises(ValueError, match="origin_commit"):
        freeze(root, FILES, "main", manifest)
    (root / "a.json").write_text("not JSON")
    with pytest.raises(ValueError):
        freeze(root, FILES, COMMIT, manifest)
    assert not manifest.exists()


def test_tracked_harbor_fingerprints_remain_byte_compatible() -> None:
    repo = Path(__file__).resolve().parents[3]
    manifest = json.loads((repo / "evals/harbor/corpus/manifest.json").read_bytes())
    for task_id, task in manifest["tasks"].items():
        directory = repo / task["fixture"]
        legacy = hashlib.sha256()
        for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
            if path.is_file():
                data = path.read_bytes()
                legacy.update(path.relative_to(directory).as_posix().encode("utf-8"))
                legacy.update(b"\0")
                legacy.update(len(data).to_bytes(8, "big"))
                legacy.update(data)
        assert tree_sha256(directory) == legacy.hexdigest(), task_id


def test_duplicate_ids_in_manifest_rejected_even_with_matching_digest(corpus) -> None:
    root, manifest = corpus
    freeze(root, FILES, COMMIT, manifest)
    document = json.loads(manifest.read_bytes())
    document["files"][1]["id"] = document["files"][0]["id"]
    body = {key: value for key, value in document.items() if key != "manifest_sha256"}
    document["manifest_sha256"] = frozen_corpus.sha256(body)
    manifest.chmod(0o600)
    manifest.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="duplicate ids"):
        verify(root, manifest)
