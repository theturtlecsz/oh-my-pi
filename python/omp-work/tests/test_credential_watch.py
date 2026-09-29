"""OMP-406-s06: credential watcher signals new SSH keys as alarm signals.

Contracts defended here:

- the first run only writes the baseline (no command), and a rerun with nothing
  changed signals nothing;
- a new file, a changed file, and an appended ``authorized_keys`` line each
  produce exactly one ``credential_appeared`` envelope whose detail is the
  file's SHA-256;
- file bytes never appear in the envelope JSON;
- the operation id is ``uuid5(NAMESPACE_URL, f"omp-credential:{key}:{sha}")``,
  stable across a retry of the same key+sha;
- a client error leaves that key unsaved for the next run;
- the state file is written with mode 0600.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from omp_work.credential_watch import scan, watch_credentials
from omp_work.v1.models import CommandEnvelope

WORKSPACE = UUID("00000000-0000-7000-8000-000000000010")


class RecordingClient:
    """Captures every envelope handed to ``execute``."""

    def __init__(self) -> None:
        self.envelopes: list[CommandEnvelope] = []

    def execute(self, envelope: CommandEnvelope) -> None:
        self.envelopes.append(envelope)


class FailingClient:
    """Captures the attempted envelope, then refuses it."""

    def __init__(self) -> None:
        self.envelopes: list[CommandEnvelope] = []

    def execute(self, envelope: CommandEnvelope) -> None:
        self.envelopes.append(envelope)
        raise RuntimeError("service unavailable")


def _ssh_dir(tmp_path: Path) -> Path:
    root = tmp_path / "ssh"
    root.mkdir()
    return root


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _envelope_json(envelope: CommandEnvelope) -> str:
    return json.dumps(envelope.model_dump(mode="json"), sort_keys=True)


def test_first_run_writes_baseline_and_signals_nothing(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    _write(root / "id_ed25519", b"PRIVATE KEY BYTES")
    state = tmp_path / "state.json"

    client = RecordingClient()
    assert (
        watch_credentials(
            client, workspace_id=WORKSPACE, roots=(root,), state_path=state
        )
        == []
    )
    assert client.envelopes == []
    assert json.loads(state.read_text()) == scan((root,))
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


def test_new_key_signals_one_envelope(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    state = tmp_path / "state.json"

    watch_credentials(
        RecordingClient(), workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    data = b"ssh-ed25519 AAAA new public key"
    _write(root / "id_ed25519.pub", data)
    client = RecordingClient()
    assert watch_credentials(
        client, workspace_id=WORKSPACE, roots=(root,), state_path=state
    ) == [str(root / "id_ed25519.pub")]

    assert len(client.envelopes) == 1
    command = client.envelopes[0].command
    assert command.type == "record_alarm_signal"
    assert command.payload.signal == "credential_appeared"
    assert command.payload.subject == str(root / "id_ed25519.pub")
    assert command.payload.detail == "sha256:" + hashlib.sha256(data).hexdigest()

    # rerun with nothing changed signals nothing
    rerun = RecordingClient()
    assert (
        watch_credentials(
            rerun, workspace_id=WORKSPACE, roots=(root,), state_path=state
        )
        == []
    )
    assert rerun.envelopes == []


def test_appended_authorized_keys_line_signals_one(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    keys = root / "authorized_keys"
    _write(keys, b"# a comment\nssh-ed25519 AAAA first\n\n")
    state = tmp_path / "state.json"
    watch_credentials(
        RecordingClient(), workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    line = b"ssh-rsa BBBB appended"
    _write(keys, b"# a comment\nssh-ed25519 AAAA first\n\n" + line + b"\n")
    client = RecordingClient()
    signalled = watch_credentials(
        client, workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    assert len(client.envelopes) == 1
    assert len(signalled) == 1
    digest = hashlib.sha256(line).hexdigest()
    assert signalled[0] == f"{keys}#{digest[:16]}"
    assert client.envelopes[0].command.payload.detail == "sha256:" + digest


def test_changed_file_signals_one_and_hides_bytes(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    key = root / "id_ed25519"
    _write(key, b"OLD-BYTES")
    state = tmp_path / "state.json"
    watch_credentials(
        RecordingClient(), workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    secret = b"NEW-SECRET-BYTES"
    _write(key, secret)
    client = RecordingClient()
    assert watch_credentials(
        client, workspace_id=WORKSPACE, roots=(root,), state_path=state
    ) == [str(key)]

    assert len(client.envelopes) == 1
    payload = _envelope_json(client.envelopes[0])
    assert "NEW-SECRET-BYTES" not in payload
    assert "OLD-BYTES" not in payload
    assert (
        client.envelopes[0].command.payload.detail
        == "sha256:" + hashlib.sha256(secret).hexdigest()
    )


def test_operation_id_stable_across_retry(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    state = tmp_path / "state.json"
    watch_credentials(
        RecordingClient(), workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    key = root / "id_rsa.pub"
    data = b"ssh-rsa CCCC"
    _write(key, data)
    digest = hashlib.sha256(data).hexdigest()

    failing = FailingClient()
    with pytest.raises(RuntimeError):
        watch_credentials(
            failing, workspace_id=WORKSPACE, roots=(root,), state_path=state
        )

    # the refused key stayed out of the state, so the next run retries it
    assert key.name not in json.dumps(json.loads(state.read_text()))
    retry = RecordingClient()
    assert watch_credentials(
        retry, workspace_id=WORKSPACE, roots=(root,), state_path=state
    ) == [str(key)]

    expected = uuid5(NAMESPACE_URL, f"omp-credential:{key}:{digest}")
    assert failing.envelopes[0].operation_id == expected
    assert retry.envelopes[0].operation_id == expected


def test_vanished_key_dropped_silently(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    gone = root / "id_ed25519.pub"
    _write(gone, b"ssh-ed25519 DDDD")
    state = tmp_path / "state.json"
    watch_credentials(
        RecordingClient(), workspace_id=WORKSPACE, roots=(root,), state_path=state
    )

    gone.unlink()
    client = RecordingClient()
    assert (
        watch_credentials(
            client, workspace_id=WORKSPACE, roots=(root,), state_path=state
        )
        == []
    )
    assert client.envelopes == []
    assert json.loads(state.read_text()) == {}


def test_scan_skips_ignored_and_unreadable(tmp_path: Path) -> None:
    root = _ssh_dir(tmp_path)
    _write(root / "known_hosts", b"host key")
    _write(root / "known_hosts.old", b"host key old")
    _write(root / "config", b"Host *")
    _write(root / "id_ed25519.pub", b"ssh-ed25519 EEEE")

    nested = root / "nested"
    nested.mkdir()
    _write(nested / "id_ed25519.pub", b"ssh-ed25519 NESTED")

    mapping = scan((root,))
    assert set(mapping) == {str(root / "id_ed25519.pub")}
    assert (
        mapping[str(root / "id_ed25519.pub")]
        == hashlib.sha256(b"ssh-ed25519 EEEE").hexdigest()
    )

    missing = tmp_path / "absent"
    assert scan((missing,)) == {}
