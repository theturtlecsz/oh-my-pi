"""OMP-424-s03: the workspace controller moves from Grok Bot to a second client.

One world, one server process. Grok Bot and the second client are both minted
as ``actor_kind`` client capabilities; Grok Bot is designated first. The Grok
Bot journey and the second client journey each print an ``identity`` — the
service fingerprint and the contract sha256 — which must agree, because the
controller moves by configuration and the owner signature alone and changes
neither the service nor the contract. Only ``owner-controller.json`` differs
between the two config-directory snapshots, the runs yield the same step
outcomes and final states, and the relay authority follows the designation:
before it the second client's unsigned pause is refused 403, after it Grok
Bot's is refused the same way and the second client's is applied.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work.v1.models import (
    ApproveMissionCommand,
    ApproveMissionPayload,
    CommandEnvelope,
    ItemBudget,
    MissionDraft,
    SetMissionStatusCommand,
    SetMissionStatusPayload,
    SubmitMissionCommand,
    SubmitMissionPayload,
)
from omp_work.v1.owner_controller import designated_controller
from second_client_world import Capability, World, world

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

_BUDGET = ItemBudget(
    usd="5.00",
    tokens=100,
    wall_clock_seconds=600,
    max_subagents=1,
)


def _snapshot(config_dir: Path) -> dict[str, str]:
    """sha256 of every regular file under ``config_dir``, keyed by relative path."""
    return {
        str(path.relative_to(config_dir)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(config_dir.rglob("*"))
        if path.is_file()
    }


def _identity(w: World, capability: Capability) -> dict[str, object]:
    code, body = w.client(capability, "identity")
    assert code == 0, body
    return body


def _envelope(w: World, command: object) -> CommandEnvelope:
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=w.workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )


def _running_mission(w: World, objective: str) -> UUID:
    """Plant an approved, running mission for the unsigned-pause relay probes."""
    mission_id = uuid4()
    w.platform.execute(
        _envelope(
            w,
            SubmitMissionCommand(
                type="submit_mission",
                payload=SubmitMissionPayload(
                    mission_id=mission_id,
                    draft=MissionDraft(
                        project_id=w.project_id,
                        objective=objective,
                        risk_policy="risk-parent",
                        approval_policy="approval-parent",
                        effort_policy="effort-parent",
                        budget_policy=_BUDGET,
                    ),
                ),
            ),
        )
    )
    w.platform.execute(
        _envelope(
            w,
            ApproveMissionCommand(
                type="approve_mission",
                payload=ApproveMissionPayload(
                    mission_id=mission_id,
                    revision=1,
                    basis_kind="decision",
                    basis_id=str(uuid4()),
                ),
            ),
        )
    )
    w.platform.execute(
        _envelope(
            w,
            SetMissionStatusCommand(
                type="set_mission_status",
                payload=SetMissionStatusPayload(
                    mission_id=mission_id,
                    target_status="running",
                    cause_kind="principal",
                ),
            ),
        )
    )
    return mission_id


def _pause(w: World, capability: Capability, mission_id: UUID) -> tuple[int, dict[str, object]]:
    return w.client(capability, "pause", "--mission-id", str(mission_id))


def _refused_not_controller(code: int, body: dict[str, object]) -> bool:
    return code == 1 and body.get("error", {}).get("code") == "not_designated_controller"


def _steps(journey: list[tuple[str, dict[str, object], str | None]]):
    """The externally visible shape of a journey: (step, outcome, state) per step."""
    return [(name, body.get("outcome"), state) for name, body, state in journey]


def _states(journey: list[tuple[str, dict[str, object], str | None]], step: str):
    return [state for name, _body, state in journey if name == step]


def _retarget_config(capability: Capability, mission_id: UUID) -> None:
    """Point a minted client config at another mission (the config, not the world)."""
    data = json.loads(capability.config_path.read_text(encoding="utf-8"))
    data["mission_id"] = str(mission_id)
    capability.config_path.write_text(json.dumps(data), encoding="utf-8")


def test_controller_moves_by_configuration_and_owner_signature(tmp_path: Path) -> None:
    with world(tmp_path) as w:
        # 1. Both clients are minted before any designation.
        grokbot = w.mint("grokbot")
        second = w.mint("second-client")
        w.designate(grokbot.actor_id)

        # A. Before the move, the designation names Grok Bot and its unsigned
        # pause applies, while the second client's is refused 403.
        before = _running_mission(w, "Probe the Grok Bot designation")
        code, body = _pause(w, grokbot, before)
        assert code == 0, body
        assert body["outcome"] == "applied"
        assert body["result"]["mission"]["status"] == "paused"
        assert _refused_not_controller(*_pause(w, second, before))

        # 2. Grok Bot runs the whole journey.
        j1 = w.run_journey(grokbot)
        i1 = _identity(w, grokbot)

        # 3. The snapshots bracket the owner's re-designation.
        s1 = _snapshot(w.config.config_dir)
        assert designated_controller(w.config.config_dir, w.workspace_id) == grokbot.actor_id
        w.designate(second.actor_id)
        s2 = _snapshot(w.config.config_dir)
        assert designated_controller(w.config.config_dir, w.workspace_id) == second.actor_id

        changed = {name for name in s1.keys() | s2.keys() if s1.get(name) != s2.get(name)}
        assert changed == {"owner-controller.json"}

        # B. After the move, the roles are exactly swapped: Grok Bot's unsigned
        # pause is refused 403 and the second client's is applied.
        after = _running_mission(w, "Probe the second-client designation")
        assert _refused_not_controller(*_pause(w, grokbot, after))
        code, body = _pause(w, second, after)
        assert code == 0, body
        assert body["outcome"] == "applied"
        assert body["result"]["mission"]["status"] == "paused"

        # 4. The second client runs the same journey over its own new mission.
        w.mission_id = uuid4()
        _retarget_config(second, w.mission_id)
        j2 = w.run_journey(second)
        i2 = _identity(w, second)

    # Identity is the service and the contract, not the controller.
    assert i1["service_fingerprint"] == i2["service_fingerprint"]
    assert i1["contract_sha256"] == i2["contract_sha256"]

    # Both runs reach the same outcomes and the same final state.
    assert _steps(j1) == _steps(j2)
    assert _states(j1, "status") == ["running", "completed"]
    assert _states(j2, "status") == ["running", "completed"]

    # The second client is a distinct actor from Grok Bot and the owner.
    assert second.actor_id != grokbot.actor_id
    assert second.actor_id != w.owner_id

    # Its capability is a client capability, not an owner capability, and its
    # fixture config names only its own capability file.
    capability = json.loads(second.path.read_text(encoding="utf-8"))
    assert capability["actor_kind"] == "client"
    assert capability["actor_id"] == str(second.actor_id)
    assert second.path.name != "owner.json"
    assert second.actor_id != w.owner_id
    config = json.loads(second.config_path.read_text(encoding="utf-8"))
    assert config["capability"] == str(second.path)
    assert "grokbot" not in json.dumps(config)
