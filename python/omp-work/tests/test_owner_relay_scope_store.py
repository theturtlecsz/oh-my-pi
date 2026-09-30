"""OMP-416-s05: relay confirm_scope, edit_scope, and decision answers on PostgreSQL.

Tests controller confirm of current revision, stale revision rejection,
broadening detection when adding an unregistered repository (requiring an
owner signature over relay bytes), tier 3 decision signature verification,
and unsigned controller answers for no-class decisions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.v1.canonical import text_sha256
from omp_work.v1.missions import effective_draft
from omp_work.v1.models import CommandEnvelope, MissionStatus, OperationState
from omp_work.v1.owner_controller import designation_message, write_designation
from omp_work.v1.owner_signature import (
    NAMESPACE,
    decision_signature_message,
    relay_signature_message,
)
from omp_work.v1.store import PostgresWorkStore, WorkStoreError
from test_mission_status_store import _get
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1 and ensure ssh-keygen is installed",
)

_TEXT = 'Hold here.\nKeep this exact instruction: "confirm now".'
_SOURCE = "thread:1712345678.000100"
_RECEIVED_AT = "2026-09-30T12:34:56+00:00"


def _instruction(text: str = _TEXT) -> dict[str, object]:
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


def _apply(store: PostgresWorkStore, workspace_id: UUID, actor_id: UUID, command: dict[str, object]):
    return store.execute(
        _envelope(workspace_id, command),
        actor_id=actor_id,
        actor_kind="client",
        required_scope="work.client",
    )


def _generate_key(tmp_path: Path) -> Path:
    key = tmp_path / "owner"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _designate(service, key: Path, workspace_id: UUID, controller_id: UUID) -> None:
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers = service.config.config_dir / "owner_allowed_signers"
    signers.write_text(
        f"owner {Path(f'{key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    write_designation(
        service.config.config_dir,
        workspace_id,
        controller_id,
        _sign(key, designation_message(workspace_id, controller_id)),
    )


def _setup_project_with_repo(
    service, repo_key: str = "repo-a"
) -> tuple[UUID, UUID, PostgresWorkStore]:
    workspace_id = uuid4()
    project_id = uuid4()
    _grant(service, workspace_id)
    with psycopg.connect(
        **service.config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **service.config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(OWNER)),
        )
        cur.execute(
            "INSERT INTO omp_work.projects"
            "(project_id, workspace_id, key, name, kind, provenance)"
            " VALUES (%s, %s, %s, %s, 'surface', %s)",
            (
                project_id,
                workspace_id,
                f"p-{project_id.hex[:8]}",
                "Mission project",
                "{}",
            ),
        )
    store = PostgresWorkStore(service.config)
    store.update_profile(
        workspace_id,
        OWNER,
        project_id,
        repositories=[
            {
                "key": repo_key,
                "name": repo_key,
                "url": f"https://example.test/{repo_key}.git",
                "default_branch": "main",
                "protected_branches": ["main"],
                "automation_ci_secret_free": True,
            }
        ],
    )
    return workspace_id, project_id, store


def _draft_intake(
    store: PostgresWorkStore,
    workspace_id: UUID,
    project_id: UUID,
    mission_id: UUID,
    repositories: tuple[str, ...] = ("repo-a",),
    base_revision: int | None = None,
) -> tuple[dict[str, object], UUID]:
    text = "Bound the memory growth of the cache eviction path."
    intake = {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {
            "id": "goal-1",
            "statement": "Bound memory growth",
            "source_span_ids": [],
        },
        "acceptance_criteria": [
            {
                "id": "ac-1",
                "statement": "RSS <= 256MB",
                "source_span_ids": [],
                "observable_outcome": "RSS <= 256MB",
                "oracle": "automated_test",
            }
        ],
    }
    scope = {
        "project_id": str(project_id),
        "repositories": list(repositories),
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": {
            "usd": "10.00",
            "tokens": 1000,
            "wall_clock_seconds": 3600,
            "max_subagents": 4,
        },
    }
    payload = {
        "mission_id": str(mission_id),
        "base_revision": base_revision,
        "intake": intake,
        "scope": scope,
    }
    receipt, result = store.execute(
        _envelope(workspace_id, {"type": "draft_mission_intake", "payload": payload}),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.mutate",
    )
    assert receipt.state == OperationState.APPLIED
    assert result["outcome"] == "awaiting_owner"
    decision_id = UUID(str(result["decision_id"]))
    return result, decision_id


def _latest_event(
    service, workspace_id: UUID, aggregate_id: UUID, aggregate_type: str = "mission"
) -> dict[str, object]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT event_type, payload FROM omp_audit.domain_events"
            " WHERE workspace_id=%s AND aggregate_id=%s AND aggregate_type=%s"
            " AND outcome='applied' ORDER BY sequence DESC LIMIT 1",
            (workspace_id, aggregate_id, aggregate_type),
        )
        row = cur.fetchone()
    assert row is not None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return {"event_type": row["event_type"], "payload": payload}


def test_controller_confirms_current_revision_approved(
    service, tmp_path: Path
) -> None:
    """Controller confirms current revision -> approved, decision answered, relay provenance stored."""
    key = _generate_key(tmp_path)
    workspace_id, project_id, store = _setup_project_with_repo(service, "repo-a")
    controller_id = uuid4()
    _designate(service, key, workspace_id, controller_id)

    mission_id = uuid4()
    _draft_res, decision_id = _draft_intake(
        store, workspace_id, project_id, mission_id
    )

    instruction_text = "Confirm revision 1."
    command = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "confirm_scope",
            "mission_id": str(mission_id),
            "decision_id": str(decision_id),
            "revision": 1,
            "instruction": _instruction(instruction_text),
        },
    }
    receipt, result = _apply(store, workspace_id, controller_id, command)
    assert receipt.state == OperationState.APPLIED
    assert result["type"] == "relay_owner_intent"
    assert result["intent"] == "confirm_scope"
    assert result["mission"]["status"] == "approved"
    assert result["relay"]["relayed_by"] == str(controller_id)
    assert result["relay"]["signed"] is False
    assert result["relay"]["instruction"]["text"] == instruction_text

    decisions = store.decisions(workspace_id, controller_id)["decisions"]
    assert any(
        d["decision_id"] == str(decision_id) and d["status"] == "answered"
        for d in decisions
    )

    event = _latest_event(service, workspace_id, mission_id, "mission")
    assert event["event_type"] == "answer_mission_draft"
    assert event["payload"]["relay"]["relayed_by"] == str(controller_id)
    assert event["payload"]["relay"]["signed"] is False
    assert event["payload"]["relay"]["instruction"]["text"] == instruction_text
    assert event["payload"]["mission"]["status"] == "approved"

    resp = _get(service.client, workspace_id, mission_id)
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"


def test_confirm_old_revision_after_redraft_revision_conflict(
    service, tmp_path: Path
) -> None:
    """Confirm of an old revision after a redraft -> revision_conflict, unchanged."""
    key = _generate_key(tmp_path)
    workspace_id, project_id, store = _setup_project_with_repo(service, "repo-a")
    controller_id = uuid4()
    _designate(service, key, workspace_id, controller_id)

    mission_id = uuid4()
    _res1, decision_id_1 = _draft_intake(
        store, workspace_id, project_id, mission_id
    )

    _res2, _decision_id_2 = _draft_intake(
        store, workspace_id, project_id, mission_id, base_revision=1
    )

    command = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "confirm_scope",
            "mission_id": str(mission_id),
            "decision_id": str(decision_id_1),
            "revision": 1,
            "instruction": _instruction("Confirm old revision 1."),
        },
    }
    with pytest.raises(WorkStoreError) as exc_info:
        _apply(store, workspace_id, controller_id, command)
    assert exc_info.value.code == "revision_conflict"

    resp = _get(service.client, workspace_id, mission_id)
    assert resp.status_code == 200
    data = resp.json()
    assert data["revision"] == 2
    assert data["status"] == "awaiting_confirmation"


def test_edit_adding_unregistered_repository_broadens_scope(
    service, tmp_path: Path
) -> None:
    """Edit adding an unregistered repository -> approval_required broaden_scope, signed -> applied."""
    key = _generate_key(tmp_path)
    workspace_id, project_id, store = _setup_project_with_repo(service, "repo-a")
    controller_id = uuid4()
    _designate(service, key, workspace_id, controller_id)

    mission_id = uuid4()
    _draft_res, decision_id = _draft_intake(
        store, workspace_id, project_id, mission_id, repositories=("repo-a",)
    )

    from omp_work.v1.api_models import MissionView

    cur_view = MissionView.model_validate(_draft_res["mission"])
    edited_draft = effective_draft(cur_view).model_copy(
        update={"repositories": ("repo-a", "unregistered-repo")}
    )

    unsigned_command = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "edit_scope",
            "mission_id": str(mission_id),
            "decision_id": str(decision_id),
            "revision": 1,
            "draft": edited_draft.model_dump(mode="json"),
            "instruction": _instruction("Add unregistered repo."),
        },
    }
    with pytest.raises(WorkStoreError) as exc_info:
        _apply(store, workspace_id, controller_id, unsigned_command)
    assert exc_info.value.code == "approval_required"
    assert exc_info.value.diagnostics == ("broaden_scope",)

    resp_after = _get(service.client, workspace_id, mission_id)
    assert resp_after.status_code == 200
    assert resp_after.json()["status"] == "awaiting_confirmation"

    unsigned_env = _envelope(workspace_id, unsigned_command)
    sig_msg = relay_signature_message(workspace_id, unsigned_env.command.payload)
    signature = _sign(key, sig_msg)

    signed_command = {
        "type": "relay_owner_intent",
        "payload": {
            **unsigned_command["payload"],
            "owner_signature": signature,
        },
    }
    receipt, result = _apply(store, workspace_id, controller_id, signed_command)
    assert receipt.state == OperationState.APPLIED
    assert result["mission"]["status"] == "approved"
    assert result["relay"]["signed"] is True
    assert (
        "unregistered-repo"
        in result["mission"]["approved_scope"]["envelope"]["repositories"]
    )

    decisions = store.decisions(workspace_id, controller_id)["decisions"]
    assert any(
        d["decision_id"] == str(decision_id) and d["status"] == "answered"
        for d in decisions
    )


def test_tier3_decision_unsigned_from_controller_refused_signed_answered(
    service, tmp_path: Path
) -> None:
    """Tier 3 decision unsigned from controller -> approval_required, signed -> answered."""
    key = _generate_key(tmp_path)
    workspace_id, project_id, store = _setup_project_with_repo(service, "repo-a")
    controller_id = uuid4()
    _designate(service, key, workspace_id, controller_id)

    decision_id = uuid4()
    create_payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "question": "Apply contract hash update?",
        "why_it_matters": "Changes contract.",
        "risk_of_delay": "Stalls.",
        "options": ["approve", "reject"],
        "risk_of_each_choice": {
            "approve": "New rules become binding immediately.",
            "reject": "Work stalls.",
        },
        "action_class": "contract_hash",
    }
    store.execute(
        _envelope(
            workspace_id, {"type": "create_decision", "payload": create_payload}
        ),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.mutate",
    )

    unsigned_command = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "answer_decision",
            "decision_id": str(decision_id),
            "answer": "approve",
            "instruction": _instruction("Approve contract hash."),
        },
    }
    with pytest.raises(WorkStoreError) as exc_info:
        _apply(store, workspace_id, controller_id, unsigned_command)
    assert exc_info.value.code == "approval_required"
    assert exc_info.value.diagnostics == ("owner_signature_required",)

    decisions = store.decisions(workspace_id, controller_id)["decisions"]
    assert any(
        d["decision_id"] == str(decision_id) and d["status"] == "pending"
        for d in decisions
    )

    sig_msg = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class="contract_hash",
        answer="approve",
    )
    signature = _sign(key, sig_msg)

    signed_command = {
        "type": "relay_owner_intent",
        "payload": {
            **unsigned_command["payload"],
            "owner_signature": signature,
        },
    }
    receipt, result = _apply(store, workspace_id, controller_id, signed_command)
    assert receipt.state == OperationState.APPLIED
    assert result["type"] == "relay_owner_intent"
    assert result["intent"] == "answer_decision"
    assert result["answer"] == "approve"
    assert result["decision_id"] == str(decision_id)
    assert result["relay"]["signed"] is True

    decisions_after = store.decisions(workspace_id, controller_id)["decisions"]
    assert any(
        d["decision_id"] == str(decision_id) and d["status"] == "answered"
        for d in decisions_after
    )


def test_no_class_decision_from_controller_answered(
    service, tmp_path: Path
) -> None:
    """No-class decision from controller -> answered."""
    key = _generate_key(tmp_path)
    workspace_id, project_id, store = _setup_project_with_repo(service, "repo-a")
    controller_id = uuid4()
    _designate(service, key, workspace_id, controller_id)

    decision_id = uuid4()
    create_payload = {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "question": "Pick option A or B?",
        "why_it_matters": "Workflow selection.",
        "risk_of_delay": "Stalls.",
        "options": ["A", "B"],
        "risk_of_each_choice": {
            "A": "Risk of option A.",
            "B": "Risk of option B.",
        },
        "action_class": None,
    }
    store.execute(
        _envelope(
            workspace_id, {"type": "create_decision", "payload": create_payload}
        ),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.mutate",
    )

    command = {
        "type": "relay_owner_intent",
        "payload": {
            "intent": "answer_decision",
            "decision_id": str(decision_id),
            "answer": "A",
            "instruction": _instruction("Pick option A."),
        },
    }
    receipt, result = _apply(store, workspace_id, controller_id, command)
    assert receipt.state == OperationState.APPLIED
    assert result["type"] == "relay_owner_intent"
    assert result["intent"] == "answer_decision"
    assert result["answer"] == "A"
    assert result["decision_id"] == str(decision_id)
    assert result["relay"]["signed"] is False
    assert result["relay"]["relayed_by"] == str(controller_id)

    decisions = store.decisions(workspace_id, controller_id)["decisions"]
    assert any(
        d["decision_id"] == str(decision_id) and d["status"] == "answered"
        for d in decisions
    )
