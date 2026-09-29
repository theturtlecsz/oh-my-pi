from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from omp_work.v1.owner_signature import (
    NAMESPACE,
    decision_signature_message,
    verify_owner_signature,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None,
    reason="ssh-keygen is not installed",
)

_WORKSPACE_ID = UUID("11111111-1111-1111-1111-111111111111")
_DECISION_ID = UUID("22222222-2222-2222-2222-222222222222")
_ACTION_CLASS = "tier3"
_ANSWER = "approve"
_EXPECTED_MESSAGE = (
    b'{"action_class":"tier3","answer":"approve",'
    b'"decision_id":"22222222-2222-2222-2222-222222222222",'
    b'"workspace_id":"11111111-1111-1111-1111-111111111111"}'
)


def _message(*, answer: str = _ANSWER) -> bytes:
    return decision_signature_message(
        workspace_id=_WORKSPACE_ID,
        decision_id=_DECISION_ID,
        action_class=_ACTION_CLASS,
        answer=answer,
    )


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


def _sign(key: Path, message: bytes, *, namespace: str = NAMESPACE) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def test_valid_signature_verifies(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    message = _message()
    signature = _sign(key, message)
    assert verify_owner_signature(allowed, message, signature) is True


def test_different_answer_does_not_verify(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    signature = _sign(key, _message())
    assert verify_owner_signature(allowed, _message(answer="deny"), signature) is False


def test_signature_by_second_key_does_not_verify(tmp_path: Path) -> None:
    owner = _generate_key(tmp_path, "owner")
    other = _generate_key(tmp_path, "other")
    allowed = _allowed_signers(tmp_path, Path(f"{owner}.pub"))
    message = _message()
    signature = _sign(other, message)
    assert verify_owner_signature(allowed, message, signature) is False


def test_other_namespace_does_not_verify(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    allowed = _allowed_signers(tmp_path, Path(f"{key}.pub"))
    message = _message()
    signature = _sign(key, message, namespace="other-namespace")
    assert verify_owner_signature(allowed, message, signature) is False


def test_missing_allowed_signers_returns_false(tmp_path: Path) -> None:
    key = _generate_key(tmp_path, "owner")
    message = _message()
    signature = _sign(key, message)
    assert verify_owner_signature(tmp_path / "missing", message, signature) is False


def test_message_bytes_are_sorted_compact_json() -> None:
    assert _message() == _EXPECTED_MESSAGE
