"""Exact-record primitives for a Fleet Knowledge state root (OMP-312).

A state root holds up to four SQLite stores plus a ``sources/`` tree. This
module owns the shared vocabulary the backup, rebuild, and rollback operations
build on: the store table, the value codec that round-trips every SQLite value
through JSON, a single-transaction reader that copies a store with the sqlite3
backup API while holding one read snapshot, and the manifest checksum helpers.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omp_work.knowledge_publication import StructuralPublicationStore
from omp_work.v1.canonical import sha256

from omp_knowledge.context.store import ContextBundleStore
from omp_knowledge.learning.store import LearningStore
from omp_knowledge.publication import PublicationManager
from omp_knowledge.vectors.store import VectorProjectionStore

FORMAT = "omp-knowledge-backup/1"
SOURCES_DIR = "sources"
MANIFEST_NAME = "manifest.json"
RECORDS_NAME = "exact_records.json"


class MaintenanceError(Exception):
    """A refused backup, rebuild, or rollback; the reason is user facing."""


@dataclass(frozen=True)
class StoreSpec:
    """One store of a state root: file name and the owner that owns its schema."""

    name: str
    filename: str
    create_schema: Callable[[Path], object]


def _learning_schema(root: Path) -> object:
    # An explicit ``.sqlite`` file path makes the owner's directory/suffix
    # heuristic unambiguous.
    return LearningStore(root / "learning.sqlite")


def _context_schema(root: Path) -> object:
    return ContextBundleStore(root)


def _structural_schema(root: Path) -> object:
    return StructuralPublicationStore(root)


def _publications_schema(root: Path) -> object:
    return PublicationManager(root)


def _vectors_schema(root: Path) -> object:
    return VectorProjectionStore(root)


STORE_SPECS: tuple[StoreSpec, ...] = (
    StoreSpec("learning", "learning.sqlite", _learning_schema),
    StoreSpec("context_bundles", "context-bundles.sqlite", _context_schema),
    StoreSpec("structural_publications", "structural-publications.sqlite", _structural_schema),
    StoreSpec("publications", "publications.sqlite", _publications_schema),
    StoreSpec("vectors", "vectors.sqlite", _vectors_schema),
)


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def encode_value(value: Any) -> Any:
    """JSON-safe encoding of one SQLite value that decodes back exactly."""
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"__float__": repr(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    raise MaintenanceError(f"unsupported column value type {type(value).__name__}")


def decode_value(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"__bytes__"}:
            return base64.b64decode(value["__bytes__"])
        if set(value) == {"__float__"}:
            return float(value["__float__"])
    return value


def list_tables(conn: sqlite3.Connection) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def read_table(
    conn: sqlite3.Connection, table: str, sql: str | None = None
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """All rows of one table in a deterministic order (primary key, else rowid)."""
    if sql is None:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        sql = row[0] if row is not None else None
    info = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
    columns = [row[1] for row in info]
    key_columns = sorted(
        ((row[1], row[5]) for row in info if row[5]), key=lambda item: item[1]
    )
    order = ""
    if key_columns:
        order = " ORDER BY " + ", ".join(_quote_ident(name) for name, _ in key_columns)
    elif sql is None or "WITHOUT ROWID" not in sql.upper():
        order = " ORDER BY rowid"
    selection = ", ".join(_quote_ident(column) for column in columns)
    rows = [
        tuple(row)
        for row in conn.execute(
            f"SELECT {selection} FROM {_quote_ident(table)}{order}",  # nosec B608 - identifiers quoted from schema introspection
        ).fetchall()
    ]
    return columns, rows


def table_digest(columns: Iterable[str], encoded_rows: Iterable[Iterable[Any]]) -> str:
    """Canonical digest of one table's columns and exact rows."""
    return sha256(
        {"columns": list(columns), "rows": [list(row) for row in encoded_rows]}
    )


