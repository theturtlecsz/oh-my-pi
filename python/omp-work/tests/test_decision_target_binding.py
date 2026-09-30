"""Unit checks for optional target and expiry fields on the owner signature message."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from omp_work.v1.decision_records import find_decision
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import (
    NAMESPACE,
    decision_signature_message,
    verify_owner_signature,
)
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from test_workflow_service import _grant

pytest_plugins = ["test_workflow_service"]

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


def _targeted_payload(
    decision_id: UUID,
    project_id: UUID,
    *,
    target_sha256: str = _TARGET_A,
    action_class: str | None = "contract_hash",
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": "OMP-403",
        "question": "Apply this target?",
        "why_it_matters": "The approval must name the target it covers.",
        "risk_of_delay": "Pipeline halts until answered.",
        "options": ["approve", "reject"],
        "evidence_refs": ["receipt:target-binding-123"],
        "default_if_any": "reject",
        "risk_of_each_choice": {
            "approve": "New rules become binding.",
            "reject": "Work stalls.",
        },
        "action_class": action_class,
        "target_sha256": target_sha256,
        "resume_state": "target-approved",
    }


def _envelope(workspace_id: UUID, command: dict[str, object]) -> CommandEnvelope:
    return CommandEnvelope.model_validate(
        {
            "api_version": "work.omp.dev/v1",
            "workspace_id": str(workspace_id),
            "operation_id": str(uuid4()),
            "request_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "command": command,
        }
    )


@pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)
def test_decision_target_binding_store_lifecycle(service, tmp_path: Path) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    project_id = uuid4()
    decision_id = uuid4()

    owner_key = _generate_key(tmp_path, "owner_key")
    pubkey = Path(f"{owner_key}.pub")

    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        f"owner {pubkey.read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )

    try:
        body = _targeted_payload(decision_id, project_id, target_sha256=_TARGET_A)
        create_receipt, create_result = store.execute(
            _envelope(workspace_id, {"type": "create_decision", "payload": body}),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.mutate",
        )
        assert create_receipt.state == OperationState.APPLIED
        assert create_result["decision_id"] == str(decision_id)

        with store._transaction(workspace_id, actor_id) as cur:
            pending_view = find_decision(cur, workspace_id, decision_id)
            assert pending_view is not None
            assert pending_view["target_sha256"] == _TARGET_A
            assert pending_view["expires_at"] is None

            # Unknown id returns None
            assert find_decision(cur, workspace_id, uuid4()) is None
            assert find_decision(cur, workspace_id, str(uuid4())) is None

        # Targeted decision answered without expires_at -> expires_at_required
        with pytest.raises(WorkStoreError) as exc_missing_expiry:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_missing_expiry.value.code == "approval_required"
        assert exc_missing_expiry.value.diagnostics == ("expires_at_required",)

        future_expiry = datetime(2099, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # Signature over another target -> owner_signature_invalid
        wrong_target_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
            target_sha256=_TARGET_B,
            expires_at=future_expiry,
        )
        wrong_target_sig = _sign(owner_key, wrong_target_msg)
        with pytest.raises(WorkStoreError) as exc_wrong_target:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                            "owner_signature": wrong_target_sig,
                            "expires_at": future_expiry.isoformat(),
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_wrong_target.value.code == "approval_required"
        assert exc_wrong_target.value.diagnostics == ("owner_signature_invalid",)

        # Signature over another expiry -> owner_signature_invalid
        other_expiry = datetime(2099, 1, 2, 12, 0, 0, tzinfo=timezone.utc)
        wrong_expiry_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
            target_sha256=_TARGET_A,
            expires_at=other_expiry,
        )
        wrong_expiry_sig = _sign(owner_key, wrong_expiry_msg)
        with pytest.raises(WorkStoreError) as exc_wrong_expiry:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                            "owner_signature": wrong_expiry_sig,
                            "expires_at": future_expiry.isoformat(),
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_wrong_expiry.value.code == "approval_required"
        assert exc_wrong_expiry.value.diagnostics == ("owner_signature_invalid",)

        # Past expires_at -> authorization_expired
        past_expiry = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        past_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
            target_sha256=_TARGET_A,
            expires_at=past_expiry,
        )
        past_sig = _sign(owner_key, past_msg)
        with pytest.raises(WorkStoreError) as exc_past:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                            "owner_signature": past_sig,
                            "expires_at": past_expiry.isoformat(),
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_past.value.code == "approval_required"
        assert exc_past.value.diagnostics == ("authorization_expired",)

        # Valid answer -> find_decision returns target_sha256 and expires_at
        valid_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
            target_sha256=_TARGET_A,
            expires_at=future_expiry,
        )
        valid_sig = _sign(owner_key, valid_msg)
        answer_receipt, answer_result = store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {
                        "decision_id": str(decision_id),
                        "answer": "approve",
                        "owner_signature": valid_sig,
                        "expires_at": future_expiry.isoformat(),
                    },
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert answer_receipt.state == OperationState.APPLIED
        assert answer_result["type"] == "answer_decision"
        assert answer_result["decision_id"] == str(decision_id)
        assert (
            datetime.fromisoformat(answer_result["expires_at"])
            == future_expiry
        )

        with store._transaction(workspace_id, actor_id) as cur:
            answered_view = find_decision(cur, workspace_id, decision_id)
            assert answered_view is not None
            assert answered_view["status"] == "answered"
            assert answered_view["target_sha256"] == _TARGET_A
            assert (
                datetime.fromisoformat(str(answered_view["expires_at"]))
                == future_expiry
            )
            # Both str and UUID lookup work
            assert find_decision(cur, workspace_id, str(decision_id)) == answered_view

    finally:
        if signers_path.exists():
            signers_path.unlink()
