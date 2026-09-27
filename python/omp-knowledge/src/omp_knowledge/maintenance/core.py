"""Backup, exact-record rebuild, and rollback of a Fleet Knowledge state root.

``create_backup`` copies every present store with the sqlite3 backup API and
reads all its rows inside one read transaction, so the copied database and the
exact records come from the same snapshot even while a writer is active;
``rebuild_from_records`` recreates the stores through their own schema owners,
inserts the exact rows, and refuses unless integrity and per-table digests match;
``rollback`` verifies every manifest checksum before it touches the live root,
then moves the current root aside (never deletes it) and restores the copies.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .records import (
    FORMAT,
    MANIFEST_NAME,
    RECORDS_NAME,
    SOURCES_DIR,
    STORE_SPECS,
    MaintenanceError,
    StoreSpec,
    aside_path,
    copy_store,
    decode_value,
    extract_tables,
    file_sha256,
    load_manifest,
    load_records,
    manifest_entry,
    remove_sidecars,
    verify_manifest_files,
    verify_record_digests,
    verify_store_contents,
    write_json,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_files(state_root: Path) -> list[str]:
    """Relative POSIX paths of every regular file under ``sources/``."""
    root = state_root / SOURCES_DIR
    if not root.is_dir():
        return []
    found: list[str] = []
    for directory, _dirnames, filenames in os.walk(root):
        base = Path(directory)
        for filename in filenames:
            relative = (base / filename).relative_to(state_root)
            found.append(relative.as_posix())
    return sorted(found)


def _copy_sources(source_root: Path, dest_root: Path) -> None:
    source = source_root / SOURCES_DIR
    destination = dest_root / SOURCES_DIR
    if not source.is_dir():
        # A tree removed from the state root must not survive in a backup
        # directory that previously held it; a backup reflects the root now.
        if destination.is_dir():
            shutil.rmtree(destination)
        return
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def create_backup(
    state_root: str | os.PathLike[str],
    backup_dir: str | os.PathLike[str],
) -> dict[str, Any]:
    """Take a consistent backup of a state root into ``backup_dir``."""
    root = Path(state_root).resolve()
    destination = Path(backup_dir).resolve()
    if not root.is_dir():
        raise MaintenanceError(f"state root is not a directory: {root}")
    destination.mkdir(parents=True, exist_ok=True)

    files: dict[str, dict[str, Any]] = {}
    stores: dict[str, dict[str, Any]] = {}
    record_stores: dict[str, dict[str, Any]] = {}

    for spec in STORE_SPECS:
        source = root / spec.filename
        target = destination / spec.filename
        if not source.is_file():
            # A store removed from the root must not linger from an earlier
            # backup in the same directory; the manifest would keep naming it.
            if target.exists():
                target.unlink()
            remove_sidecars(target)
            continue
        tables = copy_store(source, target)
        remove_sidecars(target)
        stores[spec.name] = {
            "file": spec.filename,
            "tables": {
                name: {"sha256": info["sha256"], "rows": len(info["rows"])}
                for name, info in tables.items()
            },
        }
        record_stores[spec.name] = {"file": spec.filename, "tables": tables}
        files[spec.filename] = manifest_entry(target)

    for relative in _source_files(root):
        files[relative] = manifest_entry(root / relative)
    _copy_sources(root, destination)

    write_json(
        destination / RECORDS_NAME,
        {"format": FORMAT, "stores": record_stores},
    )
    files[RECORDS_NAME] = manifest_entry(destination / RECORDS_NAME)
    manifest = {
        "format": FORMAT,
        "created_at": _utc_now(),
        "files": files,
        "stores": stores,
    }
    write_json(destination / MANIFEST_NAME, manifest)
    return {
        "backup_dir": str(destination),
        "stores": sorted(stores),
        "files": len(files),
        "manifest_sha256": file_sha256(destination / MANIFEST_NAME),
    }


def _create_store(spec: StoreSpec, target_root: Path) -> None:
    """Create one store's empty schema through its own owner."""
    owner = spec.create_schema(target_root)
    close = getattr(owner, "close", None)
    if callable(close):
        close()