def extract_tables(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every table of a store, with exact encoded rows and a per-table digest."""
    tables: dict[str, dict[str, Any]] = {}
    for name, sql in list_tables(conn):
        columns, rows = read_table(conn, name, sql)
        encoded = [[encode_value(value) for value in row] for row in rows]
        tables[name] = {
            "columns": columns,
            "rows": encoded,
            "sha256": table_digest(columns, encoded),
        }
    return tables


def copy_store(src_path: Path, dest_path: Path) -> dict[str, dict[str, Any]]:
    """Read every table and copy the file with the backup API in one read txn.

    The rows and the copied file both come from the same read snapshot, so a
    concurrent writer cannot tear the records away from the copy.
    """
    src = sqlite3.connect(str(src_path), isolation_level=None)
    src.execute("PRAGMA busy_timeout = 5000")
    dest = sqlite3.connect(str(dest_path))
    try:
        src.execute("BEGIN")
        tables = extract_tables(src)
        src.backup(dest)
        src.execute("COMMIT")
    except BaseException:
        try:
            src.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        src.close()
        dest.close()
    return tables


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_entry(path: Path) -> dict[str, Any]:
    return {"sha256": file_sha256(path), "size": path.stat().st_size}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_json(path: Path, *, what: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MaintenanceError(f"{what} missing: {path}") from exc
    except (OSError, ValueError) as exc:
        raise MaintenanceError(f"{what} unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MaintenanceError(f"{what} is not a JSON object: {path}")
    return payload


def load_manifest(backup_dir: Path) -> dict[str, Any]:
    path = backup_dir / MANIFEST_NAME
    manifest = read_json(path, what="manifest")
    if manifest.get("format") != FORMAT:
        raise MaintenanceError(f"unsupported backup format in {path}")
    for key in ("files", "stores"):
        if not isinstance(manifest.get(key), dict):
            raise MaintenanceError(f"manifest missing {key!r}: {path}")
    return manifest


def load_records(backup_dir: Path) -> dict[str, Any]:
    path = backup_dir / RECORDS_NAME
    records = read_json(path, what="exact records")
    if records.get("format") != FORMAT:
        raise MaintenanceError(f"unsupported exact-records format in {path}")
    if not isinstance(records.get("stores"), dict):
        raise MaintenanceError(f"exact records missing 'stores': {path}")
    return records


def verify_manifest_files(backup_dir: Path, manifest: dict[str, Any]) -> None:
    """Every file the manifest names must exist with its recorded sha256."""
    for relative, entry in manifest["files"].items():
        path = backup_dir / relative
        if not path.is_file():
            raise MaintenanceError(f"backup file missing: {relative}")
        actual = file_sha256(path)
        if actual != entry.get("sha256"):
            raise MaintenanceError(
                f"backup checksum mismatch for {relative}: "
                f"expected {entry.get('sha256')}, got {actual}"
            )


def verify_record_digests(manifest: dict[str, Any], records: dict[str, Any]) -> None:
    """The records must describe exactly the stores and tables the manifest names.

    A record whose store or table set diverges from the manifest (a dropped
    store, a dropped table) is a refusal, not a smaller rebuild: the whole point
    of the exact records is that they reconstruct the whole backup. Every
    remaining table digest must also equal the manifest's recorded digest.
    """
    recorded_stores = manifest["stores"]
    record_names = set(records["stores"])
    recorded_names = set(recorded_stores)
    if record_names != recorded_names:
        raise MaintenanceError(
            "exact records store set does not match the manifest: "
            f"missing {sorted(recorded_names - record_names)}, "
            f"unexpected {sorted(record_names - recorded_names)}"
        )
    for store_name in sorted(recorded_names):
        store = records["stores"][store_name]
        if not isinstance(store, dict) or not isinstance(store.get("tables"), dict):
            raise MaintenanceError(f"exact records missing tables for {store_name!r}")
        manifest_store = recorded_stores[store_name]
        if not isinstance(manifest_store, dict):
            raise MaintenanceError(f"manifest store {store_name!r} is malformed")
        recorded = manifest_store.get("tables")
        if not isinstance(recorded, dict):
            raise MaintenanceError(f"manifest has no tables for store {store_name!r}")
        table_names = set(store["tables"])
        recorded_tables = set(recorded)
        if table_names != recorded_tables:
            raise MaintenanceError(
                f"exact records tables for store {store_name!r} do not match the "
                f"manifest: missing {sorted(recorded_tables - table_names)}, "
                f"unexpected {sorted(table_names - recorded_tables)}"
            )
        for table_name in sorted(table_names):
            table = store["tables"][table_name]
            expected = recorded[table_name]
            if not isinstance(expected, dict) or "sha256" not in expected:
                raise MaintenanceError(
                    f"manifest has no digest for {store_name}.{table_name}"
                )
            actual = table_digest(table["columns"], table["rows"])
            if actual != expected["sha256"]:
                raise MaintenanceError(
                    f"exact record digest mismatch for {store_name}.{table_name}: "
                    f"expected {expected['sha256']}, got {actual}"
                )


def verify_store_contents(backup_dir: Path, manifest: dict[str, Any]) -> None:
    """Every store the manifest names must exist as SQLite with the recorded tables.

    Checked before rebuild creates anything or rollback renames the live root, so
    a store whose bytes do not match the manifest digest (a forged record digest,
    a truncated file) refuses while the state is still untouched.
    """
    for store_name in sorted(manifest["stores"]):
        store = manifest["stores"][store_name]
        if not isinstance(store, dict):
            raise MaintenanceError(f"manifest store {store_name!r} is malformed")
        filename = store.get("file")
        if not isinstance(filename, str):
            raise MaintenanceError(f"manifest store {store_name!r} has no file name")
        db_path = backup_dir / filename
        if not db_path.is_file():
            raise MaintenanceError(f"backup store missing: {filename}")
        expected = store.get("tables")
        if not isinstance(expected, dict):
            raise MaintenanceError(f"manifest has no tables for store {store_name!r}")
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            raise MaintenanceError(f"backup store unreadable: {filename}: {exc}") from exc
        try:
            actual = extract_tables(conn)
        except sqlite3.Error as exc:
            raise MaintenanceError(f"backup store unreadable: {filename}: {exc}") from exc
        finally:
            conn.close()
        if set(actual) != set(expected):
            raise MaintenanceError(
                f"backup store {store_name} has tables {sorted(actual)} "
                f"but the manifest records {sorted(expected)}"
            )
        for table_name, info in actual.items():
            recorded = expected[table_name]
            digest = recorded.get("sha256") if isinstance(recorded, dict) else None
            if info["sha256"] != digest:
                raise MaintenanceError(
                    f"backup store {store_name}.{table_name} does not match its "
                    "recorded digest"
                )


def remove_sidecars(db_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass


def aside_path(root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = root.parent / f"{root.name}.pre-rollback-{stamp}"
    counter = 1
    while candidate.exists():
        candidate = root.parent / f"{root.name}.pre-rollback-{stamp}-{counter}"
        counter += 1
    return candidate


__all__ = [
    "FORMAT",
    "MANIFEST_NAME",
    "RECORDS_NAME",
    "SOURCES_DIR",
    "STORE_SPECS",
    "MaintenanceError",
    "StoreSpec",
    "aside_path",
    "copy_store",
    "decode_value",
    "encode_value",
    "extract_tables",
    "file_sha256",
    "list_tables",
    "load_manifest",
    "load_records",
    "manifest_entry",
    "read_json",
    "read_table",
    "remove_sidecars",
    "table_digest",
    "verify_manifest_files",
    "verify_record_digests",
    "verify_store_contents",
    "write_json",
]
