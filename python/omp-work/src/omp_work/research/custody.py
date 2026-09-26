"""Path containment, archive rejection, retention, and ACL checks for R04 custody.

Byte installation itself lives in operations.artifacts. This module decides
whether a collection, source location, cache claim, or retrieval is allowed
before any bytes are returned.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from omp_work.v1.models import RESEARCH_ARTIFACT_MAX_BYTES

_SOURCE_LOCATOR = re.compile(r"^source://[A-Za-z0-9._+-]+@[A-Za-z0-9._+-]+$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def artifact_path(data_dir: Path, workspace_id: object, digest: str) -> Path:
    return (
        data_dir / "research-artifacts" / str(workspace_id) / digest[:2] / digest
    )


def collection_root(data_dir: Path, workspace_id: object) -> Path:
    return data_dir / "research-collections" / str(workspace_id)


def _unsafe_relative(relative: str) -> bool:
    if (
        not relative
        or relative.startswith(("/", "\\"))
        or "\\" in relative
        or "\x00" in relative
    ):
        return True
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in relative):
        return True
    parts = Path(relative).parts
    return not parts or any(part in {"", ".", ".."} for part in parts)


def contained_path(root: Path, relative: str) -> Path:
    """Resolve relative under root. Absolute paths, .., and symlinks refuse."""
    if _unsafe_relative(relative):
        raise ValueError("path escapes containment")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    resolved_root = root.resolve()
    current = resolved_root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("path escapes containment")
    try:
        current.resolve().relative_to(resolved_root)
    except ValueError:
        raise ValueError("path escapes containment") from None
    return current


def validate_declared_location(location: str) -> None:
    """Source locations are contained relative paths or source://name@version."""
    if _SOURCE_LOCATOR.fullmatch(location):
        return
    if _unsafe_relative(location):
        raise ValueError("path escapes containment")


def unsafe_archive_member(name: str) -> bool:
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        return True
    return any(part == ".." for part in normalized.split("/"))


def load_collected_bytes(root: Path, relative: str, member: str | None) -> bytes:
    """Read one contained file. Zip archives are rejected when any member escapes.

    A named member is the artifact. An archive with only safe members is kept
    as the archive bytes themselves. Nothing larger than the inline ceiling is read.
    """
    path = contained_path(root, relative)
    if not path.is_file():
        raise ValueError("collected path is not a file")
    size = path.stat().st_size
    if size > RESEARCH_ARTIFACT_MAX_BYTES:
        raise ValueError("artifact exceeds inline custody ceiling")
    data = path.read_bytes()
    if len(data) > RESEARCH_ARTIFACT_MAX_BYTES:
        raise ValueError("artifact exceeds inline custody ceiling")
    if member is None and not zipfile.is_zipfile(path):
        return data
    return zip_member_bytes(data, member)


def zip_member_bytes(data: bytes, member: str | None) -> bytes:
    buffer = io.BytesIO(data)
    if not zipfile.is_zipfile(buffer):
        if member is None:
            return data
        raise ValueError("not a zip archive")
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as archive:
        for info in archive.infolist():
            if unsafe_archive_member(info.filename):
                raise ValueError("archive member escapes containment")
        if member is None:
            return data
        if unsafe_archive_member(member):
            raise ValueError("archive member escapes containment")
        try:
            extracted = archive.read(member)
        except KeyError:
            raise ValueError("archive member missing") from None
    if len(extracted) > RESEARCH_ARTIFACT_MAX_BYTES:
        raise ValueError("artifact exceeds inline custody ceiling")
    return extracted


def retention_expired(until: datetime | str | None, *, now: datetime | None = None) -> bool:
    if until is None:
        return False
    moment = now or datetime.now(UTC)
    if isinstance(until, str):
        until = datetime.fromisoformat(until)
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until < moment


def retrieval_denied(
    access_class: str,
    decision: str | None,
    *,
    project_id: str | None,
    declared_project: str | None = None,
) -> str | None:
    """Project permissions are decided before any byte read. Workspace class
    does not consult a project decision."""
    if access_class == "workspace":
        return None
    if (
        project_id is None
        or decision != "allow"
        or project_id != declared_project
    ):
        return "project permissions deny retrieval"
    return None


def replicate_masquerade(*, cached: bool) -> str | None:
    if cached:
        return "cached result cannot masquerade as new independent replicate"
    return None
