from __future__ import annotations

import re
import shutil
from datetime import datetime
from enum import StrEnum
from hashlib import sha256 as bytes_sha256
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omp_work.operations.artifacts import (
    decrypt_file,
    read_json_artifact,
    resolve_artifact_path,
)
from omp_work.operations.config import OperationsConfig
from omp_work.v1.canonical import sha256
from omp_work.v1.models import Anomaly, ReconciliationCounts, ReconciliationHashes

DIMENSIONS = (
    "worlds",
    "surfaces",
    "promises",
    "work_items",
    "states",
    "labels",
    "relations",
    "comments",
    "attachments",
    "users",
)
WORKFLOW_PREFIXES = (
    "**Plan approved**",
    "**Execution handoff**",
    "**Session review**",
    "**Close proposed**",
    "**Owner verdict in session:",
)
SCHEMA_VERSION = "linear-export/v1"


class LinearStream(StrEnum):
    teams = "teams"
    initiatives = "initiatives"
    projects = "projects"
    project_updates = "projectUpdates"
    milestones = "projectMilestones"
    issues = "issues"
    states = "workflowStates"
    labels = "issueLabels"
    initiative_projects = "initiativeToProjects"
    relations = "issueRelations"
    comments = "comments"
    attachments = "attachments"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePage(_Strict):
    schema_version: str = SCHEMA_VERSION
    export_id: UUID
    stream: str
    page_index: int = Field(ge=0)
    request_cursor: str | None = None
    end_cursor: str | None = None
    has_next_page: bool
    nodes: tuple[dict[str, Any], ...]


class ArtifactRecord(_Strict):
    path: str
    plaintext_sha256: str
    ciphertext_sha256: str
    variables_sha256: str | None = None
    stream: str | None = None
    page_index: int | None = None
    request_cursor: str | None = None
    end_cursor: str | None = None
    has_next_page: bool | None = None


class CursorRecord(_Strict):
    page_index: int
    request_cursor: str | None
    end_cursor: str | None
    has_next_page: bool
    scanned_count: int
    retained_count: int
    cumulative_count: int
    plaintext_sha256: str
    ciphertext_sha256: str
    artifact_path: str
    variables_sha256: str


class ExportRun(_Strict):
    export_id: UUID
    workspace_id: UUID
    mode: str
    base_export_id: UUID | None
    state: str
    source_started_at: datetime
    source_lower_bound: datetime | None
    source_boundary: datetime | None
    storage_root: str


class StreamSummary(_Strict):
    scanned: int = 0
    retained: int = 0
    excluded: int = 0


class AttachmentDisposition(_Strict):
    metadata_only: int = 0
    former_metadata: int = 0
    quarantined: int = 0


class ScopeReport(_Strict):
    streams: dict[str, StreamSummary]


class PrivacyReport(_Strict):
    credential_kind: str = "oauth"
    credential_scopes: tuple[str, ...] = ("read",)
    staging_cleanup: bool
    forbidden_fields_removed: bool
    body_fields_encrypted: bool = True


class SourceHashEntry(_Strict):
    id: str
    key: str | None = None
    owner_id: str | None = None
    updated_at: datetime | None = None
    source_sha256: str | None = None
    record_sha256: str
    artifact_ref: str


class SourceHashIndex(_Strict):
    worlds: dict[str, SourceHashEntry] = Field(default_factory=dict)
    surfaces: dict[str, SourceHashEntry] = Field(default_factory=dict)
    promises: dict[str, SourceHashEntry] = Field(default_factory=dict)
    work_items: dict[str, SourceHashEntry] = Field(default_factory=dict)
    states: dict[str, SourceHashEntry] = Field(default_factory=dict)
    labels: dict[str, SourceHashEntry] = Field(default_factory=dict)
    relations: dict[str, SourceHashEntry] = Field(default_factory=dict)
    comments: dict[str, SourceHashEntry] = Field(default_factory=dict)
    attachments: dict[str, SourceHashEntry] = Field(default_factory=dict)
    users: dict[str, SourceHashEntry] = Field(default_factory=dict)
    project_updates: dict[str, SourceHashEntry] = Field(default_factory=dict)


class ExportManifest(_Strict):
    export_id: UUID
    workspace_id: UUID
    mode: str
    base_export_id: UUID | None = None
    source_started_at: datetime
    source_lower_bound: datetime | None = None
    source_boundary: datetime
    source_watermark: datetime | None = None
    source_hashes: SourceHashIndex
    dimension_counts: ReconciliationCounts
    dimension_hashes: ReconciliationHashes
    raw_export_sha256: str
    manifest_sha256: str = ""
    artifacts: dict[str, ArtifactRecord]
    scope_report: ScopeReport
    privacy_report: PrivacyReport
    attachment_dispositions: AttachmentDisposition
    anomalies: tuple[Anomaly, ...] = ()


