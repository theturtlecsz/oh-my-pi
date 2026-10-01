"""OMP-417-s04: caller-supplied hold content for the tier 3 hold path.

``HoldDecision`` seeds the written decision: its ``decision_id`` names the
decision and the envelope operation/request/correlation ids, so a replay is the
store's idempotent command and opens no second decision, and its texts, evidence
refs, and resume state replace the constant hold content. Without a hold the
path is unchanged.

The unit test drives a fake store; the integration test drives Postgres.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_control_actions import (
    _REPO,
    NOW,
    _Executor,
    _mandate_all_tier3,
    _open,
    _resolver_for,
)
from test_decision_records_api import _list
from test_workflow_service import OWNER

from omp_work.action_classify import (
    Operation,
    ResolvedTarget,
    classify,
    parse_submission,
)
from omp_work.control_actions import HoldDecision, perform
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.v1.models import CommandEnvelope
from omp_work.v1.server import create_app

pytest_plugins = ["test_workflow_service"]

_POSTGRES_INTEGRATION = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)

_MERGE = {"kind": "merge", "repository": "repo-alpha", "branch": "main"}


class _FakeStore:
    """Records every envelope and refuses the block with the hold code."""

    def __init__(self) -> None:
        self.envelopes: list[CommandEnvelope] = []

    def request_action(self, *args: object, **kwargs: object) -> UUID | None:
        raise ProjectAuthorityRefused("blocked_owner_signature")

    def execute(
        self,
        envelope: CommandEnvelope,
        *,
        actor_id: UUID,
        actor_kind: str,
        required_scope: str,
    ) -> tuple[object, dict[str, object]]:
        self.envelopes.append(envelope)
        payload = envelope.command.payload
        decision_id = getattr(payload, "decision_id", None)
        return None, {"decision_id": decision_id}

    def decisions(self) -> list[CommandEnvelope]:
        return [e for e in self.envelopes if e.command.type == "create_decision"]


def _hold() -> HoldDecision:
    return HoldDecision(
        decision_id=uuid4(),
        question="Authorize the merge onto main?",
        why_it_matters="The protected branch backs the release train.",
        risk_of_delay="The merge waits on the owner.",
        evidence_refs=("ticket:OMP-417", "receipt:abc"),
        resume_state="tier3:merge_protected_branch",
    )


def _resolver():
    def resolve(operation: Operation) -> ResolvedTarget:
        return ResolvedTarget(
            repository=_REPO,
            commit=operation.commit,
            changed_paths=(),
        )

    return resolve


def test_hold_decision_seeds_the_envelope_and_carries_its_content() -> None:
    store = _FakeStore()
    workspace_id, actor_id, project_id = uuid4(), uuid4(), uuid4()
    resolver = _resolver()
    classification = classify(
        parse_submission(_MERGE), resolver(parse_submission(_MERGE))
    )
    assert classification.action_class == "merge_protected_branch"
    assert classification.tier == 3

    hold = _hold()
    outcome = perform(
        store,
        workspace_id,
        actor_id,
        project_id,
        None,
        _MERGE,
        resolver,
        _Executor(),
        datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        hold=hold,
    )

    assert outcome.status == "held"
    assert outcome.decision_id == hold.decision_id
    envelopes = store.decisions()
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.operation_id == hold.decision_id
    assert envelope.request_id == hold.decision_id
    assert envelope.correlation_id == hold.decision_id
    payload = envelope.command.payload
    assert payload.decision_id == hold.decision_id
    assert payload.question == hold.question
    assert payload.why_it_matters == hold.why_it_matters
    assert payload.risk_of_delay == hold.risk_of_delay
    assert payload.evidence_refs == hold.evidence_refs
    assert payload.resume_state == hold.resume_state
    assert payload.target_sha256 == classification.target_sha256
    assert payload.action_class == classification.action_class
    assert payload.options == ("approve", "decline")
    assert payload.default_if_any == "decline"

    replay = perform(
        store,
        workspace_id,
        actor_id,
        project_id,
        None,
        _MERGE,
        resolver,
        _Executor(),
        datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        hold=hold,
    )
    assert replay.decision_id == hold.decision_id
    assert len(store.decisions()) == 2
    assert store.decisions()[0] == store.decisions()[1]


@_POSTGRES_INTEGRATION
def test_hold_decision_replay_opens_one_decision(service) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    resolver = _resolver_for(store, workspace_id, project_id)
    operation = parse_submission(_MERGE)
    classification = classify(operation, resolver(operation))
    assert classification.action_class == "merge_protected_branch"

    hold = _hold()
    executor = _Executor()
    first = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        _MERGE,
        resolver,
        executor,
        NOW,
        hold=hold,
    )
    assert first.status == "held"
    assert first.decision_id == hold.decision_id
    assert executor.calls == []

    second = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        _MERGE,
        resolver,
        executor,
        NOW,
        hold=hold,
    )
    assert second.status == "held"
    assert second.decision_id == hold.decision_id
    assert executor.calls == []

    client = TestClient(
        create_app(service.config, capabilities_dir=service.capabilities)
    )
    try:
        decisions = _list(client, workspace_id)["decisions"]
    finally:
        client.close()
    matching = [d for d in decisions if d["decision_id"] == str(hold.decision_id)]
    assert len(matching) == 1, decisions
    row = matching[0]
    assert row["question"] == hold.question
    assert row["target_sha256"] == classification.target_sha256
