"""Detect new credentials or SSH keys in watched host locations (OMP-406).

The watcher hashes the bytes of each watched credential file — and each key
line of an ``authorized_keys`` file — then records one ``credential_appeared``
alarm signal per new or changed digest through the WorkService client. Only
paths and digests cross the function boundary: file contents are never
returned, logged, or serialized into a command envelope.

The state file is the previous scan, mapping each key (a path, or
``path#<digest prefix>`` for an ``authorized_keys`` line) to the SHA-256 of its
bytes. It is written atomically with mode 0600, and rewritten after each
applied command so a refused run retries exactly the keys it had not yet
recorded. The operation id is ``uuid5(NAMESPACE_URL, f"omp-credential:{key}:{sha}")``,
which makes a replay of an already-applied signal idempotent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .v1.client import WorkClient
from .v1.models import (
    CommandEnvelope,
    RecordAlarmSignalCommand,
    RecordAlarmSignalPayload,
)

__all__ = ["DEFAULT_ROOTS", "scan", "watch_credentials"]

logger = logging.getLogger(__name__)

DEFAULT_ROOTS: tuple[Path, ...] = (Path.home() / ".ssh",)

API_VERSION = "work.omp.dev/v1"
SIGNAL = "credential_appeared"
AUTHORIZED_KEYS_NAME = "authorized_keys"
IGNORED_NAMES = frozenset({"known_hosts", "known_hosts.old", "config"})
SUBJECT_LIMIT = 200


def scan(roots: Iterable[Path]) -> dict[str, str]:
    """Map each watched credential to the SHA-256 of its bytes.

    Non-recursive: every regular file in each root contributes its own path,
    except the ignored names (``known_hosts``, ``known_hosts.old``, ``config``).
    An ``authorized_keys`` file contributes one entry per non-blank, non-comment
    line instead, keyed ``path#<digest prefix>``. Unreadable roots and files are
    skipped. The returned mapping carries digests only — never file contents.
    """
    found: dict[str, str] = {}
    for root in roots:
        try:
            entries = sorted(Path(root).iterdir())
        except OSError:
            logger.debug("watch: unreadable root %s", root)
            continue
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
            except OSError:
                logger.debug("watch: unreadable entry %s", entry)
                continue
            if entry.name in IGNORED_NAMES:
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                logger.debug("watch: unreadable file %s", entry)
                continue
            if entry.name == AUTHORIZED_KEYS_NAME:
                _scan_authorized_keys(str(entry), data, found)
                continue
            found[str(entry)] = hashlib.sha256(data).hexdigest()
    return found


def _scan_authorized_keys(path: str, data: bytes, found: dict[str, str]) -> None:
    for line in data.split(b"\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        digest = hashlib.sha256(stripped).hexdigest()
        found[f"{path}#{digest[:16]}"] = digest


def watch_credentials(
    client: WorkClient,
    *,
    workspace_id: UUID,
    state_path: Path,
    roots: Iterable[Path] = DEFAULT_ROOTS,
) -> list[str]:
    """Record one ``credential_appeared`` signal per new or changed credential.

    With no state file the current scan is written as the baseline and nothing
    is signalled. Otherwise each key whose digest is new or changed is recorded
    with a stable operation id, and the state is saved after each applied
    command — a client error propagates with that key unsaved, so the next run
    retries it. Keys that vanished from the watch are dropped silently.
    """
    current = scan(roots)
    if not state_path.exists():
        _write_state(state_path, current)
        return []

    previous = _read_state(state_path)
    state = {key: digest for key, digest in previous.items() if key in current}
    signalled: list[str] = []
    for key in sorted(current):
        digest = current[key]
        if state.get(key) == digest:
            continue
        client.execute(_envelope(workspace_id, key, digest))
        state[key] = digest
        _write_state(state_path, state)
        signalled.append(key)
    if state != previous:
        _write_state(state_path, state)
    return signalled


def _envelope(workspace_id: UUID, key: str, digest: str) -> CommandEnvelope:
    return CommandEnvelope(
        api_version=API_VERSION,
        workspace_id=workspace_id,
        operation_id=uuid5(NAMESPACE_URL, f"omp-credential:{key}:{digest}"),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=RecordAlarmSignalCommand(
            type="record_alarm_signal",
            payload=RecordAlarmSignalPayload(
                signal=SIGNAL,
                subject=_subject(key),
                detail=f"sha256:{digest}",
            ),
        ),
    )


def _subject(key: str) -> str:
    home = str(Path.home())
    if key == home:
        shown = "~"
    elif key.startswith(home + os.sep):
        shown = "~" + key[len(home) :]
    else:
        shown = key
    return shown[:SUBJECT_LIMIT]


def _read_state(state_path: Path) -> dict[str, str]:
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in raw.items()}


def _write_state(state_path: Path, state: dict[str, str]) -> None:
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{state_path.name}.", dir=state_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(state, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
