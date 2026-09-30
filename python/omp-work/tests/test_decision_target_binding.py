"""Unit checks for optional target and expiry fields on the owner signature message."""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest

from omp_work.v1.owner_signature import (
    NAMESPACE,
    decision_signature_message,
    verify_owner_signature,
)

_WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
_DECISION_ID = UUID("22222222-2222-2222-2222-222222222222")
_ACTION_CLASS = "tier3"
_ANSWER = "approve"
_TARGET_A = "a" * 64
_TARGET_B = "b" * 64
_EXPIRES_PLUS_2 = datetime(2026, 9, 30, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
_EXPIRES_UTC = "2026-09-30T12:00:00+00:00"
_EXPIRES_OTHER = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
_FOUR_KEYS = (
    b'{"action_class":"tier3","answer":"approve",'
    b'"decision_id":"22222222-2222-2222-2222-222222222222",'
    b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
)


def _message(
    *,
    target_sha256: str | None = None,
    expires_at: datetime | None = None,
) -> bytes:
    return decision_signature_message(
        workspace_id=_WORKSPACE_ID,
        decision_id=_DECISION_ID,
        action_class=_ACTION_CLASS,
        answer=_ANSWER,
        target_sha256=target_sha256,
        expires_at=expires_at,
    )


def test_message_without_new_args_is_four_key_compact_json() -> None:
    assert _message() == _FOUR_KEYS


def test_target_and_expiry_are_sorted_and_expiry_is_utc() -> None:
    expected = (
        b'{"action_class":"tier3","answer":"approve",'
        b'"decision_id":"22222222-2222-2222-2222-222222222222",'
        b'"expires_at":"2026-09-30T12:00:00+00:00",'
        b'"target_sha256":"' + _TARGET_A.encode("ascii") + b'",'
        b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
    )
    message = _message(target_sha256=_TARGET_A, expires_at=_EXPIRES_PLUS_2)
    assert message == expected
    assert _EXPIRES_UTC.encode("ascii") in message
    assert b"+02:00" not in message


def test_only_one_optional_key_is_added() -> None:
    target_only = (
        b'{"action_class":"tier3","answer":"approve",'
        b'"decision_id":"22222222-2222-2222-2222-222222222222",'
        b'"target_sha256":"' + _TARGET_A.encode("ascii") + b'",'
        b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
    )
    expiry_only = (
        b'{"action_class":"tier3","answer":"approve",'
        b'"decision_id":"22222222-2222-2222-2222-222222222222",'
        b'"expires_at":"2026-09-30T12:00:00+00:00",'
        b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
    )
    assert _message(target_sha256=_TARGET_A) == target_only
    assert _message(expires_at=_EXPIRES_PLUS_2) == expiry_only


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _allowed_signers(tmp_path: Path, public_key: Path) -> Path:
    path = tmp_path / "allowed_signers"
    path.write_text(f"owner {public_key.read_text().strip()}\n", encoding="utf-8")
    return path


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


@pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)
def test_signature_for_one_target_rejects_other_target_or_expiry(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    signed = _message(target_sha256=_TARGET_A, expires_at=_EXPIRES_PLUS_2)
    signature = _sign(key, signed)
    assert verify_owner_signature(allowed, signed, signature) is True
    assert (
        verify_owner_signature(
            allowed,
            _message(target_sha256=_TARGET_B, expires_at=_EXPIRES_PLUS_2),
            signature,
        )
        is False
    )
    assert (
        verify_owner_signature(
            allowed,
            _message(target_sha256=_TARGET_A, expires_at=_EXPIRES_OTHER),
            signature,
        )
        is False
    )
