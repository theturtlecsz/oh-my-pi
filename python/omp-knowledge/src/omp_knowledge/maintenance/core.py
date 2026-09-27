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
    if not source.is_dir():
        return
    destination = dest_root / SOURCES_DIR
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
        if not source.is_file():
            continue
        target = destination / spec.filename
        tables = copy_store(source, target)
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
    verify_manifest_files(source, manifest)
    records = load_records(source)
    verify_record_digests(manifest, records)

    if target.exists():
        if not target.is_dir():
            raise MaintenanceError(f"target root is not a directory: {target}")
        if any(target.iterdir()):
            raise MaintenanceError(f"target root is not empty: {target}")
    target.mkdir(parents=True, exist_ok=True)

    store_names = [spec.name for spec in STORE_SPECS if spec.name in records["stores"]]
    for spec in STORE_SPECS:
        if spec.name in records["stores"]:
            _insert_store(spec, target, records["stores"][spec.name]["tables"])

    _copy_sources(source, target)
    _verify_target(target, manifest, store_names)
    return {
        "target_root": str(target),
        "stores": sorted(store_names),
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
    verify_manifest_files(source, manifest)
    records = load_records(source)
    verify_record_digests(manifest, records)

    aside: Path | None = None
    if root.exists():
        if not root.is_dir():
            raise MaintenanceError(f"state root is not a directory: {root}")
        aside = aside_path(root)
        root.rename(aside)
    root.mkdir(parents=True, exist_ok=True)

    restored_stores: list[str] = []
    for spec in STORE_SPECS:
        backup_file = source / spec.filename
        if not backup_file.is_file():
            continue
        target_file = root / spec.filename
        shutil.copyfile(backup_file, target_file)
        restored_stores.append(spec.name)

    _copy_sources(source, root)
    _verify_target(root, manifest, restored_stores)
    for spec in STORE_SPECS:
        if spec.name in restored_stores:
            remove_sidecars(root / spec.filename)
    return {
        "state_root": str(root),
        "aside": str(aside) if aside is not None else None,
        "stores": sorted(restored_stores),
        "files": len(manifest["files"]),
    }


__all__ = [
    "FORMAT",
    "MANIFEST_NAME",
    "MaintenanceError",
    "RECORDS_NAME",
    "SOURCES_DIR",
    "STORE_SPECS",
    "aside_path",
    "create_backup",
    "file_sha256",
    "load_manifest",
    "load_records",
    "rebuild_from_records",
    "rollback",
    "verify_manifest_files",
    "verify_record_digests",
]
