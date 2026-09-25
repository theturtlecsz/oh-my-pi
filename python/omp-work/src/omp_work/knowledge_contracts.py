from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import AliasChoices, Field, field_validator, model_validator

from .v1.canonical import sha256
from .v1.models import StrictModel

CODE_SNAPSHOT_ALGORITHM = "work.omp.dev/v1/code-snapshot"
CHILD_FACT_ALGORITHM = "work.omp.dev/v1/child-fact"

_HEX32_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_HEX_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")

ALLOWED_FILE_KINDS = ("base", "modified", "untracked", "deleted")
FileKind = Literal["base", "modified", "untracked", "deleted"]

ALLOWED_JOB_STATES = ("queued", "running", "succeeded", "failed", "cancelled")
JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]

ALLOWED_EXTRACTION_STATES = ("extracted", "no_lesson")
ExtractionState = Literal["extracted", "no_lesson"]

ALLOWED_OPERATIONS = frozenset({"ingest", "query", "lookup", "correct", "status"})
OperationKind = Literal["ingest", "query", "lookup", "correct", "status"]


def validate_manifest_path(path: str) -> str:
    if not path or not isinstance(path, str) or path.strip() != path:
        raise ValueError(f"invalid relative path: {path!r}")
    if path.startswith(("/", "\\")) or (
        len(path) >= 2 and path[1] == ":" and path[0].isalpha()
    ):
        raise ValueError(f"path must be relative: {path!r}")
    if path.startswith("./") or path.endswith("/") or "\\" in path or "//" in path:
        raise ValueError(f"path must use clean POSIX relative separators: {path!r}")
    segments = path.split("/")
    if any(s in (".", "..", "") for s in segments):
        raise ValueError(f"path must not contain . or .. segments: {path!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ValueError(f"path contains control characters: {path!r}")
    return path


class RepositoryIdentity(StrictModel):
    """Authoritative native identity for a repository."""

    workspace_id: UUID = Field(validation_alias=AliasChoices("workspace_id", "ws"))
    repository_id: UUID = Field(validation_alias=AliasChoices("repository_id", "repo"))
    canonical_remote_url: str = Field(min_length=1)
    root_commits: tuple[str, ...]
    verified_at: datetime

    @field_validator("root_commits", mode="before")
    @classmethod
    def _validate_root_commits(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple, set)):
            commits = list(v)
        else:
            raise TypeError(f"root_commits must be an iterable, got {type(v).__name__}")
        for c in commits:
            if not isinstance(c, str) or not _COMMIT_HEX_PATTERN.fullmatch(c):
                raise ValueError(
                    f"root_commit must be a 40 or 64 hex character commit sha, got {c!r}"
                )
        return tuple(sorted(set(commits)))

    @property
    def ws(self) -> UUID:
        return self.workspace_id

    @property
    def repo(self) -> UUID:
        return self.repository_id


class SourceRef(StrictModel):
    """Location reference pointing to a file and optional fact in a snapshot."""

    repository_id: UUID = Field(validation_alias=AliasChoices("repository_id", "repo"))
    snapshot_id: str = Field(
        validation_alias=AliasChoices("snapshot_id", "snap"),
        pattern=r"^[0-9a-f]{64}$",
    )
    path: str
    fact_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_manifest_path(v)

    @property
    def repo(self) -> UUID:
        return self.repository_id

    @property
    def snap(self) -> str:
        return self.snapshot_id


class ManifestFile(StrictModel):
    """File entry within a code snapshot manifest."""

    path: str
    kind: FileKind
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_manifest_path(v)

    @model_validator(mode="after")
    def _validate_deleted_or_present(self) -> ManifestFile:
        if self.kind == "deleted":
            if self.sha256 is not None or self.size is not None:
                raise ValueError("deleted file must have sha256=None and size=None")
        else:
            if self.sha256 is None or self.size is None:
                raise ValueError(f"{self.kind} file must have both sha256 and size")
        return self


class CodeSnapshotManifest(StrictModel):
    """Manifest of files comprising an isolated code snapshot."""

    workspace_id: UUID = Field(validation_alias=AliasChoices("workspace_id", "ws"))
    repository_id: UUID = Field(validation_alias=AliasChoices("repository_id", "repo"))
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    files: tuple[ManifestFile, ...]

    @field_validator("files", mode="before")
    @classmethod
    def _validate_and_sort_files(
        cls, v: Any
    ) -> tuple[ManifestFile | dict[str, Any], ...]:
        if not isinstance(v, (list, tuple)):
            raise TypeError(f"files must be a list or tuple, got {type(v).__name__}")
        paths: list[str] = []
        for item in v:
            p = item.path if isinstance(item, ManifestFile) else item["path"]
            paths.append(p)
        if len(paths) != len(set(paths)):
            raise ValueError("files must have unique paths")
        return tuple(
            sorted(
                v,
                key=lambda item: (
                    item.path.encode("utf-8")
                    if isinstance(item, ManifestFile)
                    else item["path"].encode("utf-8")
                ),
            )
        )

    @property
    def ws(self) -> UUID:
        return self.workspace_id

    @property
    def repo(self) -> UUID:
        return self.repository_id

    def snapshot_id(self) -> str:
        payload = {
            "algorithm": CODE_SNAPSHOT_ALGORITHM,
            "base_commit": self.base_commit,
            "files": [f.model_dump(mode="json") for f in self.files],
            "repo": str(self.repository_id),
            "ws": str(self.workspace_id),
        }
        return sha256(payload)


