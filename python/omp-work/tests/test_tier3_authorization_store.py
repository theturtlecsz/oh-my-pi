"""OMP-403: one signed owner decision authorizes exactly one tier 3 action."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from test_workflow_service import OWNER, _grant

from omp_work.action_tiers import TIER1, TIER2, TIER3
from omp_work.project_store import ProjectAuthorityRefused, Tier3Approval
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import StandingMandate
from omp_work.standing_policy import ActionRequest, StandingPolicy
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message
from omp_work.v1.store import PostgresWorkStore

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
TARGET_A = "a" * 64
TARGET_B = "b" * 64
FUTURE = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
REPOS = (
    {
        "key": "repo-alpha",
        "name": "repo-alpha",
        "url": "https://example.test/alpha.git",
        "default_branch": "main",
        "protected_branches": ["main"],
        "automation_ci_secret_free": True,
    },
)

# One covering action and the policy bounds CLASS_BOUNDS requires for each
# tier 2 class.
TIER2_COVERAGE: dict[str, tuple[dict[str, object], dict[str, object]]] = {
    "push_branch": (
        {"repositories": ("repo-alpha",), "branch_patterns": ("topic/*",)},
        {"repository": "repo-alpha", "branch": "topic/x"},
    ),
    "create_pull_request": (
        {"repositories": ("repo-alpha",), "branch_patterns": ("topic/*",)},
        {"repository": "repo-alpha", "branch": "topic/x"},
    ),
    "nonprod_update": (
        {"destinations": ("https://a.test",), "resource_types": ("vm",)},
        {"destination": "https://a.test", "resource_type": "vm"},
    ),
    "spend_beyond_threshold": (
        {"money_limit_usd": Decimal(10)},
        {"amount_usd": Decimal(5)},
    ),
    "network_access": (
        {"destinations": ("https://a.test",)},
        {"destination": "https://a.test"},
    ),
    "disposable_cloud": (
        {
            "destinations": ("https://a.test",),
            "resource_types": ("vm",),
            "money_limit_usd": Decimal(10),
        },
        {
            "destination": "https://a.test",
            "resource_type": "vm",
            "amount_usd": Decimal(5),
        },
    ),
}


def _owner(decision_id: UUID | None) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind="owner",
    )


def _workspace(service, workspace_id: UUID) -> None:
    _grant(service, workspace_id)
    with (
        psycopg.connect(**service.config.connection_kwargs("postgres")) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        cur.execute(
            "INSERT INTO omp_control.workspaces(workspace_id) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (workspace_id,),
        )


def _open(service):
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(workspace_id, OWNER, "tier3", "Tier3", "surface")
    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    return store, workspace_id, project_id


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


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


def _decision_body(
    decision_id: UUID,
    project_id: UUID,
    action_class: str | None,
    target_sha256: str | None,
) -> dict[str, object]:
    return {
        "decision_id": str(decision_id),
        "project_id": str(project_id),
        "mission_id": "OMP-403",
        "question": "Authorize this tier 3 action?",
        "why_it_matters": "A tier 3 action needs one signed owner decision.",
        "risk_of_delay": "The action waits for the owner.",
        "options": ["approve", "reject"],
        "evidence_refs": ["receipt:tier3-authorization"],
        "default_if_any": "reject",
        "risk_of_each_choice": {
            "approve": "The action runs.",
            "reject": "The action stays blocked.",
        },
        "action_class": action_class,
        "target_sha256": target_sha256,
        "resume_state": "tier3-approved",
    }


def _create_decision(
    store,
    workspace_id: UUID,
    project_id: UUID,
    decision_id: UUID,
    action_class: str | None,
    target_sha256: str | None,
) -> None:
    receipt, _ = store.execute(
        _envelope(
            workspace_id,
            {
                "type": "create_decision",
                "payload": _decision_body(
                    decision_id, project_id, action_class, target_sha256
                ),
            },
        ),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.mutate",
    )
    assert receipt.state == OperationState.APPLIED


def _answer_decision(
    store,
    workspace_id: UUID,
    key: Path,
    decision_id: UUID,
    action_class: str,
    answer: str,
    target_sha256: str,
    expires_at: datetime,
) -> None:
    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class=action_class,
        answer=answer,
        target_sha256=target_sha256,
        expires_at=expires_at,
    )
    signature = _sign(key, message)
    receipt, _ = store.execute(
        _envelope(
            workspace_id,
            {
                "type": "answer_decision",
                "payload": {
                    "decision_id": str(decision_id),
                    "answer": answer,
                    "owner_signature": signature,
                    "expires_at": expires_at.astimezone(UTC).isoformat(),
                },
            },
        ),
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert receipt.state == OperationState.APPLIED


def _signers_file(service, owner_key: Path) -> Path:
    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        f"owner {Path(f'{owner_key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    return signers_path


def _action_rows(service, workspace_id: UUID) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT action_class, tier, outcome, code, policy_id, decision_id"
            " FROM omp_work.project_action_records WHERE workspace_id=%s"
            " ORDER BY recorded_at, record_id",
            (workspace_id,),
        )
        return list(cur.fetchall())


def _cover(
    store,
    workspace_id: UUID,
    project_id: UUID,
    action_class: str,
    bounds: dict[str, object],
) -> StandingPolicy:
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class=action_class,
        decision_id=uuid4(),
        **bounds,
    )
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(uuid4()))
    return policy


def test_tier1_is_allowed_with_no_authority_or_policy(service) -> None:
    store, workspace_id, project_id = _open(service)
    for action_class in sorted(TIER1):
        store.request_action(
            workspace_id,
            OWNER,
            project_id,
            None,
            ActionRequest(action_class),
            None,
            NOW,
        )
    rows = _action_rows(service, workspace_id)
    assert [row["action_class"] for row in rows] == sorted(TIER1)
    assert all(row["tier"] == 1 and row["outcome"] == "allowed" for row in rows)
    assert all(row["policy_id"] is None and row["decision_id"] is None for row in rows)


def test_tier2_requires_a_covering_policy(service) -> None:
    store, workspace_id, project_id = _open(service)
    for action_class in sorted(TIER2):
        bounds, action_kwargs = TIER2_COVERAGE[action_class]
        action = ActionRequest(action_class, **action_kwargs)

        with pytest.raises(ProjectAuthorityRefused) as exc_info:
            store.request_action(
                workspace_id, OWNER, project_id, None, action, None, NOW
            )
        assert exc_info.value.code == "standing_policy_required"

        policy = _cover(store, workspace_id, project_id, action_class, bounds)
        store.request_action(workspace_id, OWNER, project_id, None, action, None, NOW)

        rows = _action_rows(service, workspace_id)
        assert rows[-2]["action_class"] == action_class
        assert rows[-2]["outcome"] == "refused"
        assert rows[-2]["code"] == "standing_policy_required"
        assert rows[-1]["action_class"] == action_class
        assert rows[-1]["outcome"] == "allowed"
        assert rows[-1]["code"] is None
        assert rows[-1]["policy_id"] == policy.policy_id
        assert rows[-1]["decision_id"] is None


def test_tier3_approval_lifecycle(service, tmp_path: Path) -> None:
    store, workspace_id, project_id = _open(service)
    owner_key = _generate_key(tmp_path, "owner_key")
    signers_path = _signers_file(service, owner_key)
    try:
        mandate_decision = uuid4()
        store.set_standing_mandate(
            workspace_id,
            OWNER,
            project_id,
            StandingMandate(
                mandate_id=uuid4(),
                tier3_classes=frozenset(TIER3),
                decision_id=mandate_decision,
            ),
            _owner(mandate_decision),
        )

        action_class = min(TIER3)
        decision_id = uuid4()
        _create_decision(
            store, workspace_id, project_id, decision_id, action_class, TARGET_A
        )
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            decision_id,
            action_class,
            "approve",
            TARGET_A,
            FUTURE,
        )
        approval = Tier3Approval(decision_id=decision_id, target_sha256=TARGET_A)
        action = ActionRequest(action_class)

        store.request_action(
            workspace_id, OWNER, project_id, None, action, None, NOW, approval=approval
        )
        rows = _action_rows(service, workspace_id)
        assert rows[-1]["action_class"] == action_class
        assert rows[-1]["tier"] == 3
        assert rows[-1]["outcome"] == "allowed"
        assert rows[-1]["code"] is None
        assert rows[-1]["decision_id"] == decision_id

        with pytest.raises(ProjectAuthorityRefused) as exc_used:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                action,
                None,
                NOW,
                approval=approval,
            )
        assert exc_used.value.code == "authorization_used"
        rows = _action_rows(service, workspace_id)
        assert rows[-1]["outcome"] == "refused"
        assert rows[-1]["code"] == "authorization_used"

        # Another digest: same decision, wrong target.
        with pytest.raises(ProjectAuthorityRefused) as exc_target:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                action,
                None,
                NOW,
                approval=Tier3Approval(decision_id=decision_id, target_sha256=TARGET_B),
            )
        assert exc_target.value.code == "authorization_target_mismatch"

        # Another class: same signed target, a different tier 3 action class.
        other_class = sorted(TIER3)[1]
        with pytest.raises(ProjectAuthorityRefused) as exc_class:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                ActionRequest(other_class),
                None,
                NOW,
                approval=approval,
            )
        assert exc_class.value.code == "authorization_class_mismatch"

        # Declined: the record is answered but not with "approve".
        declined_id = uuid4()
        _create_decision(
            store, workspace_id, project_id, declined_id, action_class, TARGET_A
        )
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            declined_id,
            action_class,
            "reject",
            TARGET_A,
            FUTURE,
        )
        with pytest.raises(ProjectAuthorityRefused) as exc_declined:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                action,
                None,
                NOW,
                approval=Tier3Approval(decision_id=declined_id, target_sha256=TARGET_A),
            )
        assert exc_declined.value.code == "authorization_missing"

        # Unanswered: the record exists but stays pending.
        pending_id = uuid4()
        _create_decision(
            store, workspace_id, project_id, pending_id, action_class, TARGET_A
        )
        with pytest.raises(ProjectAuthorityRefused) as exc_pending:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                action,
                None,
                NOW,
                approval=Tier3Approval(decision_id=pending_id, target_sha256=TARGET_A),
            )
        assert exc_pending.value.code == "authorization_missing"

        # Expiry: answered approve, but expires_at is DB now + 2 s.
        with store._transaction(workspace_id, OWNER) as cur:
            cur.execute("SELECT clock_timestamp() AS now")
            db_now = cur.fetchone()["now"]
        expiring_id = uuid4()
        _create_decision(
            store, workspace_id, project_id, expiring_id, action_class, TARGET_A
        )
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            expiring_id,
            action_class,
            "approve",
            TARGET_A,
            db_now + timedelta(seconds=2),
        )
        time.sleep(3)
        with pytest.raises(ProjectAuthorityRefused) as exc_expired:
            store.request_action(
                workspace_id,
                OWNER,
                project_id,
                None,
                action,
                None,
                NOW,
                approval=Tier3Approval(decision_id=expiring_id, target_sha256=TARGET_A),
            )
        assert exc_expired.value.code == "authorization_expired"
    finally:
        if signers_path.exists():
            signers_path.unlink()
