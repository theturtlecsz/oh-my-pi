"""OMP-430-s01: Relayed stop release and client oversight on PostgreSQL.

Contracts tested:
- With stop engaged, an unsigned release relay from the designated controller is approval_required ("owner_signature_required") and stop stays.
- A badly signed release relay from the designated controller is approval_required ("owner_signature_invalid") and stop stays.
- An owner-signed release relay from the designated controller releases the stop (status stopped=False, reason=instruction.text).
- owner_approval_attempt signal applies while stopped.
- Other mutations and non-release relay intents remain refused while stopped (agent_stop_engaged).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from omp_work.v1.models import (
    CommandEnvelope,
    RelayOwnerIntentPayload,
    RelayedInstruction,
)
from omp_work.v1.owner_controller import (
    designation_message,
    write_designation,
)
from omp_work.v1.owner_signature import NAMESPACE, relay_signature_message
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from test_workflow_service import _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_SOURCE = "thread:1712345678.000100"
_RECEIVED_AT = "2026-10-01T00:00:00+00:00"


def _instruction(text: str) -> dict[str, object]:
    return {
        "text": text,
        "source_message_ref": _SOURCE,
        "owner_authored": True,
        "received_at": _RECEIVED_AT,
    }


def _envelope(workspace_id, command: dict[str, object]) -> CommandEnvelope:
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


def _generate_key(tmp_path: Path) -> Path:
    key = tmp_path / "owner"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def test_stop_release_relay_and_alarm_signal_while_stopped(service, tmp_path: Path) -> None:
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen is not installed")

    workspace_id = uuid4()
    _grant(service, workspace_id)
    store = PostgresWorkStore(service.config)

    # 1. Setup owner key and allowed signers
    key = _generate_key(tmp_path)
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    pub_key_content = Path(f"{key}.pub").read_text(encoding="utf-8").strip()
    (service.config.config_dir / "owner_allowed_signers").write_text(
        f"owner {pub_key_content}\n",
        encoding="utf-8",
    )

    # 2. Setup designated controller
    controller_id = uuid4()
    desig_msg = designation_message(workspace_id, controller_id)
    desig_sig = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=desig_msg,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")
    write_designation(service.config.config_dir, workspace_id, controller_id, desig_sig)

    # 3. Engage stop
    engage_cmd = {
        "type": "engage_stop",
        "payload": {"reason": "safety threshold breached: excessive token draw"},
    }
    _receipt, engage_res = store.execute(
        _envelope(workspace_id, engage_cmd),
        actor_id=uuid4(),
        actor_kind="owner",
        required_scope="work.stop",
    )
    assert engage_res["stopped"] is True
    initial_status = store.stop_status(workspace_id, controller_id)
    assert initial_status["stopped"] is True
    assert initial_status["reason"] == "safety threshold breached: excessive token draw"

    # 4. Unsigned release relay from designated controller -> approval_required (owner_signature_required)
    unsigned_relay = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "release_stop",
            "instruction": _instruction("Release stop unsigned."),
            "owner_signature": None,
        },
    }
    with pytest.raises(WorkStoreError) as req_err:
        store.execute(
            _envelope(workspace_id, unsigned_relay),
            actor_id=controller_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert req_err.value.code == "approval_required"
    assert req_err.value.diagnostics == ("owner_signature_required",)

    # Stop stays engaged
    status = store.stop_status(workspace_id, controller_id)
    assert status["stopped"] is True
    assert status["reason"] == "safety threshold breached: excessive token draw"

    # 5. Badly signed release relay from designated controller -> approval_required (owner_signature_invalid)
    bad_sig_relay = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "release_stop",
            "instruction": _instruction("Release stop with bogus signature."),
            "owner_signature": "invalid-signature-bytes",
        },
    }
    with pytest.raises(WorkStoreError) as inv_err:
        store.execute(
            _envelope(workspace_id, bad_sig_relay),
            actor_id=controller_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert inv_err.value.code == "approval_required"
    assert inv_err.value.diagnostics == ("owner_signature_invalid",)

    # Stop stays engaged
    status = store.stop_status(workspace_id, controller_id)
    assert status["stopped"] is True

    # Other relayed intents (e.g. pause) are refused while stopped with agent_stop_engaged
    unrelated_relay = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "pause",
            "mission_id": str(uuid4()),
            "instruction": _instruction("Pause mission."),
            "owner_signature": None,
        },
    }
    with pytest.raises(WorkStoreError) as stop_refused:
        store.execute(
            _envelope(workspace_id, unrelated_relay),
            actor_id=controller_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert stop_refused.value.code == "agent_stop_engaged"

    # 6. owner_approval_attempt signal applies while stopped
    alarm_cmd = {
        "type": "record_alarm_signal",
        "payload": {
            "signal": "owner_approval_attempt",
            "subject": "Owner prompted for approval during stopped state",
            "detail": "Relay received but requires cryptographic release",
        },
    }
    receipt, alarm_res = store.execute(
        _envelope(workspace_id, alarm_cmd),
        actor_id=controller_id,
        actor_kind="client",
        required_scope="work.mutate",
    )
    assert receipt.state.value == "applied"
    assert alarm_res["signal"] == "owner_approval_attempt"
    assert alarm_res["subject"] == "Owner prompted for approval during stopped state"

    # Stop still stays engaged
    assert store.stop_status(workspace_id, controller_id)["stopped"] is True

    # 7. Owner-signed release relay releases the stop
    release_text = "Owner signed instruction: resumed by authorized controller."
    unsigned_payload = RelayOwnerIntentPayload.model_validate(
        {
            "intent": "release_stop",
            "instruction": _instruction(release_text),
        }
    )
    sign_msg = relay_signature_message(workspace_id, unsigned_payload)
    sig = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=sign_msg,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")

    signed_relay = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "release_stop",
            "instruction": _instruction(release_text),
            "owner_signature": sig,
        },
    }
    receipt, applied = store.execute(
        _envelope(workspace_id, signed_relay),
        actor_id=controller_id,
        actor_kind="client",
        required_scope="work.client",
    )
    assert receipt.state.value == "applied"
    assert applied["type"] == "relay_owner_intent"
    assert applied["intent"] == "release_stop"
    assert applied["relay"]["signed"] is True
    assert applied["relay"]["relayed_by"] == str(controller_id)
    assert applied["relay"]["instruction"]["text"] == release_text

    # Stop is now released
    released_status = store.stop_status(workspace_id, controller_id)
    assert released_status["stopped"] is False
    assert released_status["reason"] == release_text
    assert released_status["changed_by_actor_kind"] == "client"
