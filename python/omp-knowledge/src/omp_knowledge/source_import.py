"""Resumable import of one repository snapshot from retained Enola output.

``import_source`` reads ``facts.jsonl`` and ``receipt.json`` from an Enola
directory, captures the checkout's code manifest with git only (no Postgres),
and retains those three byte strings under
``<state_root>/sources/<repository_id>/<snapshot_id>/``. It then stages and
publishes from the retained bytes. Identical retained bytes are reused; a
re-run after a crash finishes the same snapshot without a second row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess  # nosec B404 - invokes git with a fixed argv and no shell
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import Field

from omp_work.knowledge_contracts import (
    CodeSnapshotManifest,
    ManifestFile,
    RepositoryIdentity,
)
from omp_work.knowledge_namespace import validate_snapshot_id
from omp_work.knowledge_publication import (
    SnapshotPublication,
    StructuralPublicationStore,
)
from omp_work.knowledge_source import (
    KnowledgeSourceError,
    capture_manifest,
    normalize_remote_url,
)
from omp_work.knowledge_structural import parse_enola_receipt
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import StrictModel

FACTS_NAME = "facts.jsonl"
RECEIPT_NAME = "receipt.json"
MANIFEST_NAME = "manifest.json"

EXIT_OK = 0
EXIT_REFUSED = 2


class SourceImportError(Exception):
    """The import was refused before publication."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class ImportResult(StrictModel):
    """Sha256 of each retained file, plus the publication record."""

    facts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    publication: SnapshotPublication


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_uuid(value: UUID | str, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise SourceImportError(
            "invalid_request", f"{name} is not a UUID: {value!r}"
        ) from exc


def _git(args: list[str], root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        raise KnowledgeSourceError("checkout_invalid")
    try:
        proc = subprocess.run(  # nosec B603 - absolute git path, no shell, fixed argv
            [git, *args],
            cwd=root,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise KnowledgeSourceError("checkout_invalid") from exc
    return proc.stdout


def _identity_from_checkout(
    workspace_id: UUID, repository_id: UUID, checkout: Path
) -> RepositoryIdentity:
    """Build a repository identity from the checkout's origin and root commits.

    Git only: the caller supplies the ids. This does not query Postgres.
    """
    root = Path(checkout).resolve()
    toplevel = os.path.realpath(_git(["rev-parse", "--show-toplevel"], root).strip())
    if toplevel != os.path.realpath(root):
        raise KnowledgeSourceError("checkout_invalid")
    origin = _git(["remote", "get-url", "origin"], root).strip()
    if not origin:
        raise KnowledgeSourceError("checkout_invalid")
    root_commits = [
        line.strip()
        for line in _git(["rev-list", "--max-parents=0", "HEAD"], root).splitlines()
        if line.strip()
    ]
    if not root_commits:
        raise KnowledgeSourceError("checkout_invalid")
    for sha in root_commits:
        _git(["cat-file", "-e", f"{sha}^{{commit}}"], root)
    return RepositoryIdentity(
        workspace_id=workspace_id,
        repository_id=repository_id,
        canonical_remote_url=normalize_remote_url(origin),
        root_commits=tuple(root_commits),
        verified_at=datetime.now(timezone.utc),
    )


def _read_enola(enola_dir: Path) -> tuple[bytes, bytes]:
    receipt_path = enola_dir / RECEIPT_NAME
    facts_path = enola_dir / FACTS_NAME
    receipt = receipt_path.read_bytes() if receipt_path.is_file() else b""
    parse_enola_receipt(receipt)
    if not facts_path.is_file():
        raise SourceImportError("missing_facts", "facts.jsonl is missing")
    return facts_path.read_bytes(), receipt


def _snapshot_dir(state_root: Path, repository_id: UUID, snapshot_id: str) -> Path:
    return state_root / "sources" / str(repository_id) / snapshot_id


def _conflict(name: str) -> SourceImportError:
    return SourceImportError(
        "snapshot_conflict",
        f"snapshot_conflict: retained {name} differs for this snapshot",
    )


def _atomic_create(dest: Path, data: bytes) -> None:
    """Write ``data`` via a temp file and ``os.replace``. Never replace differing bytes."""
    if dest.exists():
        current = dest.read_bytes() if dest.is_file() else None
        if current != data:
            raise _conflict(dest.name)
        return
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if dest.exists():
            current = dest.read_bytes() if dest.is_file() else None
            if current != data:
                raise _conflict(dest.name)
            return
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()
    if not dest.is_file() or dest.read_bytes() != data:
        raise _conflict(dest.name)


def _retain(directory: Path, payloads: Mapping[str, bytes]) -> dict[str, bytes]:
    """Reuse identical retained bytes. Refuse before writing when any file differs."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        path = directory / name
        if not path.exists():
            continue
        if not path.is_file() or path.read_bytes() != data:
            raise _conflict(name)
    for name, data in payloads.items():
        _atomic_create(directory / name, data)
    retained: dict[str, bytes] = {}
    for name, data in payloads.items():
        path = directory / name
        current = path.read_bytes()
        if current != data:
            raise _conflict(name)
        retained[name] = current
    return retained


def _stage_and_publish(
    state_root: Path,
    *,
    workspace_id: UUID,
    repository_id: UUID,
    snapshot_id: str,
    facts: bytes,
    receipt: bytes,
    manifest_files: Iterable[ManifestFile],
) -> SnapshotPublication:
    """Stage then publish, retrying a lost race with another import of this snapshot.

    Two threads can both observe an empty primary key and one ``INSERT`` then
    loses. The loser retries; the winner's row is reused and published once.
    """
    delay = 0.02
    last: Exception | None = None
    for _ in range(5):
        store = StructuralPublicationStore(state_root)
        try:
            store.stage_enola_snapshot(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
                facts_bytes=facts,
                receipt_bytes=receipt,
                manifest_files=manifest_files,
            )
            return store.publish(
                workspace_id=workspace_id,
                repository_id=repository_id,
                snapshot_id=snapshot_id,
            )
        except sqlite3.IntegrityError as exc:
            last = exc
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
        time.sleep(delay)
        delay *= 2
    raise SourceImportError(
        "publication_busy",
        f"publication_busy: could not publish snapshot {snapshot_id}: {last}",
    )


def import_source(
    state_root: Path | str,
    *,
    workspace_id: UUID | str,
    repository_id: UUID | str,
    snapshot_id: str,
    checkout: Path | str,
    enola_dir: Path | str,
) -> ImportResult:
    """Retain one Enola snapshot and publish it from those retained bytes.

    A second call with the same bytes reuses the retained files and the
    existing publication row. A call that crashes after stage and before
    publish leaves the snapshot invisible; repeating the call publishes it.
    """
    root = Path(state_root)
    workspace = _as_uuid(workspace_id, "workspace_id")
    repository = _as_uuid(repository_id, "repository_id")
    if not isinstance(snapshot_id, str):
        raise SourceImportError(
            "invalid_request", f"snapshot_id is not a string: {snapshot_id!r}"
        )
    try:
        validate_snapshot_id(snapshot_id)
    except ValueError as exc:
        raise SourceImportError("invalid_request", str(exc)) from exc

    facts, receipt = _read_enola(Path(enola_dir))
    identity = _identity_from_checkout(workspace, repository, Path(checkout))
    manifest = capture_manifest(identity, checkout)
    manifest_bytes = canonical_json(manifest.model_dump(mode="json")).encode("utf-8")

    retained = _retain(
        _snapshot_dir(root, repository, snapshot_id),
        {
            FACTS_NAME: facts,
            RECEIPT_NAME: receipt,
            MANIFEST_NAME: manifest_bytes,
        },
    )
    retained_manifest = CodeSnapshotManifest.model_validate_json(
        retained[MANIFEST_NAME]
    )
    publication = _stage_and_publish(
        root,
        workspace_id=workspace,
        repository_id=repository,
        snapshot_id=snapshot_id,
        facts=retained[FACTS_NAME],
        receipt=retained[RECEIPT_NAME],
        manifest_files=retained_manifest.files,
    )
    return ImportResult(
        facts_sha256=_sha256(retained[FACTS_NAME]),
        receipt_sha256=_sha256(retained[RECEIPT_NAME]),
        manifest_sha256=_sha256(retained[MANIFEST_NAME]),
        publication=publication,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m omp_knowledge.source_import")
    parser.add_argument("--state-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--enola-dir", required=True)
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one source import. Refusal exits 2 with the reason on stderr."""
    try:
        args = _build_parser().parse_args(argv)
        result = import_source(
            args.state_root,
            workspace_id=args.workspace,
            repository_id=args.repository_id,
            snapshot_id=args.snapshot_id,
            checkout=args.checkout,
            enola_dir=args.enola_dir,
        )
    except Exception as exc:
        print(str(exc).strip() or type(exc).__name__, file=sys.stderr)
        return EXIT_REFUSED
    if args.json:
        print(
            json.dumps(result.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        )
    else:
        publication = result.publication
        print(
            f"{publication.state} {publication.snapshot_id} "
            f"facts={result.facts_sha256} receipt={result.receipt_sha256} "
            f"manifest={result.manifest_sha256}"
        )
    return EXIT_OK


__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "FACTS_NAME",
    "MANIFEST_NAME",
    "RECEIPT_NAME",
    "ImportResult",
    "SourceImportError",
    "import_source",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
