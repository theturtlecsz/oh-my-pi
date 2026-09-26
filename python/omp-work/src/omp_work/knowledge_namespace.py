"""Repository and snapshot namespace adapter for Enola structural facts.

The complete Enola fact identity is ``(repository, enola_repo, kind, name,
file)``: the file is part of it, so two files that declare a symbol of the same
name are two facts and never collapse into one. The snapshot id is the
namespace: two candidates of one repository occupy disjoint namespaces, so
publishing one candidate cannot rewrite another candidate's structural query
results.

Nothing in this module imports an engine, a database or a model.
"""

from __future__ import annotations

import re
from uuid import NAMESPACE_OID, UUID, uuid5

from .v1.canonical import sha256

FACT_IDENTITY_ALGORITHM = "work.omp.dev/v1/structural-fact"
NAMESPACE_ALGORITHM = "work.omp.dev/v1/snapshot-namespace"

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def validate_snapshot_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or not _HEX64.fullmatch(snapshot_id):
        raise ValueError(
            f"snapshot_id must be a 64-char lowercase hex string, got {snapshot_id!r}"
        )
    return snapshot_id


def validate_structural_fact_id(structural_fact_id: str) -> str:
    if not isinstance(structural_fact_id, str) or not _HEX64.fullmatch(
        structural_fact_id
    ):
        raise ValueError(
            "structural_fact_id must be a 64-char lowercase hex string, got "
            f"{structural_fact_id!r}"
        )
    return structural_fact_id


def fact_identity_string(
    *,
    repository_id: UUID | str,
    enola_repo: str | None,
    kind: str,
    name: str,
    file: str,
) -> str:
    """The human-readable ``repo\\0enola_repo\\0kind\\0name\\0file`` identity.

    It is a debugging and test surface: :func:`structural_fact_id` hashes the
    same parts canonically.
    """
    for field, value in (("kind", kind), ("name", name), ("file", file)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} is required for a structural fact identity")
    return "\x00".join([str(repository_id), enola_repo or "", kind, name, file])


def structural_fact_id(
    *,
    repository_id: UUID | str,
    enola_repo: str | None,
    kind: str,
    name: str,
    file: str,
) -> str:
    """Canonical identity hash for one Enola structural fact.

    Two observations collide only when repository, Enola repository label,
    kind, name and file all agree. A differing file yields a different id, so
    same-name facts across files stay distinct.
    """
    for field, value in (("kind", kind), ("name", name), ("file", file)):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} is required for a structural fact identity")
    return sha256(
        {
            "algorithm": FACT_IDENTITY_ALGORITHM,
            "enola_repo": enola_repo or "",
            "file": file,
            "kind": kind,
            "name": name,
            "repo": str(repository_id),
        }
    )


def snapshot_namespace(
    *, workspace_id: UUID | str, repository_id: UUID | str, snapshot_id: str
) -> str:
    """Namespace label isolating one candidate snapshot from every other."""
    validate_snapshot_id(snapshot_id)
    return f"{workspace_id}:{repository_id}@{snapshot_id}"


def namespace_digest(*, namespace: str, structural_fact_id: str) -> str:
    """Canonical digest binding one structural fact to its snapshot namespace."""
    if not namespace:
        raise ValueError("namespace is required")
    validate_structural_fact_id(structural_fact_id)
    return sha256(
        {
            "algorithm": NAMESPACE_ALGORITHM,
            "fact": structural_fact_id,
            "namespace": namespace,
        }
    )


def fact_node_id(*, namespace: str, structural_fact_id: str) -> UUID:
    """Deterministic node UUID for a structural fact under one namespace."""
    validate_structural_fact_id(structural_fact_id)
    if not namespace:
        raise ValueError("namespace is required")
    key = f"omp-fact:{namespace}:{structural_fact_id}"
    return uuid5(NAMESPACE_OID, key)


__all__ = [
    "FACT_IDENTITY_ALGORITHM",
    "NAMESPACE_ALGORITHM",
    "fact_identity_string",
    "fact_node_id",
    "namespace_digest",
    "snapshot_namespace",
    "structural_fact_id",
    "validate_snapshot_id",
    "validate_structural_fact_id",
]
