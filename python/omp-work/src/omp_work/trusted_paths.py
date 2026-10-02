"""L-RT-03: Work Ledger project repos plus OMP's own checkouts trusted paths list builder."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import UUID


def normalize_url(url: str) -> str:
    """Normalize a git repository URL according to L-RT-03.

    - drop scheme (e.g. https://, ssh://, git://)
    - drop user@ (e.g. git@)
    - drop trailing '/' and '.git'
    - scp 'host:path' to 'host/path'
    - lowercase host
    """
    url = url.strip()
    if not url:
        return ""

    # Drop scheme (e.g. https://, ssh://, git://)
    if "://" in url:
        _, _, url = url.partition("://")

    # Drop user@ (e.g. git@github.com:... or user:pass@github.com/...)
    at_idx = url.find("@")
    if at_idx != -1:
        slash_idx = url.find("/")
        if slash_idx == -1 or at_idx < slash_idx:
            url = url[at_idx + 1 :]

    # Drop trailing '/' and '.git'
    while True:
        if url.endswith("/"):
            url = url.rstrip("/")
        elif url.endswith(".git"):
            url = url[:-4]
        else:
            break

    # scp host:path to host/path
    colon_idx = url.find(":")
    slash_idx = url.find("/")
    if colon_idx != -1 and (slash_idx == -1 or colon_idx < slash_idx):
        url = url[:colon_idx] + "/" + url[colon_idx + 1 :].lstrip("/")

    # Lowercase host
    host, sep, path = url.partition("/")
    url = host.lower() + sep + path

    while url.endswith("/"):
        url = url.rstrip("/")

    return url


def get_remote_origin(repo_path: Path | str) -> str | None:
    """Origin: git -C DIR config --get remote.origin.url."""
    try:
        proc = subprocess.run(  # nosec B603, B607
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0:
            out = proc.stdout.strip()
            return out if out else None
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def is_linked_worktree(path: Path | str) -> bool:
    """Return True if path is a linked git worktree (has a .git file)."""
    git_entry = Path(path) / ".git"
    try:
        return git_entry.is_file()
    except OSError:
        return False


def get_main_checkout(path: Path | str) -> Path | None:
    """Return main checkout (parent of git rev-parse --path-format=absolute --git-common-dir)."""
    try:
        proc = subprocess.run(  # nosec B603, B607
            [
                "git",
                "-C",
                str(path),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            common_dir = Path(proc.stdout.strip())
            return common_dir.parent
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def find_omp_checkout(source_path: Path | None = None) -> tuple[Path | None, str | None]:
    """Find the checkout holding the omp_work source and its origin URL."""
    if source_path is None:
        source_dir = Path(__file__).resolve().parent
    else:
        p = Path(source_path).resolve()
        source_dir = p.parent if p.is_file() else p

    try:
        proc = subprocess.run(  # nosec B603, B607
            ["git", "-C", str(source_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return None, None
        checkout = Path(proc.stdout.strip()).resolve()
        origin = get_remote_origin(checkout)
        return checkout, origin
    except (OSError, subprocess.SubprocessError):
        return None, None


def find_candidates(scan_roots: Iterable[Path | str], max_depth: int = 2) -> list[Path]:
    """Scan roots and subdirs to depth 2 with a .git dir or .git file (linked worktree)."""
    candidates: list[Path] = []
    seen_realpaths: set[str] = set()

    for root_raw in scan_roots:
        root = Path(root_raw).expanduser()
        if not root.is_dir():
            continue

        queue: list[tuple[Path, int]] = [(root, 0)]
        visited_dirs: set[str] = set()

        while queue:
            current, depth = queue.pop(0)

            try:
                real_current = os.path.realpath(str(current))
            except OSError:
                continue

            if real_current in visited_dirs:
                continue
            visited_dirs.add(real_current)

            git_entry = current / ".git"
            try:
                if git_entry.exists():
                    if real_current not in seen_realpaths:
                        seen_realpaths.add(real_current)
                        candidates.append(current)
            except OSError:
                pass

            if depth < max_depth:
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            if entry.name == ".git":
                                continue
                            try:
                                if entry.is_dir():
                                    queue.append((Path(entry.path), depth + 1))
                            except OSError:
                                continue
                except OSError:
                    continue

    return candidates


def get_registered_urls(store: Any, workspace_id: UUID, actor_id: UUID) -> set[str]:
    """1. Registered URLs: store.list_projects(ws, actor) (shape: see show),

    then store.read_project(...)["repositories"][*]["url"]; skip missing urls.
    """
    data = store.list_projects(workspace_id, actor_id)
    if isinstance(data, dict):
        projects = data.get("projects", [])
    elif isinstance(data, list):
        projects = data
    else:
        projects = []

    urls: set[str] = set()
    for proj in projects:
        if isinstance(proj, dict):
            pid = proj.get("project_id")
        else:
            pid = getattr(proj, "project_id", None)
        if not pid:
            continue
        if isinstance(pid, str):
            pid = UUID(pid)

        read = store.read_project(workspace_id, actor_id, pid)
        if not isinstance(read, dict):
            continue
        repos = read.get("repositories", [])
        if not isinstance(repos, (list, tuple)):
            continue
        for repo in repos:
            if isinstance(repo, dict):
                raw_url = repo.get("url")
                if isinstance(raw_url, str) and raw_url.strip():
                    norm = normalize_url(raw_url)
                    if norm:
                        urls.add(norm)
    return urls


def build_trusted_paths(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    scan_roots: Iterable[Path | str],
    *,
    omp_source: Path | None = None,
) -> list[str]:
    """Compute the sorted unique realpaths of trusted project checkouts."""
    registered_urls = get_registered_urls(store, workspace_id, actor_id)

    candidates = find_candidates(scan_roots, max_depth=2)

    omp_checkout, omp_origin = find_omp_checkout(omp_source)

    trusted_origins = set(registered_urls)
    if omp_origin:
        norm_omp = normalize_url(omp_origin)
        if norm_omp:
            trusted_origins.add(norm_omp)

    trusted_dirs: set[Path] = set()

    # OMP checkout itself: (OMP; that checkout too)
    if omp_checkout:
        trusted_dirs.add(omp_checkout)
        if is_linked_worktree(omp_checkout):
            main_co = get_main_checkout(omp_checkout)
            if main_co:
                trusted_dirs.add(main_co)

    for cand in candidates:
        cand_origin = get_remote_origin(cand)
        if cand_origin:
            norm_cand = normalize_url(cand_origin)
            if norm_cand and norm_cand in trusted_origins:
                trusted_dirs.add(cand)
                if is_linked_worktree(cand):
                    main_co = get_main_checkout(cand)
                    if main_co:
                        trusted_dirs.add(main_co)

    realpaths = {os.path.realpath(str(p)) for p in trusted_dirs}
    return sorted(realpaths)


def trusted_paths(
    store: Any,
    workspace_id: UUID,
    actor_id: UUID,
    scan_roots: Iterable[Path | str] | Path | str | None = None,
    output: Path | str | None = None,
    *,
    omp_source: Path | None = None,
) -> int:
    """Command handler for projects trusted-paths."""
    if scan_roots is None:
        scan_roots = ()
    elif isinstance(scan_roots, (str, Path)):
        scan_roots = [scan_roots]

    if output is None:
        output = Path.home() / ".omp" / "agent" / "trusted-projects.json"

    # Step 1 & Store read: Exit 2 on store error; old file kept.
    try:
        sorted_paths = build_trusted_paths(
            store,
            workspace_id,
            actor_id,
            scan_roots,
            omp_source=omp_source,
        )
    except Exception as exc:
        print(f"projects trusted-paths: {exc}", file=sys.stderr)
        return 2

    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.parent / f".{output_path.name}.tmp.{os.getpid()}"

    payload = {"version": 1, "paths": sorted_paths}
    content = json.dumps(payload, separators=(",", ":")) + "\n"

    try:
        temp_path.write_text(content, encoding="utf-8")
        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise

    return 0


trusted_paths_command = trusted_paths
cmd_trusted_paths = trusted_paths
