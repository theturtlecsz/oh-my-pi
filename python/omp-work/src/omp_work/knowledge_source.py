from __future__ import annotations

import hashlib
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .knowledge_contracts import (
    CodeSnapshotManifest,
    ManifestFile,
    RepositoryIdentity,
)


class KnowledgeSourceError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def normalize_remote_url(url: str) -> str:
    url = url.strip()
    m = re.match(r"^git@([^:/]+):(.*)$", url)
    if m:
        host, path = m.group(1), m.group(2)
        url = f"ssh://git@{host}/{path.lstrip('/')}"

    parsed = urlsplit(url)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc
        if "@" in netloc:
            userinfo, _, host_port = netloc.rpartition("@")
            userinfo += "@"
        else:
            userinfo = ""
            host_port = netloc

        if parsed.hostname is not None:
            hostname = parsed.hostname.lower()
            if "[" in host_port and "]" in host_port:
                host_str = f"[{hostname}]"
            else:
                host_str = hostname

            if parsed.port is not None and parsed.port not in (22, 443):
                netloc = f"{userinfo}{host_str}:{parsed.port}"
            else:
                netloc = f"{userinfo}{host_str}"

        url = urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def _git_run(
    args: list[str],
    root: str | Path,
    *,
    input: bytes | None = None,
    text: bool = True,
) -> str | bytes:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            timeout=10,
            env=env,
            input=input,
            capture_output=True,
            text=text,
            check=True,
        )
        return proc.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise KnowledgeSourceError("checkout_invalid") from exc


def _check_checkout_root(root: str | Path) -> Path:
    root_path = Path(root).resolve()
    proc_out = _git_run(["rev-parse", "--show-toplevel"], root_path, text=True)
    toplevel = os.path.realpath(str(proc_out).strip())
    if toplevel != os.path.realpath(str(root_path)):
        raise KnowledgeSourceError("checkout_invalid")
    return root_path


def resolve_repository_identity(
    cur: Any, workspace_id: UUID | str, root: str | Path
) -> RepositoryIdentity:
    root_path = _check_checkout_root(root)

    origin_out = _git_run(["remote", "get-url", "origin"], root_path, text=True)
    origin_url = str(origin_out).strip()
    if not origin_url:
        raise KnowledgeSourceError("checkout_invalid")
    normalized_origin = normalize_remote_url(origin_url)

    roots_out = _git_run(
        ["rev-list", "--max-parents=0", "HEAD"], root_path, text=True
    )
    root_commits = [
        line.strip() for line in str(roots_out).splitlines() if line.strip()
    ]
    if not root_commits:
        raise KnowledgeSourceError("checkout_invalid")

    for sha in root_commits:
        _git_run(["cat-file", "-e", f"{sha}^{{commit}}"], root_path, text=True)

    ws_uuid = (
        workspace_id if isinstance(workspace_id, UUID) else UUID(str(workspace_id))
    )
    cur.execute(
        "SELECT repository_id, url FROM omp_work.repositories WHERE workspace_id=%s AND NOT archived;",
        (ws_uuid,),
    )
    rows = cur.fetchall()

    matching_rows: list[tuple[UUID, str]] = []
    for row in rows:
        if isinstance(row, dict):
            repo_id = row["repository_id"]
            repo_url = row["url"]
        else:
            repo_id = row[0]
            repo_url = row[1]
        if normalize_remote_url(str(repo_url)) == normalized_origin:
            repo_uuid = (
                repo_id if isinstance(repo_id, UUID) else UUID(str(repo_id))
            )
            matching_rows.append((repo_uuid, str(repo_url)))

    if len(matching_rows) == 0:
        raise KnowledgeSourceError("repository_unresolved")
    if len(matching_rows) > 1:
        raise KnowledgeSourceError("repository_ambiguous")

    chosen_repo_id, _ = matching_rows[0]
    return RepositoryIdentity(
        workspace_id=ws_uuid,
        repository_id=chosen_repo_id,
        canonical_remote_url=normalized_origin,
        root_commits=tuple(root_commits),
        verified_at=datetime.now(timezone.utc),
    )