class EnolaFact(StrictModel):
    """Enola code AST symbol observation."""

    fact_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: str = Field(min_length=1)
    name: str = Field(validation_alias=AliasChoices("name", "fact_name"), min_length=1)
    file: str
    line: int = Field(ge=1)

    @field_validator("file")
    @classmethod
    def _validate_file(cls, v: str) -> str:
        return validate_manifest_path(v)

    def child_fact_id(self, snap: str) -> str:
        if not isinstance(snap, str) or not _HEX64_PATTERN.fullmatch(snap):
            raise ValueError(
                f"snap must be a 64-char lowercase hex string, got {snap!r}"
            )
        payload = {
            "algorithm": CHILD_FACT_ALGORITHM,
            "fact_id": self.fact_id,
            "file": self.file,
            "snap": snap,
        }
        return sha256(payload)


class IngestionJob(StrictModel):
    """Asynchronous ingestion work item tracked across states."""

    job_id: UUID
    workspace_id: UUID = Field(validation_alias=AliasChoices("workspace_id", "ws"))
    repository_id: UUID = Field(validation_alias=AliasChoices("repository_id", "repo"))
    snapshot_id: str = Field(
        validation_alias=AliasChoices("snapshot_id", "snap"),
        pattern=r"^[0-9a-f]{64}$",
    )
    idempotency_key: str = Field(min_length=1)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: JobState
    error_code: str | None = None

    @model_validator(mode="after")
    def _validate_error_code(self) -> IngestionJob:
        if self.state == "failed":
            if not self.error_code:
                raise ValueError("error_code must be provided when state is 'failed'")
        else:
            if self.error_code is not None:
                raise ValueError(
                    f"error_code must be None when state is {self.state!r}"
                )
        return self

    @property
    def ws(self) -> UUID:
        return self.workspace_id

    @property
    def repo(self) -> UUID:
        return self.repository_id

    @property
    def snap(self) -> str:
        return self.snapshot_id


class IngestionReceipt(StrictModel):
    """Receipt proving knowledge extraction completion."""

    receipt_id: UUID
    job_id: UUID
    snapshot_id: str = Field(
        validation_alias=AliasChoices("snapshot_id", "snap"),
        pattern=r"^[0-9a-f]{64}$",
    )
    extraction_state: ExtractionState
    fact_count: int = Field(ge=0)
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_extraction_state(self) -> IngestionReceipt:
        if self.fact_count == 0:
            if self.extraction_state != "no_lesson":
                raise ValueError("fact_count 0 requires extraction_state='no_lesson'")
        else:
            if self.extraction_state != "extracted":
                raise ValueError(
                    f"fact_count {self.fact_count} requires extraction_state='extracted', got {self.extraction_state!r}"
                )
        return self

    @property
    def snap(self) -> str:
        return self.snapshot_id


class KnowledgeQuery(StrictModel):
    """Semantic search query across a repository snapshot."""

    workspace_id: UUID = Field(validation_alias=AliasChoices("workspace_id", "ws"))
    repository_id: UUID = Field(validation_alias=AliasChoices("repository_id", "repo"))
    snapshot_id: str = Field(
        validation_alias=AliasChoices("snapshot_id", "snap"),
        pattern=r"^[0-9a-f]{64}$",
    )
    text: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=50)

    @property
    def ws(self) -> UUID:
        return self.workspace_id

    @property
    def repo(self) -> UUID:
        return self.repository_id

    @property
    def snap(self) -> str:
        return self.snapshot_id


class KnowledgeHit(StrictModel):
    """Search hit returned by a knowledge provider."""

    child_fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source: SourceRef
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    corrected: bool = False


class ProviderRoute(StrictModel):
    """Routing descriptor for a knowledge provider and supported operations."""

    provider: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    operations: tuple[OperationKind, ...] = ()

    @field_validator("operations", mode="before")
    @classmethod
    def _validate_operations(cls, v: Any) -> tuple[OperationKind, ...]:
        if isinstance(v, (list, tuple, set, frozenset)):
            ops = list(v)
        else:
            raise TypeError(f"operations must be an iterable, got {type(v).__name__}")
        for op in ops:
            if op not in ALLOWED_OPERATIONS:
                raise ValueError(
                    f"unsupported operation {op!r}; must be subset of {sorted(ALLOWED_OPERATIONS)}"
                )
        return tuple(sorted(set(ops)))


__all__ = [
    "CHILD_FACT_ALGORITHM",
    "CODE_SNAPSHOT_ALGORITHM",
    "CodeSnapshotManifest",
    "EnolaFact",
    "IngestionJob",
    "IngestionReceipt",
    "KnowledgeHit",
    "KnowledgeQuery",
    "ManifestFile",
    "ProviderRoute",
    "RepositoryIdentity",
    "SourceRef",
    "validate_manifest_path",
]
