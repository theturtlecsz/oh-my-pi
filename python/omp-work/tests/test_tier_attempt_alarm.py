"""OMP-430-s07: tier attempt alarms on PostgreSQL.

Tests that:
- tier 3 held, signed (done), and unknown decision (refused) each add one owner_approval_attempt alert.
- tier 2 refused standing_policy_required adds one owner_approval_attempt alert.
- tier 1 and allowed tier 2 add none.
Classification is verified via alarm_classify.classify over new domain events.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omp_work import alarm_classify
from omp_work.action_classify import classify as classify_action
from omp_work.action_classify import parse_submission
from omp_work.control_actions import perform
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.api_models import DomainEventView
from omp_work.v1.store import PostgresWorkStore
from test_control_actions import (
    FUTURE,
    NOW,
    TIER3_SUBMISSIONS,
    _answer_decision,
    _Executor,
    _generate_key,
    _mandate_all_tier3,
    _open,
    _owner,
    _resolver_for,
    _signers_file,
)
from test_workflow_service import OWNER

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)


def _get_new_alerts(
    store: PostgresWorkStore, workspace_id: UUID, watermark: int
) -> tuple[list[alarm_classify.Alert], int]:
    res = store.events(workspace_id, OWNER, after=watermark)
    events = [DomainEventView.model_validate(e) for e in res["events"]]
    next_watermark = res["next_after_sequence"]
    alerts, _rest, _state = alarm_classify.classify(
        events, alarm_classify.AlarmState(after_sequence=watermark)
    )
    return alerts, next_watermark


def test_tier_attempt_alarms(service, tmp_path: Path) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    owner_key = _generate_key(tmp_path, "owner_key")
    _signers_file(service, owner_key)
    resolver = _resolver_for(store, workspace_id, project_id)

    # Initial watermark
    watermark = store.events(workspace_id, OWNER)["watermark_sequence"]

    # 1. Tier 3 held -> adds one owner_approval_attempt alert
    submission = dict(TIER3_SUBMISSIONS["billing_change"])
    held = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission,
        resolver,
        _Executor(),
        NOW,
    )
    assert held.status == "held"
    assert held.decision_id is not None
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 1
    assert alerts[0].kind == "owner_approval_attempt"
    assert alerts[0].summary == "tier 3 billing_change"

    # Sign the decision for the next test
    op = parse_submission(submission)
    classification = classify_action(op, resolver(op))
    _answer_decision(
        store,
        workspace_id,
        owner_key,
        held.decision_id,
        "billing_change",
        classification.target_sha256,
        FUTURE,
    )
    # Advance watermark past the answer_decision event
    watermark = store.events(workspace_id, OWNER)["watermark_sequence"]

    # 2. Tier 3 signed (done) -> adds one owner_approval_attempt alert
    done_executor = _Executor()
    done = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission,
        resolver,
        done_executor,
        NOW,
        decision_id=held.decision_id,
    )
    assert done.status == "done"
    assert len(done_executor.calls) == 1
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 1
    assert alerts[0].kind == "owner_approval_attempt"
    assert alerts[0].summary == "tier 3 billing_change"

    # 3. Tier 3 unknown decision (refused) -> adds one owner_approval_attempt alert
    refused_executor = _Executor()
    unknown_id = uuid4()
    refused = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        submission,
        resolver,
        refused_executor,
        NOW,
        decision_id=unknown_id,
    )
    assert refused.status == "refused"
    assert len(refused_executor.calls) == 0
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 1
    assert alerts[0].kind == "owner_approval_attempt"
    assert alerts[0].summary == "tier 3 billing_change"

    # 4. Tier 2 refused standing_policy_required -> adds one owner_approval_attempt alert
    tier2_submission = {
        "kind": "git_push",
        "repository": "repo-alpha",
        "branch": "feature/uncovered",
    }
    tier2_refused = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        tier2_submission,
        resolver,
        _Executor(),
        NOW,
    )
    assert tier2_refused.status == "refused"
    assert tier2_refused.code == "standing_policy_required"
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 1
    assert alerts[0].kind == "owner_approval_attempt"
    assert alerts[0].summary == "tier 2 push_branch"

    # 5. Tier 1 -> adds none
    tier1_submission = {"kind": "read_state"}
    tier1_done = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        tier1_submission,
        resolver,
        _Executor(),
        NOW,
    )
    assert tier1_done.status == "done"
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 0

    # 6. Allowed tier 2 -> adds none
    policy_decision_id = uuid4()
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class="push_branch",
        repositories=("repo-alpha",),
        branch_patterns=("feature/*",),
        decision_id=policy_decision_id,
    )
    store.put_standing_policy(
        workspace_id, OWNER, project_id, policy, _owner(policy_decision_id)
    )
    # Advance past policy creation
    watermark = store.events(workspace_id, OWNER)["watermark_sequence"]

    tier2_allowed = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        tier2_submission,
        resolver,
        _Executor(),
        NOW,
    )
    assert tier2_allowed.status == "done"
    alerts, watermark = _get_new_alerts(store, workspace_id, watermark)
    assert len(alerts) == 0
