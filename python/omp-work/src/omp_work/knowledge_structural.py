"""Vendor-neutral structural extraction from Enola artifacts (FK-4).

This module turns two Enola contract artifacts (``facts.jsonl`` and
``receipt.json``) plus the snapshot's file manifest into a
:class:`StructuralProjection` and a :class:`CoverageManifest`. It imports no
engine, database, model or Cognee package, so extraction runs and is testable
with no knowledge engine installed. ``insights.json`` is not consumed: it
carries explainer prose, not structural facts.

Structural input format (for an alternative engine)
---------------------------------------------------
An engine that is not Cognee consumes the canonical JSON of
``StructuralProjection.model_dump(mode="json")``:

- ``namespace`` — the snapshot namespace label from
  :func:`omp_work.knowledge_namespace.snapshot_namespace`.
- ``nodes[]`` — one entry per distinct Enola fact identity. The identity is the
  full ``(repository, enola_repository, kind, name, file)`` tuple: two files that
  declare a symbol of the same name are two facts and never collapse. Each node
  carries ``structural_fact_id`` (canonical 64-hex), ``node_id`` (the
  namespace-bound UUID), ``kind``, ``name``, ``file``, ``line``,
  ``properties``, the complete ``observations`` lineage in raw-file order, and
  ``provenance``.
- ``edges[]`` — ``source_fact_id``, ``target_fact_id`` and ``kind``.
- ``provenance`` — repository identity, snapshot id, and the source / config /
  processing versions every published fact carries.
- ``graph_sha256`` — canonical hash of the node and edge sets, so any engine can
  verify it projected the same graph.

The per-snapshot coverage manifest is a separate value of the same module
(:class:`CoverageManifest`), built by :func:`build_coverage_manifest`.

Raw observations stay separate from the projected node: one stable Enola id may
legitimately be observed several times, and every raw observation is retained
rather than collapsed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from pydantic import Field, ValidationError, field_validator

from .knowledge_contracts import ManifestFile, validate_manifest_path
from .knowledge_namespace import (
    fact_node_id,
    snapshot_namespace,
    structural_fact_id,
    validate_snapshot_id,
)
from .v1.canonical import sha256
from .v1.models import StrictModel

STRUCTURAL_PROJECTION_VERSION = "structural-projection/v1"
STRUCTURAL_GRAPH_ALGORITHM = "work.omp.dev/v1/structural-graph"
# Only a version this code can actually produce may be stamped onto a snapshot:
# the applied projector version is recorded per fact, so an unvalidated caller
# string would let a snapshot claim a projection the code never performed.
SUPPORTED_PROJECTION_VERSIONS = frozenset({STRUCTURAL_PROJECTION_VERSION})

_SKIPPED_SAMPLE_PATTERN = re.compile(r"^(?P<path>.+?) \((?P<reason>.+)\)$")


class EnolaStructuralError(Exception):
    """Raised when an Enola artifact cannot be projected into a code graph."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def validate_projection_version(processing_version: str) -> str:
    """Reject a projector version this code cannot actually have applied."""
    if processing_version not in SUPPORTED_PROJECTION_VERSIONS:
        raise EnolaStructuralError(
            "unsupported_projection_version",
            f"unsupported structural projection version {processing_version!r}; "
            f"supported: {sorted(SUPPORTED_PROJECTION_VERSIONS)}",
        )
    return processing_version


class DuplicateFactIdentityError(EnolaStructuralError):
    """One Enola id was observed against two different fact identities."""

    def __init__(self, raw_fact_id: str, message: str) -> None:
        super().__init__("duplicate_fact_identity", message)
        self.raw_fact_id = raw_fact_id


