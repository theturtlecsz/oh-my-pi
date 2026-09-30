"""OMP-414: PostgreSQL integration tests for owner signature verification on decision records."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import (
    NAMESPACE,
    decision_signature_message,
)
from omp_work.v1.store import PostgresWorkStore
from omp_work.v1.store_shared import WorkStoreError
from test_workflow_service import _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)


def _payload(
    decision_id: UUID,
    project_id: UUID,
    *,
    action_class: str | None = "contract_hash",
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": "OMP-414",
        "question": "Apply contract hash update?",
        "why_it_matters": "The contract changes how decisions are approved.",
        "risk_of_delay": "Pipeline halts until contract is approved.",
        "options": ["approve", "reject"],
        "evidence_refs": ["receipt:contract-hash-123"],
        "default_if_any": "reject",
        "risk_of_each_choice": {
            "approve": "New rules become binding immediately.",
            "reject": "Work stalls.",
        },
        "action_class": action_class,
        "resume_state": "contract-approved",
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


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes, *, namespace: str = NAMESPACE) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", namespace],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def test_decision_signature_store_lifecycle(service, tmp_path: Path) -> None:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    project_id = uuid4()
    decision_id = uuid4()

    owner_key = _generate_key(tmp_path, "owner_key")
    other_key = _generate_key(tmp_path, "other_key")
    pubkey = Path(f"{owner_key}.pub")

    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        f"owner {pubkey.read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )

    try:
        # Create a decision with action_class "contract_hash"
        body = _payload(decision_id, project_id, action_class="contract_hash")
        create_receipt, create_result = store.execute(
            _envelope(workspace_id, {"type": "create_decision", "payload": body}),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.mutate",
        )
        assert create_receipt.state == OperationState.APPLIED
        assert create_result["status"] == "pending"

        # 1. Answer without signature -> approval_required ("owner_signature_required"), still pending
        with pytest.raises(WorkStoreError) as exc_missing:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {"decision_id": str(decision_id), "answer": "approve"},
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_missing.value.code == "approval_required"
        assert exc_missing.value.diagnostics == ("owner_signature_required",)

        pending_after_missing = store.decisions(
            workspace_id, actor_id, status="pending"
        )["decisions"]
        assert any(d["decision_id"] == str(decision_id) for d in pending_after_missing)

        # 2a. Signature over a different answer ("reject") -> approval_required ("owner_signature_invalid")
        wrong_answer_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="reject",
        )
        wrong_answer_sig = _sign(owner_key, wrong_answer_msg)
        with pytest.raises(WorkStoreError) as exc_wrong_answer:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                            "owner_signature": wrong_answer_sig,
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_wrong_answer.value.code == "approval_required"
        assert exc_wrong_answer.value.diagnostics == ("owner_signature_invalid",)

        # 2b. Signature by another key -> approval_required ("owner_signature_invalid")
        valid_msg = decision_signature_message(
            workspace_id=workspace_id,
            decision_id=decision_id,
            action_class="contract_hash",
            answer="approve",
        )
        other_key_sig = _sign(other_key, valid_msg)
        with pytest.raises(WorkStoreError) as exc_wrong_key:
            store.execute(
                _envelope(
                    workspace_id,
                    {
                        "type": "answer_decision",
                        "payload": {
                            "decision_id": str(decision_id),
                            "answer": "approve",
                            "owner_signature": other_key_sig,
                        },
                    },
                ),
                actor_id=actor_id,
                actor_kind="owner",
                required_scope="work.approve",
            )
        assert exc_wrong_key.value.code == "approval_required"
        assert exc_wrong_key.value.diagnostics == ("owner_signature_invalid",)

        pending_after_invalid = store.decisions(
            workspace_id, actor_id, status="pending"
        )["decisions"]
        assert any(d["decision_id"] == str(decision_id) for d in pending_after_invalid)

        # 3. Valid signature -> applied, answered
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
                    },
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert answer_receipt.state == OperationState.APPLIED
        assert answer_result == {
            "type": "answer_decision",
            "decision_id": str(decision_id),
            "mission_id": "OMP-414",
            "answer": "approve",
            "resume_state": "contract-approved",
        }

        pending_after_applied = store.decisions(
            workspace_id, actor_id, status="pending"
        )["decisions"]
        assert not any(d["decision_id"] == str(decision_id) for d in pending_after_applied)

        answered = store.decisions(workspace_id, actor_id, status="answered")["decisions"]
        assert any(
            d["decision_id"] == str(decision_id) and d["answer"] == "approve"
            for d in answered
        )

        # 4. Decision with action_class None is answered without a signature
        none_decision_id = uuid4()
        body_none = _payload(none_decision_id, project_id, action_class=None)
        store.execute(
            _envelope(workspace_id, {"type": "create_decision", "payload": body_none}),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.mutate",
        )
        none_receipt, none_result = store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {
                        "decision_id": str(none_decision_id),
                        "answer": "approve",
                    },
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
        assert none_receipt.state == OperationState.APPLIED
        assert none_result["type"] == "answer_decision"
        assert none_result["decision_id"] == str(none_decision_id)
        assert none_result["answer"] == "approve"

    finally:
        if signers_path.exists():
            signers_path.unlink()


def test_tier3_answer_refused_when_signers_file_missing(service, tmp_path: Path) -> None:
    """A missing owner_allowed_signers file is not a bypass.

    verify_owner_signature returns false when the file is absent, and a
    missing signature is refused before that call. Either way the decision
    stays pending.
    """
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _grant(service, workspace_id)
    actor_id = uuid4()
    project_id = uuid4()
    decision_id = uuid4()

    signers_path = service.config.config_dir / "owner_allowed_signers"
    if signers_path.exists():
        signers_path.unlink()
    assert not signers_path.is_file()

    body = _payload(decision_id, project_id, action_class="contract_hash")
    create_receipt, _create_result = store.execute(
        _envelope(workspace_id, {"type": "create_decision", "payload": body}),
        actor_id=actor_id,
        actor_kind="owner",
        required_scope="work.mutate",
    )
    assert create_receipt.state == OperationState.APPLIED

    with pytest.raises(WorkStoreError) as exc_missing:
        store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {"decision_id": str(decision_id), "answer": "approve"},
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
    assert exc_missing.value.code == "approval_required"
    assert exc_missing.value.diagnostics == ("owner_signature_required",)

    pending = store.decisions(workspace_id, actor_id, status="pending")["decisions"]
    assert any(d["decision_id"] == str(decision_id) for d in pending)

    key = _generate_key(tmp_path, "unregistered_key")
    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class="contract_hash",
        answer="approve",
    )
    signature = _sign(key, message)
    assert not signers_path.is_file()
    with pytest.raises(WorkStoreError) as exc_invalid:
        store.execute(
            _envelope(
                workspace_id,
                {
                    "type": "answer_decision",
                    "payload": {
                        "decision_id": str(decision_id),
                        "answer": "approve",
                        "owner_signature": signature,
                    },
                },
            ),
            actor_id=actor_id,
            actor_kind="owner",
            required_scope="work.approve",
        )
    assert exc_invalid.value.code == "approval_required"
    assert exc_invalid.value.diagnostics == ("owner_signature_invalid",)

    pending_after = store.decisions(workspace_id, actor_id, status="pending")["decisions"]
    assert any(d["decision_id"] == str(decision_id) for d in pending_after)
    answered = store.decisions(workspace_id, actor_id, status="answered")["decisions"]
    assert not any(d["decision_id"] == str(decision_id) for d in answered)