def capture_manifest(
    identity: RepositoryIdentity, root: str | Path
) -> CodeSnapshotManifest:
    root_path = _check_checkout_root(root)

    base_commit_out = _git_run(["rev-parse", "HEAD"], root_path, text=True)
    base_commit = str(base_commit_out).strip()

    ls_out = _git_run(["ls-tree", "-r", "-z", "HEAD"], root_path, text=False)
    assert isinstance(ls_out, bytes)

    blob_entries: list[tuple[str, str]] = []
    for item in ls_out.split(b"\0"):
        if not item:
            continue
        parts = item.split(b"\t", 1)
        if len(parts) != 2:
            continue
        meta, path_bytes = parts
        meta_parts = meta.split()
        if len(meta_parts) != 3:
            continue
        _, obj_type, obj_sha = meta_parts
        if obj_type != b"blob":
            continue
        path_str = path_bytes.decode("utf-8")
        blob_entries.append((obj_sha.decode("ascii"), path_str))

    blob_map: dict[str, tuple[str, int]] = {}
    unique_blobs = list(dict.fromkeys(sha for sha, _ in blob_entries))
    if unique_blobs:
        input_bytes = b"".join(f"{sha}\n".encode("ascii") for sha in unique_blobs)
        cat_out = _git_run(
            ["cat-file", "--batch"], root_path, input=input_bytes, text=False
        )
        assert isinstance(cat_out, bytes)
        idx = 0
        for _ in unique_blobs:
            header_end = cat_out.find(b"\n", idx)
            if header_end == -1:
                raise KnowledgeSourceError("checkout_invalid")
            header = cat_out[idx:header_end].decode("ascii", errors="replace")
            hparts = header.split()
            if len(hparts) != 3 or hparts[1] != "blob":
                raise KnowledgeSourceError("checkout_invalid")
            sha = hparts[0]
            size = int(hparts[2])
            content_start = header_end + 1
            content_end = content_start + size
            if content_end > len(cat_out):
                raise KnowledgeSourceError("checkout_invalid")
            content = cat_out[content_start:content_end]
            idx = content_end + 1
            blob_map[sha] = (hashlib.sha256(content).hexdigest(), size)

    manifest_files: dict[str, ManifestFile] = {}
    for obj_sha, path in blob_entries:
        if obj_sha not in blob_map:
            raise KnowledgeSourceError("checkout_invalid")
        sha256_hash, size = blob_map[obj_sha]
        manifest_files[path] = ManifestFile(
            path=path,
            kind="base",
            sha256=sha256_hash,
            size=size,
        )

    status_out = _git_run(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        root_path,
        text=False,
    )
    assert isinstance(status_out, bytes)

    status_items = status_out.split(b"\0")
    i = 0
    while i < len(status_items) and status_items[i]:
        entry = status_items[i]
        if len(entry) < 3:
            i += 1
            continue
        code = entry[:2].decode("ascii", errors="replace")
        path = entry[3:].decode("utf-8")
        i += 1

        orig_path: str | None = None
        if "R" in code or "C" in code:
            if i < len(status_items):
                orig_path = status_items[i].decode("utf-8")
                i += 1

        if code == "!!":
            continue

        if orig_path and "R" in code:
            if orig_path in manifest_files:
                manifest_files[orig_path] = ManifestFile(
                    path=orig_path,
                    kind="deleted",
                    sha256=None,
                    size=None,
                )

        x, y = code[0], code[1]
        is_deleted = y == "D" or (x == "D" and y not in ("M", "T"))
        if is_deleted:
            if path in manifest_files:
                manifest_files[path] = ManifestFile(
                    path=path,
                    kind="deleted",
                    sha256=None,
                    size=None,
                )
        elif code == "??":
            try:
                data = (root_path / path).read_bytes()
            except OSError as exc:
                raise KnowledgeSourceError("checkout_invalid") from exc
            manifest_files[path] = ManifestFile(
                path=path,
                kind="untracked",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
        else:
            try:
                data = (root_path / path).read_bytes()
            except OSError as exc:
                raise KnowledgeSourceError("checkout_invalid") from exc
            manifest_files[path] = ManifestFile(
                path=path,
                kind="modified",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )

    return CodeSnapshotManifest(
        workspace_id=identity.workspace_id,
        repository_id=identity.repository_id,
        base_commit=base_commit,
        files=tuple(manifest_files.values()),
    )


__all__ = [
    "KnowledgeSourceError",
    "capture_manifest",
    "normalize_remote_url",
    "resolve_repository_identity",
]
