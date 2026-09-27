"""Sealed evidence directories.

``seal`` writes ``manifest.json`` as a flat map of every other file to its
sha256, then returns the sha256 of those exact manifest bytes. ``load_evidence``
hashes those same bytes. It does not re-serialize the manifest. The digest is
passed out of band; a matching digest is what binds the directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

MANIFEST_NAME = "manifest.json"
RUN_NAME = "run.json"

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_FIELDS = ("run_id", "nonce", "fixture_id", "fixture_digest", "variant", "experiment")


class EvidenceError(Exception):
    """The directory does not match the out-of-band manifest digest."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = list(reasons)
        super().__init__("; ".join(self.reasons))


class EvidenceSealedError(Exception):
    """A write was attempted after ``seal``."""


def canonical_json(document: Any) -> bytes:
    """UTF-8 JSON, sorted keys, no insignificant whitespace, one trailing newline."""

    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        + b"\n"
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value: str) -> bool:
    return _SHA256_RE.fullmatch(value) is not None


def _token(value: str, field: str) -> str:
    if not isinstance(value, str) or value == "" or any(char in value for char in "\n\r\t"):
        raise ValueError(f"{field} must be a non-empty single-line string")
    return value


def _sha256_hex(value: str, field: str) -> str:
    if not isinstance(value, str) or not _is_sha256(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return value


def _check_user_name(name: str) -> str:
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise ValueError(f"unsafe evidence file name: {name!r}")
    if name in {MANIFEST_NAME, RUN_NAME}:
        raise ValueError(f"reserved evidence file name: {name}")
    return name


def _on_disk_files(directory: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    reasons: list[str] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        current = Path(dirpath)
        for dirname in dirnames:
            child = current / dirname
            if child.is_symlink():
                reasons.append(f"unexpected symlink: {child.relative_to(directory).as_posix()}")
        for filename in filenames:
            path = current / filename
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink():
                reasons.append(f"unexpected symlink: {rel}")
                continue
            if rel == MANIFEST_NAME:
                continue
            found[rel] = path
    if reasons:
        raise EvidenceError(reasons)
    return found


def write_manifest(directory: Path) -> str:
    """Rewrite ``manifest.json`` from the other files and return its sha256."""

    mapping = {name: _sha256(path.read_bytes()) for name, path in sorted(_on_disk_files(directory).items())}
    payload = canonical_json(mapping)
    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise EvidenceError([f"unexpected symlink: {MANIFEST_NAME}"])
    manifest_path.write_bytes(payload)
    return _sha256(payload)


class Evidence:
    """A directory whose manifest digest matched and whose file hashes matched."""

    def __init__(
        self,
        directory: Path,
        manifest_sha256: str,
        files: Mapping[str, str],
        run_id: str,
        nonce: str,
        fixture_id: str,
        fixture_digest: str,
        variant: str,
        experiment: str,
    ) -> None:
        self.directory = directory
        self.manifest_sha256 = manifest_sha256
        self.files = MappingProxyType(dict(files))
        self.run_id = run_id
        self.nonce = nonce
        self.fixture_id = fixture_id
        self.fixture_digest = fixture_digest
        self.variant = variant
        self.experiment = experiment

    def read_bytes(self, name: str) -> bytes:
        if name not in self.files:
            raise EvidenceError([f"missing file: {name}"])
        return (self.directory / name).read_bytes()

    def read_json(self, name: str) -> Any:
        raw = self.read_bytes(name)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvidenceError([f"malformed {name}"]) from exc

    def read_jsonl(self, name: str) -> list[Any]:
        raw = self.read_bytes(name)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EvidenceError([f"malformed {name}"]) from exc
        records: list[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line == "":
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EvidenceError([f"malformed {name}:{line_no}"]) from exc
        return records


class EvidenceWriter:
    """Write an evidence directory, then seal it.

    ``directory`` is created if needed and must be empty. ``run.json`` records
    ``run_id``, ``nonce``, ``fixture_id``, ``fixture_digest``, ``variant``, and
    ``experiment``. After ``seal``, every write raises ``EvidenceSealedError``.
    """

    def __init__(
        self,
        directory: str | Path,
        run_id: str,
        nonce: str,
        fixture_id: str,
        fixture_digest: str,
        variant: str,
        experiment: str,
    ) -> None:
        self.run_id = _token(run_id, "run_id")
        self.nonce = _token(nonce, "nonce")
        self.fixture_id = _token(fixture_id, "fixture_id")
        self.fixture_digest = _sha256_hex(fixture_digest, "fixture_digest")
        self.variant = _token(variant, "variant")
        self.experiment = _token(experiment, "experiment")
        self.directory = Path(directory)
        self._sealed = False
        self.directory.mkdir(parents=True, exist_ok=True)
        if any(self.directory.iterdir()):
            raise FileExistsError(f"evidence directory is not empty: {self.directory}")
        self._write_identity()

    def _identity(self) -> dict[str, str]:
        return {
            "experiment": self.experiment,
            "fixture_digest": self.fixture_digest,
            "fixture_id": self.fixture_id,
            "nonce": self.nonce,
            "run_id": self.run_id,
            "variant": self.variant,
        }

    def _write_identity(self) -> None:
        (self.directory / RUN_NAME).write_bytes(canonical_json(self._identity()))

    def _guard(self) -> None:
        if self._sealed:
            raise EvidenceSealedError("evidence is sealed")

    def write_json(self, name: str, document: Any) -> None:
        self._guard()
        path = self.directory / _check_user_name(name)
        path.write_bytes(canonical_json(document))

    def append_jsonl(self, name: str, document: Any) -> None:
        self._guard()
        path = self.directory / _check_user_name(name)
        with path.open("ab") as handle:
            handle.write(canonical_json(document))

    def add_file(self, name: str, content: bytes) -> None:
        self._guard()
        if not isinstance(content, bytes):
            raise TypeError("add_file content must be bytes")
        path = self.directory / _check_user_name(name)
        path.write_bytes(content)

    def seal(self) -> str:
        """Write ``manifest.json`` and return the sha256 of its bytes."""

        self._guard()
        self._write_identity()
        digest = write_manifest(self.directory)
        self._sealed = True
        return digest


def _malformed_manifest_entry(key: object) -> str:
    return f"malformed manifest entry: {key!r}"


def load_evidence(directory: str | Path, manifest_sha256: str) -> Evidence:
    """Load ``directory`` if ``manifest_sha256`` is the sha256 of ``manifest.json``.

    Raises ``EvidenceError`` when the digest does not match, a listed file is
    missing or altered, or the directory contains a file the manifest does not
    list. ``manifest.json`` itself is not a listed file.
    """

    directory = Path(directory)
    if not directory.is_dir():
        raise EvidenceError([f"missing evidence directory: {directory}"])
    if not isinstance(manifest_sha256, str):
        raise EvidenceError(["manifest sha256 is not a sha256 hex digest"])
    expected = manifest_sha256.lower()
    if not _is_sha256(expected):
        raise EvidenceError(["manifest sha256 is not a sha256 hex digest"])
    manifest_path = directory / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise EvidenceError([f"unexpected symlink: {MANIFEST_NAME}"])
    if not manifest_path.is_file():
        raise EvidenceError([f"missing file: {MANIFEST_NAME}"])
    manifest_raw = manifest_path.read_bytes()
    if _sha256(manifest_raw) != expected:
        raise EvidenceError(["manifest sha256 mismatch"])
    try:
        parsed = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(["malformed manifest.json"]) from exc
    if not isinstance(parsed, dict):
        raise EvidenceError(["malformed manifest.json"])

    reasons: list[str] = []
    declared: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str) or not _is_sha256(value):
            reasons.append(_malformed_manifest_entry(key))
            continue
        if key == MANIFEST_NAME or _NAME_RE.fullmatch(key) is None or "/" in key or "\\" in key:
            reasons.append(_malformed_manifest_entry(key))
            continue
        declared[key] = value
    on_disk = _on_disk_files(directory)
    for name in sorted(set(declared) - set(on_disk)):
        reasons.append(f"missing file: {name}")
    for name in sorted(set(on_disk) - set(declared)):
        reasons.append(f"extra file: {name}")
    for name in sorted(set(declared) & set(on_disk)):
        if _sha256(on_disk[name].read_bytes()) != declared[name]:
            reasons.append(f"altered file: {name}")
    if reasons:
        raise EvidenceError(reasons)
    if RUN_NAME not in declared:
        raise EvidenceError([f"missing file: {RUN_NAME}"])

    try:
        identity = json.loads(on_disk[RUN_NAME].read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(["malformed run.json"]) from exc
    if not isinstance(identity, dict):
        raise EvidenceError(["malformed run.json"])
    fields: dict[str, str] = {}
    for field in _IDENTITY_FIELDS:
        value = identity.get(field)
        if not isinstance(value, str) or value == "" or any(char in value for char in "\n\r\t"):
            raise EvidenceError(["malformed run.json"])
        fields[field] = value
    if not _is_sha256(fields["fixture_digest"]):
        raise EvidenceError(["malformed run.json"])
    return Evidence(
        directory=directory,
        manifest_sha256=expected,
        files=declared,
        run_id=fields["run_id"],
        nonce=fields["nonce"],
        fixture_id=fields["fixture_id"],
        fixture_digest=fields["fixture_digest"],
        variant=fields["variant"],
        experiment=fields["experiment"],
    )
