"""OMP-430-s07: stop starts no work on PostgreSQL.

Tests that:
- Sign a tier 3 decision, then a client principal engages stop.
- While stopped:
  - perform with that signed decision is refused agent_stop_engaged, no executor call, no action row;
  - tier 1 perform is refused agent_stop_engaged;
  - owner answer_decision and signed relay answer_decision on another decision are refused, it stays open;
  - relayed resume (unsigned, signed) is refused;
  - stop is still engaged.
- After the owner's release_stop, perform runs the executor once.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.action_classify import classify as classify_action
from omp_work.action_classify import parse_submission
from omp_work.control_actions import perform
from omp_work.v1.decision_records import find_decision
from omp_work.v1.models import (
    CommandEnvelope,
    RelayOwnerIntentPayload,
)
from omp_work.v1.owner_signature import NAMESPACE, relay_signature_message
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from psycopg.rows import dict_row
from test_control_actions import (
    FUTURE,
    NOW,
    REPOS,
    TIER3_SUBMISSIONS,
    _action_rows,
    _answer_decision,
    _Executor,
    _generate_key,
    _mandate_all_tier3,
    _resolver_for,
    _signers_file,
)
from test_mission_status_store import _draft, _project
from test_workflow_service import OWNER, _command

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
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


def _approved_mission(service) -> tuple[UUID, UUID, UUID]:
    workspace_id, project_id = _project(service)
    mission_id = uuid4()
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "submit_mission",
            "payload": {
                "mission_id": str(mission_id),
                "draft": _draft(project_id),
            },
        },
    )
    assert status == 200, body
    status, body = _command(
        service,
        workspace_id,
        {
            "type": "approve_mission",
            "payload": {
                "mission_id": str(mission_id),
                "revision": 1,
                "basis_kind": "decision",
                "basis_id": str(uuid4()),
            },
        },
    )
    assert status == 200, body
    return workspace_id, project_id, mission_id


def test_stop_starts_no_work(service, tmp_path: Path) -> None:
    workspace_id, project_id, mission_id = _approved_mission(service)
    store = PostgresWorkStore(service.config)

    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    _mandate_all_tier3(store, workspace_id, project_id)
    owner_key = _generate_key(tmp_path, "owner_key")
    _signers_file(service, owner_key)
    resolver = _resolver_for(store, workspace_id, project_id)

    # 1. Sign a tier 3 decision (decision_id_1)
    submission_1 = dict(TIER3_SUBMISSIONS["billing_change"])
    op_1 = parse_submission(submission_1)
    classification_1 = classify_action(op_1, resolver(op_1))
    held_1 = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission_1,
        resolver,
        _Executor(),
        NOW,
    )
    assert held_1.status == "held"
    assert held_1.decision_id is not None
    decision_id_1 = held_1.decision_id
    _answer_decision(
        store,
        workspace_id,
        owner_key,
        decision_id_1,
        "billing_change",
        classification_1.target_sha256,
        FUTURE,
    )

    # Also hold another decision (decision_id_2) before stop engages
    submission_2 = dict(TIER3_SUBMISSIONS["broaden_scope"])
    op_2 = parse_submission(submission_2)
    classification_2 = classify_action(op_2, resolver(op_2))
    held_2 = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission_2,
        resolver,
        _Executor(),
        NOW,
    )
    assert held_2.status == "held"
    assert held_2.decision_id is not None
    decision_id_2 = held_2.decision_id

    # 2. Client principal engages stop
    client_id = uuid4()
    engage_cmd = {
        "type": "engage_stop",
        "payload": {"reason": "client principal engagement: stop all work"},
    }
    _receipt, engage_res = store.execute(
        _envelope(workspace_id, engage_cmd),
        actor_id=client_id,
        actor_kind="client",
        required_scope="work.stop",
    )
    assert engage_res["stopped"] is True
    assert store.stop_status(workspace_id, client_id)["stopped"] is True

    # 3. While stopped: perform with it is refused agent_stop_engaged, no executor call, no action row
    rows_before = _action_rows(service, workspace_id)
    stopped_executor = _Executor()
    res_stopped = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission_1,
        resolver,
        stopped_executor,
        NOW,
        decision_id=decision_id_1,
    )
    assert res_stopped.status == "refused"
    assert res_stopped.code == "agent_stop_engaged"
    assert stopped_executor.calls == []
    rows_after = _action_rows(service, workspace_id)
    assert len(rows_after) == len(rows_before)

    # 4. While stopped: tier 1 perform refused
    tier1_submission = {"kind": "read_state"}
    tier1_executor = _Executor()
    tier1_res = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        tier1_submission,
        resolver,
        tier1_executor,
        NOW,
    )
    assert tier1_res.status == "refused"
    assert tier1_res.code == "agent_stop_engaged"
    assert tier1_executor.calls == []

    # 5. While stopped: owner answer_decision and signed relay answer_decision on another decision refused, it stays open
    with pytest.raises(WorkStoreError) as exc_owner_answer:
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            decision_id_2,
            "broaden_scope",
            classification_2.target_sha256,
            FUTURE,
        )
    assert exc_owner_answer.value.code == "agent_stop_engaged"

    relay_answer_payload = RelayOwnerIntentPayload.model_validate(
        {
            "intent": "answer_decision",
            "decision_id": str(decision_id_2),
            "answer": "approve",
            "instruction": _instruction("Signed relay answer."),
        }
    )
    relay_answer_msg = relay_signature_message(workspace_id, relay_answer_payload)
    relay_answer_sig = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(owner_key), "-n", NAMESPACE],
        input=relay_answer_msg,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")
    signed_relay_answer_cmd = {
        "type": "relay_owner_intent",
        "payload": {
            **relay_answer_payload.model_dump(mode="json"),
            "owner_signature": relay_answer_sig,
        },
    }
    with pytest.raises(WorkStoreError) as exc_relay_answer:
        store.execute(
            _envelope(workspace_id, signed_relay_answer_cmd),
            actor_id=client_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert exc_relay_answer.value.code == "agent_stop_engaged"

    # Decision 2 stays open
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        d2 = find_decision(cur, workspace_id, decision_id_2)
        assert d2 is not None
        assert d2["status"] == "pending"

    # 6. While stopped: relayed resume (unsigned, signed) refused
    unsigned_resume_cmd = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "resume",
            "mission_id": str(mission_id),
            "instruction": _instruction("Unsigned resume instruction."),
            "owner_signature": None,
        },
    }
    with pytest.raises(WorkStoreError) as exc_unsigned_resume:
        store.execute(
            _envelope(workspace_id, unsigned_resume_cmd),
            actor_id=client_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert exc_unsigned_resume.value.code == "agent_stop_engaged"

    resume_payload = RelayOwnerIntentPayload.model_validate(
        {
            "intent": "resume",
            "mission_id": str(mission_id),
            "instruction": _instruction("Signed resume instruction."),
        }
    )
    resume_sig_msg = relay_signature_message(workspace_id, resume_payload)
    resume_sig = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(owner_key), "-n", NAMESPACE],
        input=resume_sig_msg,
        capture_output=True,
        check=True,
    ).stdout.decode("ascii")
    signed_resume_cmd = {
        "type": "relay_owner_intent",
        "payload": {
            **resume_payload.model_dump(mode="json"),
            "owner_signature": resume_sig,
        },
    }
    with pytest.raises(WorkStoreError) as exc_signed_resume:
        store.execute(
            _envelope(workspace_id, signed_resume_cmd),
            actor_id=client_id,
            actor_kind="client",
            required_scope="work.client",
        )
    assert exc_signed_resume.value.code == "agent_stop_engaged"

    # 7. Stop still engaged
    assert store.stop_status(workspace_id, client_id)["stopped"] is True

    # 8. After the owner's release_stop that perform runs the executor once
    release_cmd = {
        "type": "release_stop",
        "payload": {"reason": "owner releases stop"},
    }
    store.execute(
        _envelope(workspace_id, release_cmd),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert store.stop_status(workspace_id, client_id)["stopped"] is False

    allowed_executor = _Executor()
    res_resumed = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission_1,
        resolver,
        allowed_executor,
        NOW,
        decision_id=decision_id_1,
    )
    assert res_resumed.status == "done"
    assert len(allowed_executor.calls) == 1
