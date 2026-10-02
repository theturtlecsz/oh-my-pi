"""Candidate workspace freezing and materialization for research engineering adapter (R07, OMP-315)."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import subprocess  # nosec B404 - git argv list only, no shell
import tarfile

__all__ = [
    "freeze_candidate",
    "materialize",
]


def freeze_candidate(repo: Path | str, commit: str) -> tuple[bytes, str]:
    """Archive a git commit into tar bytes and return (bytes, sha256_hex)."""
    cmd = ["git", "-C", str(repo), "archive", "--format=tar", str(commit)]
    proc = subprocess.run(  # nosec B603 - fixed argv, no shell
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    archive_bytes = proc.stdout
    digest = hashlib.sha256(archive_bytes).hexdigest()
    return archive_bytes, digest


def materialize(
    tar: bytes | Path | str | io.BytesIO,
    sha256: str | bytes,
    dest: Path | str,
) -> Path:
    """Verify digest and extract tar archive files and directories safely into dest.

    Refuses digest mismatches, absolute paths, parent directory traversal (".."),
    symlinks, hardlinks, and device members before writing anything.
    """
    if isinstance(tar, (str, Path)):
        tar_bytes = Path(tar).read_bytes()
    elif isinstance(tar, io.BytesIO):
        tar_bytes = tar.getvalue()
    elif isinstance(tar, (bytes, bytearray)):
        tar_bytes = bytes(tar)
    else:
        raise TypeError(f"tar must be bytes, Path, str, or BytesIO, got {type(tar)}")

    actual_sha = hashlib.sha256(tar_bytes).hexdigest()
    if isinstance(sha256, bytes):
        expected_sha = sha256.hex().lower()
    else:
        expected_sha = str(sha256).strip().lower()

    if actual_sha != expected_sha:
        raise ValueError(f"digest mismatch: expected {expected_sha}, got {actual_sha}")

    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
            members = archive.getmembers()
            for member in members:
                norm_name = member.name.replace("\\", "/")
                if (
                    norm_name.startswith("/")
                    or os.path.isabs(member.name)
                    or re.match(r"^[a-zA-Z]:", member.name)
                ):
                    raise ValueError(f"refused absolute member: {member.name}")

                parts = [p for p in norm_name.split("/") if p]
                if any(p == ".." for p in parts):
                    raise ValueError(f"refused '..' member: {member.name}")

                if member.issym() or member.type == tarfile.SYMTYPE:
                    raise ValueError(f"refused symlink member: {member.name}")

                if member.islnk() or member.type == tarfile.LNKTYPE:
                    raise ValueError(f"refused hardlink member: {member.name}")

                if (
                    member.isdev()
                    or member.ischr()
                    or member.isblk()
                    or member.isfifo()
                ):
                    raise ValueError(f"refused device member: {member.name}")

                if not (member.isreg() or member.isdir()):
                    raise ValueError(f"refused unsupported member type: {member.name}")

            dest_path = Path(dest)
            dest_path.mkdir(parents=True, exist_ok=True)
            archive.extractall(path=dest_path)
            return dest_path
    except tarfile.TarError as err:
        raise ValueError(f"invalid tar archive: {err}") from err
