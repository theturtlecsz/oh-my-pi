"""R04 content-addressed artifact/source store (mechanical C24 after C23 empty-soft).

Acceptance: bytes/manifests verify; path/archive escapes refused; inaccessible
sources marked; project permissions before retrieval; cache cannot masquerade
as a new independent replicate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
import hashlib

from omp_work.v1.canonical import sha256, validate_execution_path

Access = Literal["allow", "deny", "inaccessible"]


class ArtifactStoreError(ValueError):
    pass


@dataclass
class StoredObject:
    digest: str
    kind: str
    bytes_len: int
    relative_path: str | None = None


@dataclass
class Manifest:
    entries: dict[str, str]  # relative_path -> content digest
    digest: str = ""

    def __post_init__(self) -> None:
        if not self.digest:
            self.digest = sha256(
                {"entries": dict(sorted(self.entries.items())), "kind": "artifact-manifest-v1"}
            )


@dataclass
class ArtifactStore:
    """In-memory + optional on-disk content-addressed store with ACL gate."""

    root: Path | None = None
    _blobs: dict[str, bytes] = field(default_factory=dict)
    _acl: dict[str, Access] = field(default_factory=dict)
    _source_status: dict[str, str] = field(default_factory=dict)
    _cache_index: dict[str, str] = field(default_factory=dict)

    def put_bytes(self, data: bytes, *, relative_path: str | None = None) -> StoredObject:
        if relative_path is not None:
            validate_execution_path(relative_path)
        digest = hashlib.sha256(data).hexdigest()
        if digest not in self._blobs:
            self._blobs[digest] = data
            if self.root is not None:
                dest = self.root / "blobs" / digest[:2] / digest
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    dest.write_bytes(data)
                    dest.chmod(0o400)
        return StoredObject(
            digest=digest, kind="bytes", bytes_len=len(data), relative_path=relative_path
        )

    def put_text(self, text: str, *, relative_path: str | None = None) -> StoredObject:
        return self.put_bytes(text.encode("utf-8"), relative_path=relative_path)

    def put_manifest(self, path_to_digest: dict[str, str]) -> Manifest:
        for path in path_to_digest:
            validate_execution_path(path)
        for digest in path_to_digest.values():
            if digest not in self._blobs:
                if self.root is not None:
                    candidate = self.root / "blobs" / digest[:2] / digest
                    if not candidate.is_file():
                        raise ArtifactStoreError(f"manifest references missing blob: {digest}")
                else:
                    raise ArtifactStoreError(f"manifest references missing blob: {digest}")
        return Manifest(entries=dict(path_to_digest))

    def verify_bytes(self, digest: str, data: bytes) -> None:
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise ArtifactStoreError("bytes digest mismatch")
        if digest in self._blobs and self._blobs[digest] != data:
            raise ArtifactStoreError("stored blob mismatch")

    def verify_manifest(self, manifest: Manifest) -> None:
        expected = sha256(
            {"entries": dict(sorted(manifest.entries.items())), "kind": "artifact-manifest-v1"}
        )
        if manifest.digest != expected:
            raise ArtifactStoreError("manifest digest mismatch")
        for path, digest in manifest.entries.items():
            validate_execution_path(path)
            if digest not in self._blobs:
                raise ArtifactStoreError(f"manifest entry missing blob: {path}")

    def mark_source(self, source_id: str, status: str) -> None:
        if status not in ("ok", "inaccessible"):
            raise ArtifactStoreError("source status must be ok|inaccessible")
        self._source_status[source_id] = status

    def source_status(self, source_id: str) -> str:
        return self._source_status.get(source_id, "inaccessible")

    def set_project_access(self, project_id: str, access: Access) -> None:
        self._acl[project_id] = access

    def retrieve(self, digest: str, *, project_id: str) -> bytes:
        access = self._acl.get(project_id, "deny")
        if access == "deny":
            raise ArtifactStoreError("project permissions deny retrieval")
        if access == "inaccessible":
            raise ArtifactStoreError("source inaccessible")
        if digest not in self._blobs:
            raise ArtifactStoreError("blob not found")
        return self._blobs[digest]

    def remember_cache(self, cache_key: str, digest: str) -> None:
        if digest not in self._blobs:
            raise ArtifactStoreError("cannot cache missing digest")
        self._cache_index[cache_key] = digest

    def claim_independent_replicate(
        self, *, cache_hit: bool, digest: str, claimed_independent: bool
    ) -> None:
        """Refuse cache masquerading as a new independent replicate."""
        if claimed_independent and cache_hit:
            raise ArtifactStoreError(
                "cached result cannot masquerade as new independent replicate"
            )
        if claimed_independent and digest in self._cache_index.values():
            raise ArtifactStoreError(
                "cached result cannot masquerade as new independent replicate"
            )