def _insert_store(spec: StoreSpec, target_root: Path, tables: dict[str, Any]) -> None:
    """Create one store's schema through its owner, then insert its exact rows."""
    _create_store(spec, target_root)
    db_path = target_root / spec.filename
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("BEGIN")
        for name in sorted(tables):
            columns = tables[name]["columns"]
            rows = tables[name]["rows"]
            if not columns:
                continue
            selection = ", ".join(f'"{column}"' for column in columns)
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "{name}" ({selection}) VALUES ({placeholders})',
                [tuple(decode_value(value) for value in row) for row in rows],
            )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    check = sqlite3.connect(str(db_path))
    try:
        status = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if status != "ok":
        raise MaintenanceError(f"integrity_check failed for {spec.filename}: {status}")


def _verify_target(
    target_root: Path, manifest: dict[str, Any], store_names: list[str]
) -> None:
    for spec in STORE_SPECS:
        if spec.name not in store_names:
            continue
        expected = manifest["stores"][spec.name]["tables"]
        conn = sqlite3.connect(str(target_root / spec.filename))
        try:
            actual = extract_tables(conn)
        finally:
            conn.close()
        if set(actual) != set(expected):
            raise MaintenanceError(
                f"rebuilt store {spec.name} has tables {sorted(actual)} "
                f"but the manifest records {sorted(expected)}"
            )
        for name, info in actual.items():
            if info["sha256"] != expected[name]["sha256"]:
                raise MaintenanceError(
                    f"rebuilt store {spec.name}.{name} does not match its recorded digest"
                )


def rebuild_from_records(
    backup_dir: str | os.PathLike[str],
    target_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Rebuild a state root from an exact-record backup into an empty target."""
    source = Path(backup_dir).resolve()
    target = Path(target_root).resolve()
    manifest = load_manifest(source)
    # Everything that can refuse must refuse before the target is created:
    # checksums, the record/manifest store and table sets, the record digests,
    # and the on-disk store contents.
    verify_manifest_files(source, manifest)
    records = load_records(source)
    verify_record_digests(manifest, records)
    verify_store_contents(source, manifest)

    if target.exists():
        if not target.is_dir():
            raise MaintenanceError(f"target root is not a directory: {target}")
        if any(target.iterdir()):
            raise MaintenanceError(f"target root is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    store_names = sorted(records["stores"])
    for spec in STORE_SPECS:
        if spec.name in store_names:
            _insert_store(spec, target, records["stores"][spec.name]["tables"])

    _copy_sources(source, target)
    _verify_target(target, manifest, store_names)
    return {
        "target_root": str(target),
        "stores": store_names,
        "files": len(manifest["files"]),
    }


def rollback(
    backup_dir: str | os.PathLike[str],
    state_root: str | os.PathLike[str],
) -> dict[str, Any]:
    """Restore a state root from a backup, moving the current root aside."""
    source = Path(backup_dir).resolve()
    root = Path(state_root).resolve()
    manifest = load_manifest(source)
    # Refuse on any damage while the live root is still in place: checksums, the
    # record/manifest store and table sets, the record digests, and the on-disk
    # store contents. Only after all of that passes do we touch the live root.
    verify_manifest_files(source, manifest)
    records = load_records(source)
    verify_record_digests(manifest, records)
    verify_store_contents(source, manifest)

    # Restore only the files the manifest names, into a staging directory in the
    # root's parent (so the rename is atomic on one filesystem). The old root is
    # never mutated until the staged tree is complete and verified.
    store_names = sorted(records["stores"])
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f"{root.name}.rollback-", dir=str(root.parent))
    )
    aside: Path | None = None
    try:
        for spec in STORE_SPECS:
            if spec.name not in store_names:
                continue
            backup_file = source / spec.filename
            target_file = staging / spec.filename
            shutil.copyfile(backup_file, target_file)
            remove_sidecars(target_file)
        _copy_sources(source, staging)
        _verify_target(staging, manifest, store_names)

        if root.exists():
            if not root.is_dir():
                raise MaintenanceError(f"state root is not a directory: {root}")
            aside = aside_path(root)
            root.rename(aside)
        try:
            staging.rename(root)
        except BaseException:
            if aside is not None:
                aside.rename(root)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    return {
        "state_root": str(root),
        "aside": str(aside) if aside is not None else None,
        "stores": store_names,
        "files": len(manifest["files"]),
    }


__all__ = [
    "FORMAT",
    "MANIFEST_NAME",
    "RECORDS_NAME",
    "SOURCES_DIR",
    "STORE_SPECS",
    "MaintenanceError",
    "aside_path",
    "create_backup",
    "file_sha256",
    "load_manifest",
    "load_records",
    "rebuild_from_records",
    "rollback",
    "verify_manifest_files",
    "verify_record_digests",
    "verify_store_contents",
]
