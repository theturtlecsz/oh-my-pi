from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import CommandEnvelope

CANDIDATE_HASH_ALGORITHM = "work.omp.dev/v1/candidate-sha256"
_COMMIT_SHA_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def candidate_sha256(commit_sha: str, paths: Iterable[str]) -> str:
    """Canonical candidate hash, pinned by decision 0004 and contracts/v1/candidate-hash.json.

    Paths are the commit's complete file list (`git diff-tree --no-commit-id --name-only -r`),
    hashed exactly as stored — no Unicode normalization, since a Git tree may legally contain
    both NFC and NFD spellings of the same displayed name as distinct entries.
    """
    if not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("commit_sha must be a full lowercase hex object id (40 or 64 chars)")
    ordered = sorted(paths, key=lambda path: path.encode("utf-8"))
    if not ordered:
        raise ValueError("candidate path set must not be empty")
    previous: str | None = None
    for path in ordered:
        if not path or path.startswith("./") or path.endswith("/") or "\\" in path or "//" in path:
            raise ValueError(f"candidate path is not a canonical repo-relative file path: {path!r}")
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
            raise ValueError(f"candidate path contains control characters: {path!r}")
        if path == previous:
            raise ValueError(f"duplicate candidate path: {path!r}")
        previous = path
    return sha256({"algorithm": CANDIDATE_HASH_ALGORITHM, "commit_sha": commit_sha, "paths": ordered})


def command_sha256(envelope: CommandEnvelope) -> str:
    return sha256({
        "api_version": envelope.api_version,
        "workspace_id": str(envelope.workspace_id),
        "command": envelope.command.model_dump(mode="json"),
    })
