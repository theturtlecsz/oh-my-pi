"""Byte-bound corpus snapshots shared by Harbor and local replay studies."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from omp_work.operations.artifacts import install_bytes_artifact
from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import StrictModel, hex64


def tree_sha256(directory: str | Path) -> str:
    """sha256 over files in directory in sorted relpath order.

    Each file contributes: sorted relpath, NUL, 8-byte BE length, bytes.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise ValueError(f"not a directory: {directory}")
    files: list[tuple[str, Path]] = []
    for dirpath, _, filenames in os.walk(dir_path, followlinks=False):
        current = Path(dirpath)
        for filename in filenames:
            path = current / filename
            if path.is_file():
                relpath = path.relative_to(dir_path).as_posix()
                files.append((relpath, path))
    files.sort(key=lambda item: item[0])
    hasher = hashlib.sha256()
    for relpath, path in files:
        data = path.read_bytes()
        hasher.update(relpath.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


class CorpusFile(StrictModel):
    id: str = Field(min_length=1, pattern=r"\S")
    path: str
    sha256: hex64

    @field_validator("path")
    @classmethod
    def relative_json(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (not value or path.is_absolute() or ".." in path.parts
                or path.as_posix() != value or "\\" in value or "\0" in value
                or path.suffix != ".json"):
            raise ValueError("path must be a canonical relative JSON path")
        return value


class FrozenCorpus(StrictModel):
    version: Literal[1] = 1
    origin_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    files: list[CorpusFile] = Field(min_length=1)
    tree_sha256: hex64
    manifest_sha256: hex64

    @model_validator(mode="after")
    def unique_files(self) -> FrozenCorpus:
        if len({f.id for f in self.files}) != len(self.files):
            raise ValueError("duplicate ids")
        if len({f.path for f in self.files}) != len(self.files):
            raise ValueError("duplicate paths")
        return self


def _snapshot(directory: Path, files: Sequence[CorpusFile]) -> list[CorpusFile]:
    root = directory.resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {directory}")
    actual: set[str] = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            if (Path(current) / name).is_symlink():
                raise ValueError("symlinks are not corpus inputs")
        actual.update((Path(current) / name).relative_to(root).as_posix() for name in names)
    expected = {file.path for file in files}
    if actual != expected:
        raise ValueError(f"file set drift: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    result = []
    for file in sorted(files, key=lambda f: f.path):
        data = (root / file.path).read_bytes()
        json.loads(data)  # JSON parsing boundary; preserve original bytes for hashing.
        result.append(CorpusFile(id=file.id, path=file.path, sha256=hashlib.sha256(data).hexdigest()))
    return result


def _manifest_path(directory: Path, manifest: Path) -> None:
    if manifest.resolve().is_relative_to(directory.resolve()):
        raise ValueError("manifest must live outside input directory")


def freeze(
    directory: str | Path,
    files: Sequence[tuple[str, str]],
    origin_commit: str,
    manifest: str | Path,
) -> FrozenCorpus:
    """Seal exactly the listed (id, relative JSON path) pairs; never overwrite drift."""
    root, destination = Path(directory), Path(manifest)
    _manifest_path(root, destination)
    draft = FrozenCorpus(
        origin_commit=origin_commit,
        files=[CorpusFile(id=id, path=path, sha256="0" * 64) for id, path in files],
        tree_sha256="0" * 64, manifest_sha256="0" * 64,
    )
    body = {
        "version": draft.version, "origin_commit": draft.origin_commit,
        "files": [f.model_dump() for f in _snapshot(root, draft.files)],
        "tree_sha256": tree_sha256(root),
    }
    sealed = FrozenCorpus(**body, manifest_sha256=sha256(body))
    data = canonical_json(sealed.model_dump()).encode("utf-8")
    install_bytes_artifact(destination, data, hashlib.sha256(data).hexdigest())
    return sealed


def verify(directory: str | Path, manifest: str | Path) -> FrozenCorpus:
    """Return validated manifest or raise ValueError on malformed inputs or drift."""
    root, source = Path(directory), Path(manifest)
    _manifest_path(root, source)
    sealed = FrozenCorpus.model_validate_json(source.read_bytes())
    if sha256(sealed.model_dump(exclude={"manifest_sha256"})) != sealed.manifest_sha256:
        raise ValueError("manifest digest drift")
    if _snapshot(root, sealed.files) != sorted(sealed.files, key=lambda f: f.path):
        raise ValueError("file byte drift")
    if tree_sha256(root) != sealed.tree_sha256:
        raise ValueError("tree digest drift")
    return sealed
