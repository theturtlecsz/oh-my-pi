"""OMP-426-s08: mission intake end to end, driven entirely through WorkClient.

The client reaches the service TestClient transport with an owner bearer and an
agent bearer. Defended contracts:

1. a clear request files one pending owner decision; the owner's confirm approves
   the mission with basis ``decision`` and ``link_mission_work`` then succeeds;
2. an ambiguous request returns clarifying questions and writes no mission or
   decision; a clarified intake for the same mission id then awaits the owner;
3. the owner's reject abandons the mission and ``link_mission_work`` is refused;
4. an agent-filed new scope files the same pending decision, the agent's answer
   is forbidden (403), and the owner's confirm approves the mission;
5. an owner note files a new pending decision and never approves the mission.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import bootstrap
from omp_work.v1.canonical import sha256, text_sha256
from omp_work.v1.client import WorkClient
from omp_work.v1.models import (
    AnswerMissionDraftPayload,
    CommandEnvelope,
    CreateWorkBatchCommand,
    CreateWorkBatchPayload,
    CreateWorkInput,
    DraftMissionIntakePayload,
    LinkMissionWorkCommand,
    LinkMissionWorkPayload,
    OwnerInstruction,
)
from omp_work.v1.server import create_app
from omp_work.v1.service import WorkError
from pg_native import native_postgres, seed_authority
from starlette.testclient import TestClient

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = {
    "usd": "10.00",
    "tokens": 1000,
    "wall_clock_seconds": 3600,
    "max_subagents": 4,
}

_ITEM_BUDGET = {
    "usd": "2.00",
    "tokens": 100,
    "wall_clock_seconds": 60,
    "max_subagents": 1,
}

OWNER_SCOPES = (
    "work.read",
    "work.mutate",
    "work.approve",
    "work.close",
    "work.execute",
)
AGENT_SCOPES = ("work.read", "work.mutate", "work.approve", "work.execute")


def _config(root) -> OperationsConfig:
    credentials = root / "config" / "credentials"
    credentials.mkdir(parents=True, mode=0o700, exist_ok=True)
    for role in (
        "postgres",
        "omp_work_migrator",
        "omp_work_app",
        "omp_work_importer",
        "omp_work_readonly",
        "omp_work_backup",
        "gpg-passphrase",
        "operator-actor-id",
    ):
        path = credentials / role
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])
    return OperationsConfig(
        config_dir=root / "config",
        state_dir=root / "state",
        data_dir=root / "data",
        port=port,
    )


def _bearer(
    directory,
    name: str,
    *,
    token: str,
    actor_id: UUID,
    actor_kind: str,
    workspace_id: UUID,
    scopes: tuple[str, ...],
):
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / name
    path.write_text(
        json.dumps(
            {
                "token": token,
                "actor_id": str(actor_id),
                "actor_kind": actor_kind,
                "workspaces": [str(workspace_id)],
                "scopes": list(scopes),
            }
        )
    )
    path.chmod(0o600)
    return path


def _seed_project(
    config: OperationsConfig, workspace_id: UUID, project_id: UUID
) -> None:
    with psycopg.connect(
        **config.connection_kwargs("postgres"), autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )
    with (
        psycopg.connect(
            **config.connection_kwargs("omp_work_app"), autocommit=True
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT set_config('omp.workspace_id', %s, false),"
            " set_config('omp.actor_id', %s, false)",
            (str(workspace_id), str(uuid4())),
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
                json.dumps({}),
            ),
        )


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("mission-intake-e2e")
    config = _config(root)
    with native_postgres(root, config.port):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "omp_work.operations.database.validate_bundle", lambda **kw: None
        )
        try:
            bootstrap(config)
        finally:
            monkeypatch.undo()

        workspace_id, project_id = uuid4(), uuid4()
        owner_id, agent_id = uuid4(), uuid4()
        seed_authority(config.connection_kwargs("postgres"), workspace_id, owner_id)
        _seed_project(config, workspace_id, project_id)

        capabilities = root / "capabilities"
        owner_bearer = _bearer(
            capabilities,
            "owner.json",
            token="owner-token",
            actor_id=owner_id,
            actor_kind="owner",
            workspace_id=workspace_id,
            scopes=OWNER_SCOPES,
        )
        agent_bearer = _bearer(
            capabilities,
            "agent.json",
            token="agent-token",
            actor_id=agent_id,
            actor_kind="agent",
            workspace_id=workspace_id,
            scopes=AGENT_SCOPES,
        )

        tc = TestClient(create_app(config, capabilities_dir=capabilities))
        yield SimpleNamespace(
            config=config,
            workspace_id=workspace_id,
            project_id=project_id,
            owner=WorkClient(
                "http://testserver", workspace_id, owner_bearer, transport=tc._transport
            ),
            agent=WorkClient(
                "http://testserver", workspace_id, agent_bearer, transport=tc._transport
            ),
        )


def _instruction(text: str = "Confirm this mission draft.") -> OwnerInstruction:
    return OwnerInstruction.model_validate(
        {
            "text": text,
            "provenance": {
                "channel": "owner-chat",
                "message_ref": "msg-426-s08",
                "received_at": "2026-09-30T12:00:00+00:00",
            },
        }
    )


def _intake(
    *,
    criteria: tuple[tuple[str, str, str | None], ...] = (
        ("ac-1", "RSS <= 256MB", "automated_test"),
    ),
) -> dict[str, object]:
    text = "Bound the memory growth of the cache eviction path."
    return {
        "archetype": "small_code_change",
        "source": {"text": text, "sha256": text_sha256(text), "spans": []},
        "goal": {
            "id": "goal-1",
            "statement": "Bound memory growth",
            "source_span_ids": [],
        },
        "acceptance_criteria": [
            {
                "id": criterion_id,
                "statement": outcome,
                "source_span_ids": [],
                "observable_outcome": outcome,
                "oracle": oracle,
            }
            for criterion_id, outcome, oracle in criteria
        ],
    }


def _scope(project_id: UUID, **overrides: object) -> dict[str, object]:
    scope: dict[str, object] = {
        "project_id": str(project_id),
        "repositories": ["repo-a", "repo-b"],
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
        "risk_policy": "risk-parent",
        "approval_policy": "approval-parent",
        "effort_policy": "effort-parent",
        "budget_policy": _BUDGET,
    }
    scope.update(overrides)
    return scope


def _draft(
    world: SimpleNamespace,
    client: WorkClient,
    mission_id: UUID,
    *,
    base_revision: int | None = None,
    intake: dict[str, object] | None = None,
):
    return client.draft_mission_intake(
        DraftMissionIntakePayload.model_validate(
            {
                "mission_id": str(mission_id),
                "base_revision": base_revision,
                "intake": _intake() if intake is None else intake,
                "scope": _scope(world.project_id),
                "instruction": _instruction(),
            }
        )
    )


def _answer(
    client: WorkClient,
    mission_id: UUID,
    decision_id: UUID,
    revision: int,
    answer: dict[str, object],
    *,
    instruction: OwnerInstruction | None = None,
):
    return client.answer_mission_draft(
        AnswerMissionDraftPayload.model_validate(
            {
                "decision_id": str(decision_id),
                "mission_id": str(mission_id),
                "revision": revision,
                "answer": answer,
                "instruction": instruction or _instruction(),
            }
        )
    )


def _create_item(world: SimpleNamespace, client: WorkClient) -> dict[str, object]:
    response = client.execute(
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=world.workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=CreateWorkBatchCommand(
                type="create_work_batch",
                payload=CreateWorkBatchPayload(
                    items=(
                        CreateWorkInput(
                            client_ref="ref-a", title=f"work {uuid4().hex}"
                        ),
                    )
                ),
            ),
        )
    )
    return response.result.items[0].model_dump(mode="json")


def _seed_budget(world: SimpleNamespace, item: dict[str, object]) -> None:
    candidate_id, receipt_id = uuid4(), uuid4()
    work_id, revision_id = UUID(str(item["work_id"])), UUID(str(item["revision_id"]))
    payload = {
        "draft": {"budget": _ITEM_BUDGET},
        "semantic_sha256": "0" * 64,
        "rule_bundle_sha256": "0" * 64,
        "ratified_by": str(uuid4()),
        "assessment_operation_id": str(uuid4()),
        "admission_receipt_id": str(uuid4()),
    }
    with psycopg.connect(**world.config.connection_kwargs("postgres")) as conn:
        conn.execute(
            "INSERT INTO omp_work.candidates(candidate_id,workspace_id,work_id,revision_id,candidate_sha256,commit_sha,kind,allocated_at) VALUES(%s,%s,%s,%s,%s,NULL,'planned',%s)",
            (
                candidate_id,
                world.workspace_id,
                work_id,
                revision_id,
                "e" * 64,
                datetime.now(UTC),
            ),
        )
        conn.execute(
            "INSERT INTO omp_evidence.receipts(receipt_id,workspace_id,work_id,revision_id,candidate_id,kind,payload,payload_sha256,issuer,issued_at,candidate_sha256,candidate_commit) VALUES(%s,%s,%s,%s,%s,'intake_publication',%s,%s,'work-service/bounded-intake',%s,%s,NULL)",
            (
                receipt_id,
                world.workspace_id,
                work_id,
                revision_id,
                candidate_id,
                json.dumps(payload),
                sha256(payload),
                datetime.now(UTC),
                "0" * 64,
            ),
        )


def _link(client: WorkClient, world: SimpleNamespace, mission_id: UUID, work_id: UUID):
    return client.execute(
        CommandEnvelope(
            api_version="work.omp.dev/v1",
            workspace_id=world.workspace_id,
            operation_id=uuid4(),
            request_id=uuid4(),
            correlation_id=uuid4(),
            command=LinkMissionWorkCommand(
                type="link_mission_work",
                payload=LinkMissionWorkPayload(mission_id=mission_id, work_id=work_id),
            ),
        )
    )


def test_clear_request_confirm_approves_and_links(world: SimpleNamespace) -> None:
    mission_id = uuid4()
    drafted = _draft(world, world.owner, mission_id).result
    assert drafted.outcome == "awaiting_owner"
    assert drafted.decision_id is not None
    decision_id = UUID(str(drafted.decision_id))

    page = world.owner.decisions(status="pending", mission_id=str(mission_id))
    matching = [d for d in page.decisions if d.decision_id == decision_id]
    assert len(matching) == 1
    assert matching[0].status == "pending"

    pending = world.owner.mission(mission_id)
    assert pending.status == "awaiting_confirmation"
    assert pending.revision == 1

    answered = _answer(
        world.owner,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "confirm"},
    ).result
    assert answered.outcome == "approved"
    assert answered.mission.status == "approved"
    assert answered.mission.approved_scope is not None
    assert answered.mission.approved_scope.basis_kind == "decision"
    assert answered.mission.approved_scope.basis_id == str(decision_id)

    item = _create_item(world, world.owner)
    _seed_budget(world, item)
    linked = _link(world.owner, world, mission_id, UUID(str(item["work_id"])))
    assert linked.result.type == "link_mission_work"
    assert any(
        link.work_id == UUID(str(item["work_id"]))
        for link in linked.result.mission.links
    )


def test_ambiguous_request_clarifies_then_clarified_awaits_owner(
    world: SimpleNamespace,
) -> None:
    mission_id = uuid4()
    clarified = _draft(
        world,
        world.owner,
        mission_id,
        intake=_intake(criteria=(("ac-1", "RSS <= 256MB", None),)),
    ).result
    assert clarified.outcome == "clarify"
    assert clarified.decision_id is None
    assert clarified.mission is None
    assert len(clarified.questions) == 1
    assert clarified.questions[0].rule_class == "missing_verification_oracle"

    page = world.owner.decisions(status="pending", mission_id=str(mission_id))
    assert page.decisions == ()

    awaited = _draft(world, world.owner, mission_id).result
    assert awaited.outcome == "awaiting_owner"
    assert awaited.decision_id is not None
    assert awaited.mission is not None
    assert awaited.mission.status == "awaiting_confirmation"


def test_reject_abandons_and_link_refused(world: SimpleNamespace) -> None:
    mission_id = uuid4()
    drafted = _draft(world, world.owner, mission_id).result
    decision_id = UUID(str(drafted.decision_id))

    rejected = _answer(
        world.owner,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "reject"},
    ).result
    assert rejected.outcome == "rejected"
    assert world.owner.mission(mission_id).status == "abandoned"

    item = _create_item(world, world.owner)
    _seed_budget(world, item)
    with pytest.raises(WorkError) as refused:
        _link(world.owner, world, mission_id, UUID(str(item["work_id"])))
    assert refused.value.code == "mission_transition_refused"
    assert refused.value.status == 409


def test_agent_files_scope_agent_refused_owner_confirms(world: SimpleNamespace) -> None:
    mission_id = uuid4()
    drafted = _draft(world, world.agent, mission_id).result
    assert drafted.outcome == "awaiting_owner"
    decision_id = UUID(str(drafted.decision_id))

    page = world.owner.decisions(status="pending", mission_id=str(mission_id))
    assert any(d.decision_id == decision_id for d in page.decisions)

    with pytest.raises(WorkError) as refused:
        _answer(
            world.agent,
            mission_id,
            decision_id,
            1,
            {"kind": "option", "option": "confirm"},
        )
    assert refused.value.code == "forbidden"
    assert refused.value.status == 403
    assert world.owner.mission(mission_id).status == "awaiting_confirmation"

    approved = _answer(
        world.owner,
        mission_id,
        decision_id,
        1,
        {"kind": "option", "option": "confirm"},
    ).result
    assert approved.outcome == "approved"
    assert approved.mission.status == "approved"


def test_owner_note_files_new_pending_decision(world: SimpleNamespace) -> None:
    mission_id = uuid4()
    drafted = _draft(world, world.owner, mission_id).result
    decision_id = UUID(str(drafted.decision_id))

    noted = _answer(
        world.owner,
        mission_id,
        decision_id,
        1,
        {"kind": "note", "text": "Please bound cache size instead."},
    ).result
    assert noted.outcome == "noted"
    assert noted.next_decision_id is not None
    next_decision_id = UUID(str(noted.next_decision_id))
    assert next_decision_id != decision_id
    assert noted.mission.status == "awaiting_confirmation"
    assert noted.mission.approved_scope is None
    assert world.owner.mission(mission_id).status != "approved"

    pending = world.owner.decisions(status="pending", mission_id=str(mission_id))
    assert any(d.decision_id == next_decision_id for d in pending.decisions)