class StructuralRelation(StrictModel):
    kind: str = Field(min_length=1)
    target_name: str | None = None
    target_raw_fact_id: str | None = None
    target_fact_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class StructuralObservation(StrictModel):
    """One raw ``facts.jsonl`` row, retained in file order."""

    raw_index: int = Field(ge=0)
    raw_fact_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    file: str
    enola_repository: str | None = None
    line: int | None = Field(default=None, ge=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    relations: tuple[StructuralRelation, ...] = ()

    @field_validator("file")
    @classmethod
    def _validate_file(cls, v: str) -> str:
        return validate_manifest_path(v)


class FactProvenance(StrictModel):
    """Identity and version provenance carried by every projected fact."""

    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version: str = Field(min_length=1)
    config_version: str | None = None
    processing_version: str = Field(min_length=1)


class StructuralNode(StrictModel):
    structural_fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    name: str = Field(min_length=1)
    file: str
    enola_repository: str | None = None
    line: int | None = Field(default=None, ge=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    observations: tuple[StructuralObservation, ...]
    provenance: FactProvenance

    @field_validator("file")
    @classmethod
    def _validate_file(cls, v: str) -> str:
        return validate_manifest_path(v)


class StructuralEdge(StrictModel):
    source_fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_fact_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: str = Field(min_length=1)


class StructuralProjection(StrictModel):
    """The canonical structural input an engine consumes for one snapshot."""

    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    namespace: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    config_version: str | None = None
    processing_version: str = Field(min_length=1)
    nodes: tuple[StructuralNode, ...]
    edges: tuple[StructuralEdge, ...]
    raw_fact_count: int = Field(ge=0)
    graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReceiptInfo(StrictModel):
    """The receipt fields extraction needs, independent of any engine."""

    enola_version: str = Field(min_length=1)
    extractor_version: str | None = None
    config_hash: str | None = None
    quality: dict[str, Any] = Field(default_factory=dict)


def parse_enola_receipt(receipt_bytes: bytes | None) -> ReceiptInfo:
    if not receipt_bytes:
        raise EnolaStructuralError("missing_receipt", "receipt.json is missing")
    try:
        data = json.loads(receipt_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EnolaStructuralError(
            "corrupt_receipt", f"receipt.json is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise EnolaStructuralError("corrupt_receipt", "receipt.json must be an object")
    version = data.get("enola_version", data.get("version"))
    if not isinstance(version, str) or not version:
        raise EnolaStructuralError(
            "corrupt_receipt", "receipt.json has no enola_version"
        )
    config_hash = data.get("config_hash")
    if isinstance(config_hash, str):
        config_hash = config_hash.removeprefix("sha256:")
    quality = data.get("quality")
    return ReceiptInfo(
        enola_version=version,
        extractor_version=data.get("extractor_version"),
        config_hash=config_hash,
        quality=quality if isinstance(quality, dict) else {},
    )


def _relation_parts(value: Any) -> tuple[str, str | None, str | None] | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("kind") or value.get("relationship")
    if not isinstance(kind, str) or not kind:
        return None
    target_name = value.get("target")
    target_id = value.get("target_id")
    return (
        kind,
        target_name if isinstance(target_name, str) and target_name else None,
        target_id if isinstance(target_id, str) and target_id else None,
    )


def _parse_fact_row(row: Any, index: int) -> StructuralObservation:
    if not isinstance(row, dict):
        raise EnolaStructuralError(
            "corrupt_facts", f"facts.jsonl row {index + 1} is not a JSON object"
        )

    def required(field: str) -> str:
        value = row.get(field)
        if not isinstance(value, str) or not value:
            raise EnolaStructuralError(
                "corrupt_facts",
                f"facts.jsonl row {index + 1} is missing required field {field!r}",
            )
        return value

    properties = row.get("props")
    relations = [
        relation
        for relation in (_relation_parts(raw) for raw in (row.get("relations") or ()))
        if relation is not None
    ]
    line = row.get("line")
    enola_repository = row.get("repo")
    try:
        return StructuralObservation(
            raw_index=index,
            raw_fact_id=required("id"),
            kind=required("kind"),
            name=required("name"),
            file=required("file"),
            enola_repository=(
                enola_repository
                if isinstance(enola_repository, str) and enola_repository
                else None
            ),
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
            properties=dict(properties) if isinstance(properties, dict) else {},
            relations=tuple(
                StructuralRelation(
                    kind=kind, target_name=target, target_raw_fact_id=target_id
                )
                for kind, target, target_id in relations
            ),
        )
    except ValidationError as exc:
        raise EnolaStructuralError(
            "corrupt_facts", f"facts.jsonl row {index + 1} is invalid: {exc}"
        ) from exc


def parse_enola_facts(facts_bytes: bytes) -> tuple[StructuralObservation, ...]:
    """Parse ``facts.jsonl`` preserving raw order and duplicate observations."""
    if not facts_bytes:
        raise EnolaStructuralError("corrupt_facts", "facts.jsonl is empty")
    observations: list[StructuralObservation] = []
    for index, raw_line in enumerate(facts_bytes.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EnolaStructuralError(
                "corrupt_facts",
                f"facts.jsonl line {index + 1} is not valid JSON: {exc}",
            ) from exc
        observations.append(_parse_fact_row(row, index))
    return tuple(observations)


class _Index:
    """Raw-fact-id and name lookups for relation resolution."""

    def __init__(
        self,
        observations: Iterable[StructuralObservation],
        *,
        repository_id: UUID,
    ) -> None:
        self.repository_id = repository_id
        self.raw_id_to_identity: dict[str, str] = {}
        self.identity_to_observations: dict[str, list[StructuralObservation]] = {}
        self.identity_order: list[str] = []
        self.name_to_identities: dict[str, list[str]] = {}
        for observation in observations:
            identity = structural_fact_id(
                repository_id=self.repository_id,
                enola_repo=observation.enola_repository,
                kind=observation.kind,
                name=observation.name,
                file=observation.file,
            )
            previous = self.raw_id_to_identity.get(observation.raw_fact_id)
            if previous is not None and previous != identity:
                raise DuplicateFactIdentityError(
                    observation.raw_fact_id,
                    f"Enola id {observation.raw_fact_id!r} maps to conflicting "
                    f"identities ({previous} and {identity})",
                )
            self.raw_id_to_identity[observation.raw_fact_id] = identity
            group = self.identity_to_observations.get(identity)
            if group is None:
                self.identity_to_observations[identity] = [observation]
                self.identity_order.append(identity)
            else:
                group.append(observation)

    def resolve(self, relation: StructuralRelation) -> str | None:
        if relation.target_raw_fact_id:
            resolved = self.raw_id_to_identity.get(relation.target_raw_fact_id)
            if resolved is not None:
                return resolved
        if relation.target_name:
            matches = self.name_to_identities.get(relation.target_name, [])
            if len(matches) == 1:
                return matches[0]
        return None

    def index_names(self) -> None:
        for identity in self.identity_order:
            observation = self.identity_to_observations[identity][0]
            self.name_to_identities.setdefault(observation.name, []).append(identity)


def project_structural_snapshot(
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    facts_bytes: bytes,
    receipt_bytes: bytes | None,
    processing_version: str = STRUCTURAL_PROJECTION_VERSION,
) -> StructuralProjection:
    """Project Enola facts into a namespaced structural code graph.

    Two observations share a node only when their full identity
    ``(repo, enola_repository, kind, name, file)`` agrees; a file difference
    yields a distinct ``structural_fact_id`` and therefore a distinct node.
    """
    validate_snapshot_id(snapshot_id)
    validate_projection_version(processing_version)
    observations = parse_enola_facts(facts_bytes)
    receipt = parse_enola_receipt(receipt_bytes)
    namespace = snapshot_namespace(
        workspace_id=workspace_id, repository_id=repository_id, snapshot_id=snapshot_id
    )
    provenance = FactProvenance(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        source_version=receipt.enola_version,
        config_version=receipt.config_hash,
        processing_version=processing_version,
    )

    index = _Index(observations, repository_id=repository_id)
    index.index_names()

    nodes: list[StructuralNode] = []
    for identity in index.identity_order:
        group = index.identity_to_observations[identity]
        first = group[0]
        nodes.append(
            StructuralNode(
                structural_fact_id=identity,
                namespace=namespace,
                node_id=str(
                    fact_node_id(namespace=namespace, structural_fact_id=identity)
                ),
                kind=first.kind,
                name=first.name,
                file=first.file,
                enola_repository=first.enola_repository,
                line=first.line,
                properties=dict(first.properties),
                observations=tuple(group),
                provenance=provenance,
            )
        )
    nodes.sort(key=lambda node: node.structural_fact_id.encode("ascii"))

    edge_keys: set[tuple[str, str, str]] = set()
    for node in nodes:
        for observation in node.observations:
            for relation in observation.relations:
                target = index.resolve(relation)
                if target is not None:
                    edge_keys.add((node.structural_fact_id, target, relation.kind))
    edges = tuple(
        StructuralEdge(source_fact_id=source, target_fact_id=target, kind=kind)
        for source, target, kind in sorted(
            edge_keys, key=lambda key: tuple(part.encode("utf-8") for part in key)
        )
    )

    graph_sha256 = sha256(
        {
            "algorithm": STRUCTURAL_GRAPH_ALGORITHM,
            "edges": [[e.source_fact_id, e.target_fact_id, e.kind] for e in edges],
            "nodes": [n.structural_fact_id for n in nodes],
        }
    )

    return StructuralProjection(
        workspace_id=workspace_id,
        repository_id=repository_id,
        snapshot_id=snapshot_id,
        namespace=namespace,
        source_version=receipt.enola_version,
        config_version=receipt.config_hash,
        processing_version=processing_version,
        nodes=tuple(nodes),
        edges=edges,
        raw_fact_count=len(observations),
        graph_sha256=graph_sha256,
    )


class CoverageEntry(StrictModel):
    path: str
    state: str = Field(pattern=r"^(extracted|skipped)$")
    reason: str = Field(min_length=1)
    fact_count: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return validate_manifest_path(v)


class CoverageManifest(StrictModel):
    """Per-snapshot record of extracted and skipped files with reasons."""

    workspace_id: UUID
    repository_id: UUID
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_version: str = Field(min_length=1)
    config_version: str | None = None
    processing_version: str = Field(min_length=1)
    files: tuple[CoverageEntry, ...]
    extracted_files: int = Field(ge=0)
    skipped_files: int = Field(ge=0)
    raw_fact_count: int = Field(ge=0)

    def skipped(self) -> tuple[CoverageEntry, ...]:
        return tuple(entry for entry in self.files if entry.state == "skipped")


def _skip_reason_hints(quality: Mapping[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    sample = quality.get("skipped_sample")
    if isinstance(sample, list):
        for item in sample:
            if not isinstance(item, str):
                continue
            match = _SKIPPED_SAMPLE_PATTERN.match(item)
            if match is None:
                continue
            path = match.group("path").rstrip("/")
            if path:
                hints[path] = f"excluded: {match.group('reason')}"
    return hints


def _excluded_kind_hint(quality: Mapping[str, Any], path: str) -> str | None:
    census = quality.get("census")
    excluded = census.get("excluded_kinds") if isinstance(census, dict) else None
    if not isinstance(excluded, dict):
        return None
    for extension in excluded:
        if isinstance(extension, str) and extension and path.endswith(extension):
            return f"excluded_kind:{extension}"
    return None


def build_coverage_manifest(
    projection: StructuralProjection,
    *,
    manifest_files: Iterable[ManifestFile],
    quality: Mapping[str, Any] | None = None,
) -> CoverageManifest:
    """List every snapshot file as extracted or skipped, with its reason."""
    quality = quality or {}
    fact_counts: dict[str, int] = {}
    for node in projection.nodes:
        fact_counts[node.file] = fact_counts.get(node.file, 0) + 1

    hints = _skip_reason_hints(quality)
    entries: dict[str, CoverageEntry] = {}
    for entry in manifest_files:
        validate_manifest_path(entry.path)
        if entry.kind == "deleted":
            entries[entry.path] = CoverageEntry(
                path=entry.path, state="skipped", reason="deleted", fact_count=0
            )
        elif entry.path in fact_counts:
            entries[entry.path] = CoverageEntry(
                path=entry.path,
                state="extracted",
                reason="extracted",
                fact_count=fact_counts[entry.path],
            )
        else:
            reason = (
                hints.get(entry.path)
                or _excluded_kind_hint(quality, entry.path)
                or "no_facts_emitted"
            )
            entries[entry.path] = CoverageEntry(
                path=entry.path, state="skipped", reason=reason, fact_count=0
            )

    for path, count in fact_counts.items():
        if path not in entries:
            entries[path] = CoverageEntry(
                path=path, state="extracted", reason="extracted", fact_count=count
            )

    ordered = tuple(
        sorted(entries.values(), key=lambda entry: entry.path.encode("utf-8"))
    )
    return CoverageManifest(
        workspace_id=projection.workspace_id,
        repository_id=projection.repository_id,
        snapshot_id=projection.snapshot_id,
        source_version=projection.source_version,
        config_version=projection.config_version,
        processing_version=projection.processing_version,
        files=ordered,
        extracted_files=sum(1 for entry in ordered if entry.state == "extracted"),
        skipped_files=sum(1 for entry in ordered if entry.state == "skipped"),
        raw_fact_count=projection.raw_fact_count,
    )


__all__ = [
    "STRUCTURAL_GRAPH_ALGORITHM",
    "STRUCTURAL_PROJECTION_VERSION",
    "SUPPORTED_PROJECTION_VERSIONS",
    "CoverageEntry",
    "CoverageManifest",
    "DuplicateFactIdentityError",
    "EnolaStructuralError",
    "FactProvenance",
    "ReceiptInfo",
    "StructuralEdge",
    "StructuralNode",
    "StructuralObservation",
    "StructuralProjection",
    "StructuralRelation",
    "build_coverage_manifest",
    "parse_enola_facts",
    "parse_enola_receipt",
    "project_structural_snapshot",
    "validate_projection_version",
]
