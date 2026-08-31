from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import CommandEnvelope
CANDIDATE_HASH_ALGORITHM = "work.omp.dev/v1/candidate-sha256"
_COMMIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def text_sha256(text: str) -> str:
    """Plain UTF-8 byte hash for rendered event text and sealed task bodies —
    NOT canonical-JSON: TS and Python must hash the exact same bytes."""
    return hashlib.sha256(text.encode()).hexdigest()


def candidate_sha256(commit_sha: str, paths: Iterable[str]) -> str:
    """Canonical candidate hash, pinned by decision 0004 and contracts/v1/candidate-hash.json.

    Paths are the commit's complete file list (`git diff-tree --no-commit-id --name-only -r`),
    hashed exactly as stored — no Unicode normalization, since a Git tree may legally contain
    both NFC and NFD spellings of the same displayed name as distinct entries.
    """
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError(
            "commit_sha must be a full lowercase hex object id (40 or 64 chars)"
        )
    ordered = sorted(paths, key=lambda path: path.encode("utf-8"))
    if not ordered:
        raise ValueError("candidate path set must not be empty")
    previous: str | None = None
    for path in ordered:
        if (
            not path
            or path.startswith("./")
            or path.endswith("/")
            or "\\" in path
            or "//" in path
        ):
            raise ValueError(
                f"candidate path is not a canonical repo-relative file path: {path!r}"
            )
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
            raise ValueError(f"candidate path contains control characters: {path!r}")
        if path == previous:
            raise ValueError(f"duplicate candidate path: {path!r}")
        previous = path
    return sha256(
        {
            "algorithm": CANDIDATE_HASH_ALGORITHM,
            "commit_sha": commit_sha,
            "paths": ordered,
        }
    )


def validate_execution_path(path: str) -> None:
    if not path or not isinstance(path, str) or path.strip() != path:
        raise ValueError(f"invalid plan path: {path!r}")
    if (
        path.startswith("/")
        or path.startswith("\\")
        or (len(path) >= 2 and path[1] == ":" and path[0].isalpha())
    ):
        raise ValueError(f"plan path must be repository-relative: {path!r}")
    if path.startswith("./") or path.endswith("/") or "\\" in path or "//" in path:
        raise ValueError(f"plan path must use POSIX relative separators: {path!r}")
    segments = path.split("/")
    if any(s in (".", "..", "") for s in segments):
        raise ValueError(f"plan path must not contain . or .. segments: {path!r}")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ValueError(f"plan path contains control characters: {path!r}")
    if any(c in "*?[]{}" for c in path):
        raise ValueError(f"plan path contains glob metacharacters: {path!r}")


def validate_execution_paths(paths: Iterable[str]) -> list[str]:
    path_list = list(paths)
    if not path_list:
        return []
    for p in path_list:
        validate_execution_path(p)
    ordered = sorted(set(path_list), key=lambda p: p.encode("utf-8"))
    if len(ordered) != len(path_list):
        raise ValueError("duplicate plan paths")
    return ordered


def command_sha256(envelope: CommandEnvelope) -> str:
    return sha256(
        {
            "api_version": envelope.api_version,
            "workspace_id": str(envelope.workspace_id),
            "command": envelope.command.model_dump(mode="json"),
        }
    )


def close_attempt_identity_sha256(
    *,
    work_id: str | UUID,
    revision_id: str | UUID,
    candidate_id: str | UUID,
    candidate_sha256: str,
    candidate_commit: str,
    plan_receipt_id: str | UUID,
    repository: str,
    diff_sha256: str,
    starting_dirty_paths: Iterable[str],
    sealed_riders: Iterable[dict[str, object]],
) -> str:
    """Canonical close attempt resume identity (OMP-140)."""
    rider_snapshots = [
        {
            "work_id": str(r["work_id"]),
            "revision_id": str(r["revision_id"]),
            "title": str(r["title"]),
            "criteria": list(r.get("criteria") or []),
            "evidence_sha256": str(r["evidence_sha256"]),
        }
        for r in sorted(sealed_riders, key=lambda r: str(r["work_id"]))
    ]
    return sha256(
        {
            "work_id": str(work_id),
            "revision_id": str(revision_id),
            "candidate_id": str(candidate_id),
            "candidate_sha256": str(candidate_sha256),
            "candidate_commit": str(candidate_commit),
            "plan_receipt_id": str(plan_receipt_id),
            "repository": str(repository),
            "diff_sha256": str(diff_sha256),
            "starting_dirty_paths": list(starting_dirty_paths),
            "riders": rider_snapshots,
        }
    )