def page_sort_key(item: tuple[str, ArtifactRecord]) -> tuple[int, int, int]:
    key, _ = item
    phase, stream, index = key.rsplit(":", 2)
    return (
        ("baseline", "overlap", "delta").index(phase),
        list(LinearStream).index(LinearStream(stream)),
        int(index),
    )


def load_manifest(config: OperationsConfig, export_id: UUID) -> ExportManifest:
    matches = list(
        (config.data_dir / "linear-exports").glob(f"*/{export_id}/manifest-*.json.gpg")
    )
    if len(matches) != 1:
        raise ValueError("linear_manifest_missing")
    encrypted = matches[0]
    if encrypted.stat().st_mode & 0o777 != 0o400:
        raise RuntimeError("pagination_count_hash_gap")
    staging = config.state_dir / "staging" / str(export_id)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    try:
        plain = staging / "manifest.json"
        decrypt_file(encrypted, plain, config.secret_path("gpg-passphrase"))
        try:
            manifest = ExportManifest.model_validate_json(
                plain.read_text(encoding="utf-8")
            )
        except Exception:
            raise RuntimeError("pagination_count_hash_gap") from None
        if (
            sha256(manifest.model_dump(mode="json", exclude={"manifest_sha256"}))
            != manifest.manifest_sha256
        ):
            raise RuntimeError("pagination_count_hash_gap")
        artifact = ArtifactRecord(
            path=str(encrypted.relative_to(config.data_dir)),
            plaintext_sha256=sha256(manifest.model_dump(mode="json")),
            ciphertext_sha256=bytes_sha256(encrypted.read_bytes()).hexdigest(),
        )
        return manifest.model_copy(
            update={"artifacts": {**manifest.artifacts, "manifest": artifact}}
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_export(
    config: OperationsConfig, export_id: UUID
) -> tuple[ExportManifest, SourceHashIndex, tuple[SourcePage, ...]]:
    manifest = load_manifest(config, export_id)
    artifact = manifest.artifacts.get("source-hashes")
    if artifact is None:
        raise RuntimeError("pagination_count_hash_gap")
    staging = config.state_dir / "staging" / f"load-{export_id}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o700, parents=True)
    try:
        source_hashes_path = resolve_artifact_path(artifact.path, config.data_dir)
        payload = read_json_artifact(
            source_hashes_path,
            staging / "source-hashes.json",
            config.secret_path("gpg-passphrase"),
            expected_plaintext_sha256=artifact.plaintext_sha256,
            expected_ciphertext_sha256=artifact.ciphertext_sha256,
            data_dir=config.data_dir,
            decrypt_fn=decrypt_file,
        )
        try:
            source_hashes = SourceHashIndex.model_validate(payload)
        except Exception:
            raise RuntimeError("pagination_count_hash_gap") from None
        if source_hashes != manifest.source_hashes:
            raise RuntimeError("pagination_count_hash_gap")

        page_items = [
            (key, art)
            for key, art in manifest.artifacts.items()
            if re.fullmatch(r"(?:baseline|overlap|delta):[^:]+:\d+", key)
        ]
        page_items.sort(key=page_sort_key)
        pages: list[SourcePage] = []
        for idx, (key, page_artifact) in enumerate(page_items):
            page_path = resolve_artifact_path(page_artifact.path, config.data_dir)
            page_payload = read_json_artifact(
                page_path,
                staging / f"page-{idx}.json",
                config.secret_path("gpg-passphrase"),
                expected_plaintext_sha256=page_artifact.plaintext_sha256,
                expected_ciphertext_sha256=page_artifact.ciphertext_sha256,
                data_dir=config.data_dir,
                decrypt_fn=decrypt_file,
            )
            try:
                page = SourcePage.model_validate(page_payload)
                _, stream_name, page_index = key.rsplit(":", 2)
                LinearStream(stream_name)
            except Exception:
                raise RuntimeError("pagination_count_hash_gap") from None
            if (
                page.export_id != export_id
                or page.stream != key.rsplit(":", 1)[0]
                or page.page_index != int(page_index)
            ):
                raise RuntimeError("pagination_count_hash_gap")
            pages.append(page)
        return manifest, source_hashes, tuple(pages)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
