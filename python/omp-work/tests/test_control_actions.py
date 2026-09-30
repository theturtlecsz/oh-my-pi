"""OMP-403: the disposable-resource registry, tier 2 policy return, and control-plane executor."""

from __future__ import annotations

from collections.abc import Mapping
import os
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

from omp_work.action_classify import (
    Operation,
    ResolvedTarget,
    classify,
    parse_submission,
)
from omp_work.action_tiers import TIER3
from omp_work.control_actions import perform
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.spend_budget import SpendBudget
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_mandate import StandingMandate
from omp_work.standing_policy import ActionRequest, RepositoryRecord, StandingPolicy
from omp_work.v1.models import CommandEnvelope, OperationState
from omp_work.v1.owner_signature import NAMESPACE, decision_signature_message
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1"
    or shutil.which("ssh-keygen") is None,
    reason="requires OMP_WORK_POSTGRES_INTEGRATION=1 and ssh-keygen",
)

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
FUTURE = datetime(2099, 1, 1, 12, 0, tzinfo=UTC)
REPOS = (
    {
        "key": "repo-alpha",
        "name": "repo-alpha",
        "url": "https://example.test/alpha.git",
        "default_branch": "main",
        "protected_branches": ["main", "release"],
        "automation_ci_secret_free": True,
    },
)

_REPO = RepositoryRecord(
    key="repo-alpha",
    default_branch="main",
    protected_branches=("main", "release"),
    automation_ci_secret_free=True,
)

TIER3_SUBMISSIONS: dict[str, dict[str, object]] = {
    "merge_protected_branch": {
        "kind": "merge",
        "repository": "repo-alpha",
        "branch": "main",
    },
    "production_deploy": {"kind": "deploy", "environment": "prod"},
    "destructive_infra": {"kind": "infra_destroy"},
    "credential_change": {"kind": "credential_change"},
    "delete_persistent_data": {"kind": "delete_data"},
    "billing_change": {"kind": "billing_change"},
    "publish_as_owner": {"kind": "publish"},
    "broaden_scope": {"kind": "scope_broaden"},
    "outside_secrets": {"kind": "secret_read"},
    "disable_safeguards": {"kind": "safeguard_change"},
}


class _Executor:
    """Records every executor call so a held action proves nothing ran."""

    def __init__(self) -> None:
        self.calls: list[tuple[Operation, ResolvedTarget]] = []

    def __call__(self, operation: Operation, resolved: ResolvedTarget) -> object:
        self.calls.append((operation, resolved))
        return {"ran": True}


class _SequenceResolver:
    """Returns each target once, then repeats the last (one resolve per phase)."""

    def __init__(self, targets: list[ResolvedTarget]) -> None:
        self._targets = targets
        self.calls = 0

    def __call__(self, operation: Operation) -> ResolvedTarget:
        index = min(self.calls, len(self._targets) - 1)
        self.calls += 1
        return self._targets[index]


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


def _open(service, key: str = "control") -> tuple[PostgresWorkStore, UUID, UUID]:
    store = PostgresWorkStore(service.config)
    workspace_id = uuid4()
    _workspace(service, workspace_id)
    project_id = store.ensure_project(
        workspace_id, OWNER, key, f"Control {key}", "surface"
    )
    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    return store, workspace_id, project_id


def _owner(decision_id: UUID | None) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind="owner",
    )


def _mandate_all_tier3(store: PostgresWorkStore, workspace_id: UUID, project_id: UUID) -> None:
    decision_id = uuid4()
    store.set_standing_mandate(
        workspace_id,
        OWNER,
        project_id,
        StandingMandate(
            mandate_id=uuid4(),
            tier3_classes=frozenset(TIER3),
            decision_id=decision_id,
        ),
        _owner(decision_id),
    )


def _resolver_for(
    store: PostgresWorkStore,
    workspace_id: UUID,
    project_id: UUID,
    *,
    paths_by_commit: Mapping[str, tuple[str, ...]] | None = None,
    data_inside: frozenset[str] = frozenset(),
):
    def resolve(operation: Operation) -> ResolvedTarget:
        repository = _REPO if operation.repository == "repo-alpha" else None
        paths = tuple((paths_by_commit or {}).get(operation.commit or "", ()))
        disposable = False
        if operation.resource_id is not None:
            disposable = store.is_disposable_resource(
                workspace_id, OWNER, project_id, operation.resource_id
            )
        return ResolvedTarget(
            repository=repository,
            commit=operation.commit,
            changed_paths=paths,
            disposable=disposable,
            data_inside=operation.resource_id in data_inside,
        )

    return resolve


def _generate_key(tmp_path: Path, name: str) -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
    )
    return key


def _signers_file(service, owner_key: Path) -> Path:
    signers_path = service.config.config_dir / "owner_allowed_signers"
    service.config.config_dir.mkdir(parents=True, exist_ok=True)
    signers_path.write_text(
        f"owner {Path(f'{owner_key}.pub').read_text(encoding='utf-8').strip()}\n",
        encoding="utf-8",
    )
    return signers_path


def _sign(key: Path, message: bytes) -> str:
    completed = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
        input=message,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("ascii")


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


def _answer_decision(
    store: PostgresWorkStore,
    workspace_id: UUID,
    key: Path,
    decision_id: UUID,
    action_class: str,
    target_sha256: str,
    expires_at: datetime,
    answer: str = "approve",
) -> None:
    message = decision_signature_message(
        workspace_id=workspace_id,
        decision_id=decision_id,
        action_class=action_class,
        answer=answer,
        target_sha256=target_sha256,
        expires_at=expires_at,
    )
    envelope = _envelope(
        workspace_id,
        {
            "type": "answer_decision",
            "payload": {
                "decision_id": str(decision_id),
                "answer": answer,
                "owner_signature": _sign(key, message),
                "expires_at": expires_at.astimezone(UTC).isoformat(),
            },
        },
    )
    receipt, _ = store.execute(
        envelope,
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.approve",
    )
    assert receipt.state == OperationState.APPLIED


def _create_decision(
    store: PostgresWorkStore,
    workspace_id: UUID,
    project_id: UUID,
    decision_id: UUID,
    action_class: str,
    target_sha256: str,
) -> None:
    envelope = _envelope(
        workspace_id,
        {
            "type": "create_decision",
            "payload": {
                "decision_id": str(decision_id),
                "project_id": str(project_id),
                "mission_id": "OMP-403",
                "question": "Authorize this tier 3 action?",
                "why_it_matters": "A tier 3 action needs one signed owner decision.",
                "risk_of_delay": "The action waits for the owner.",
                "options": ["approve", "decline"],
                "default_if_any": "decline",
                "risk_of_each_choice": {
                    "approve": "The action runs.",
                    "decline": "The action stays blocked.",
                },
                "action_class": action_class,
                "target_sha256": target_sha256,
                "resume_state": "tier3",
            },
        },
    )
    receipt, _ = store.execute(
        envelope,
        actor_id=OWNER,
        actor_kind="owner",
        required_scope="work.mutate",
    )
    assert receipt.state == OperationState.APPLIED


def _action_rows(service, workspace_id: UUID) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT action_class, tier, outcome, code, decision_id"
            " FROM omp_work.project_action_records WHERE workspace_id=%s"
            " ORDER BY recorded_at, record_id",
            (workspace_id,),
        )
        return list(cur.fetchall())


def _spend_rows(service, workspace_id: UUID) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            "SELECT amount_usd FROM omp_work.spend_records WHERE workspace_id=%s",
            (workspace_id,),
        )
        return list(cur.fetchall())


def _disposable_policy(
    store: PostgresWorkStore, workspace_id: UUID, project_id: UUID
) -> StandingPolicy:
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class="disposable_cloud",
        destinations=("https://a.test",),
        resource_types=("vm",),
        money_limit_usd=Decimal("10"),
        decision_id=uuid4(),
    )
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(uuid4()))
    return policy


def test_record_then_read_is_true(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy_id = uuid4()
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", policy_id
    )
    assert store.is_disposable_resource(workspace_id, OWNER, project_id, "web-1")


def test_disposable_match_is_exact_id_not_a_like_prefix(service) -> None:
    store, workspace_id, project_id = _open(service)
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", uuid4()
    )
    assert store.is_disposable_resource(workspace_id, OWNER, project_id, "web-1")
    # ``_`` is not a wildcard and a prefix does not match.
    assert not store.is_disposable_resource(workspace_id, OWNER, project_id, "web_1")
    assert not store.is_disposable_resource(workspace_id, OWNER, project_id, "web")


def test_disposable_registry_is_scoped_to_workspace_and_project(service) -> None:
    store, workspace_id, project_id = _open(service, "control-a")
    other_project = store.ensure_project(
        workspace_id, OWNER, "control-b", "Control B", "surface"
    )
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "web-1", uuid4()
    )
    assert not store.is_disposable_resource(
        workspace_id, OWNER, other_project, "web-1"
    )

    other_store, other_workspace, other_workspace_project = _open(service, "control")
    assert not other_store.is_disposable_resource(
        other_workspace, OWNER, other_workspace_project, "web-1"
    )


def test_tier2_request_action_returns_the_covering_policy_id(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy = StandingPolicy(
        policy_id=uuid4(),
        action_class="nonprod_update",
        destinations=("https://a.test",),
        resource_types=("vm",),
        decision_id=uuid4(),
    )
    store.put_standing_policy(workspace_id, OWNER, project_id, policy, _owner(uuid4()))

    returned = store.request_action(
        workspace_id,
        OWNER,
        project_id,
        None,
        ActionRequest(
            "nonprod_update", destination="https://a.test", resource_type="vm"
        ),
        None,
        NOW,
    )
    assert returned == policy.policy_id


def test_tier2_request_action_without_a_policy_returns_nothing(service) -> None:
    store, workspace_id, project_id = _open(service)
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.request_action(
            workspace_id,
            OWNER,
            project_id,
            None,
            ActionRequest(
                "nonprod_update", destination="https://a.test", resource_type="vm"
            ),
            None,
            NOW,
        )
    assert exc_info.value.code == "standing_policy_required"


def test_tier1_runs_and_records(service) -> None:
    store, workspace_id, project_id = _open(service)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {"kind": "read_state", "tier": 3, "action_class": "merge_protected_branch"},
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "done"
    assert outcome.action_class == "read_state"
    assert outcome.tier == 1
    assert outcome.decision_id is None
    assert outcome.code is None
    assert len(executor.calls) == 1
    rows = _action_rows(service, workspace_id)
    assert rows[-1]["action_class"] == "read_state"
    assert rows[-1]["outcome"] == "allowed"


def test_tier2_spend_records_the_spend(service) -> None:
    store, workspace_id, project_id = _open(service)
    budget_decision = uuid4()
    store.set_spend_budget(
        workspace_id,
        OWNER,
        project_id,
        None,
        SpendBudget("project", Decimal("100"), Decimal("40")),
        _owner(budget_decision),
    )
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {"kind": "spend", "amount_usd": "5"},
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "done"
    assert outcome.action_class == "spend_beyond_threshold"
    assert outcome.tier == 2
    assert len(executor.calls) == 1
    assert [row["amount_usd"] for row in _spend_rows(service, workspace_id)] == [
        Decimal("5")
    ]


def test_tier3_each_class_is_held_then_runs_after_owner_answer(
    service, tmp_path: Path
) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    owner_key = _generate_key(tmp_path, "owner_key")
    signers_path = _signers_file(service, owner_key)
    resolver = _resolver_for(store, workspace_id, project_id)
    try:
        for action_class in sorted(TIER3):
            submission = dict(TIER3_SUBMISSIONS[action_class])
            operation = parse_submission(submission)
            classification = classify(operation, resolver(operation))

            executor = _Executor()
            held = perform(
                store,
                workspace_id,
                OWNER,
                project_id,
                None,
                submission,
                resolver,
                executor,
                NOW,
            )
            assert held.status == "held", action_class
            assert held.action_class == action_class
            assert held.tier == 3
            assert held.decision_id is not None
            assert held.code is None
            assert executor.calls == []

            _answer_decision(
                store,
                workspace_id,
                owner_key,
                held.decision_id,
                action_class,
                classification.target_sha256,
                FUTURE,
            )
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
            assert done.status == "done", action_class
            assert done.action_class == action_class
            assert done.decision_id == held.decision_id
            assert len(done_executor.calls) == 1
    finally:
        if signers_path.exists():
            signers_path.unlink()


def test_merge_labelled_tier1_is_held_as_merge_protected_branch(service) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {
            "kind": "merge",
            "repository": "repo-alpha",
            "branch": "main",
            "tier": 1,
            "label": "safe",
            "action_class": "modify_files",
        },
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "held"
    assert outcome.action_class == "merge_protected_branch"
    assert outcome.tier == 3
    assert outcome.decision_id is not None
    assert executor.calls == []


def test_push_touching_ci_workflow_is_held_and_never_runs(service) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {
            "kind": "git_push",
            "repository": "repo-alpha",
            "branch": "topic/x",
            "commit": "c1",
        },
        _resolver_for(
            store,
            workspace_id,
            project_id,
            paths_by_commit={"c1": (".github/workflows/ci.yml",)},
        ),
        executor,
        NOW,
    )
    assert outcome.status == "held"
    assert outcome.action_class == "disable_safeguards"
    assert outcome.tier == 3
    assert outcome.decision_id is not None
    assert executor.calls == []


def test_new_commit_on_recheck_is_target_changed(service, tmp_path: Path) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    owner_key = _generate_key(tmp_path, "owner_key")
    signers_path = _signers_file(service, owner_key)
    try:
        submission = {
            "kind": "merge",
            "repository": "repo-alpha",
            "branch": "main",
            "commit": "c1",
        }
        operation = parse_submission(submission)
        approved = classify(
            operation, ResolvedTarget(repository=_REPO, commit="c1", changed_paths=())
        )
        decision_id = uuid4()
        _create_decision(
            store,
            workspace_id,
            project_id,
            decision_id,
            approved.action_class,
            approved.target_sha256,
        )
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            decision_id,
            approved.action_class,
            approved.target_sha256,
            FUTURE,
        )

        executor = _Executor()
        resolver = _SequenceResolver(
            [
                ResolvedTarget(repository=_REPO, commit="c1", changed_paths=()),
                ResolvedTarget(repository=_REPO, commit="c2", changed_paths=()),
            ]
        )
        outcome = perform(
            store,
            workspace_id,
            OWNER,
            project_id,
            None,
            submission,
            resolver,
            executor,
            NOW,
            decision_id=decision_id,
        )
        assert outcome.status == "refused"
        assert outcome.code == "target_changed"
        assert outcome.decision_id == decision_id
        assert executor.calls == []
    finally:
        if signers_path.exists():
            signers_path.unlink()


def test_decision_reused_for_another_branch_is_target_mismatch(
    service, tmp_path: Path
) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    owner_key = _generate_key(tmp_path, "owner_key")
    signers_path = _signers_file(service, owner_key)
    try:
        resolver = _resolver_for(store, workspace_id, project_id)
        merge = {"kind": "merge", "repository": "repo-alpha", "branch": "main"}
        approved = classify(parse_submission(merge), resolver(parse_submission(merge)))
        decision_id = uuid4()
        _create_decision(
            store,
            workspace_id,
            project_id,
            decision_id,
            approved.action_class,
            approved.target_sha256,
        )
        _answer_decision(
            store,
            workspace_id,
            owner_key,
            decision_id,
            approved.action_class,
            approved.target_sha256,
            FUTURE,
        )
        executor = _Executor()
        outcome = perform(
            store,
            workspace_id,
            OWNER,
            project_id,
            None,
            {"kind": "merge", "repository": "repo-alpha", "branch": "release"},
            resolver,
            executor,
            NOW,
            decision_id=decision_id,
        )
        assert outcome.status == "refused"
        assert outcome.code == "authorization_target_mismatch"
        assert outcome.decision_id == decision_id
        assert executor.calls == []
    finally:
        if signers_path.exists():
            signers_path.unlink()


def test_delete_of_recorded_disposable_runs_tier2(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy = _disposable_policy(store, workspace_id, project_id)
    store.record_disposable_resource(
        workspace_id, OWNER, project_id, None, "res-1", policy.policy_id
    )
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {
            "kind": "cloud_delete",
            "resource_id": "res-1",
            "destination": "https://a.test",
            "resource_type": "vm",
            "amount_usd": "5",
        },
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "done"
    assert outcome.action_class == "disposable_cloud"
    assert outcome.tier == 2
    assert len(executor.calls) == 1


def test_delete_of_unrecorded_resource_is_held_destructive_infra(service) -> None:
    store, workspace_id, project_id = _open(service)
    _disposable_policy(store, workspace_id, project_id)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {
            "kind": "cloud_delete",
            "resource_id": "res-x",
            "destination": "https://a.test",
            "resource_type": "vm",
            "amount_usd": "5",
        },
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "held"
    assert outcome.action_class == "destructive_infra"
    assert outcome.tier == 3
    assert outcome.decision_id is not None
    assert executor.calls == []


def test_delete_of_resource_with_data_inside_is_held_delete_persistent_data(
    service,
) -> None:
    store, workspace_id, project_id = _open(service)
    _disposable_policy(store, workspace_id, project_id)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        None,
        {
            "kind": "cloud_delete",
            "resource_id": "res-data",
            "destination": "https://a.test",
            "resource_type": "vm",
            "amount_usd": "5",
        },
        _resolver_for(
            store, workspace_id, project_id, data_inside=frozenset({"res-data"})
        ),
        executor,
        NOW,
    )
    assert outcome.status == "held"
    assert outcome.action_class == "delete_persistent_data"
    assert outcome.tier == 3
    assert outcome.decision_id is not None
    assert executor.calls == []


def test_missing_mission_refuses_without_opening_a_decision(service) -> None:
    store, workspace_id, project_id = _open(service)
    _mandate_all_tier3(store, workspace_id, project_id)
    executor = _Executor()
    outcome = perform(
        store,
        workspace_id,
        OWNER,
        project_id,
        uuid4(),
        {"kind": "merge", "repository": "repo-alpha", "branch": "main"},
        _resolver_for(store, workspace_id, project_id),
        executor,
        NOW,
    )
    assert outcome.status == "refused"
    assert outcome.code == "mission_not_found"
    assert outcome.decision_id is None
    assert executor.calls == []
    assert _action_rows(service, workspace_id) == []


def _delete(resource_id: str) -> dict[str, object]:
    return {
        "kind": "cloud_delete",
        "resource_id": resource_id,
        "destination": "https://a.test",
        "resource_type": "vm",
        "amount_usd": "5",
    }


def test_disposable_match_is_exact_id_not_a_like_prefix_under_perform(service) -> None:
    store, workspace_id, project_id = _open(service)
    policy = _disposable_policy(store, workspace_id, project_id)
    recorded = ("web-1", "db replica", "100%_cpu")
    for resource_id in recorded:
        store.record_disposable_resource(
            workspace_id, OWNER, project_id, None, resource_id, policy.policy_id
        )
    resolver = _resolver_for(store, workspace_id, project_id)

    for resource_id in recorded:
        assert store.is_disposable_resource(
            workspace_id, OWNER, project_id, resource_id
        )
    for resource_id in ("web_1", "db", "100Xcpu"):
        assert not store.is_disposable_resource(
            workspace_id, OWNER, project_id, resource_id
        )

    for resource_id in ("web_1", "db", "100Xcpu"):
        executor = _Executor()
        outcome = perform(
            store,
            workspace_id,
            OWNER,
            project_id,
            None,
            _delete(resource_id),
            resolver,
            executor,
            NOW,
        )
        assert outcome.status == "held", resource_id
        assert outcome.action_class == "destructive_infra", resource_id
        assert outcome.tier == 3
        assert outcome.decision_id is not None
        assert executor.calls == []

    for resource_id in recorded:
        executor = _Executor()
        outcome = perform(
            store,
            workspace_id,
            OWNER,
            project_id,
            None,
            _delete(resource_id),
            resolver,
            executor,
            NOW,
        )
        assert outcome.status == "done", resource_id
        assert outcome.action_class == "disposable_cloud", resource_id
        assert outcome.tier == 2
        assert len(executor.calls) == 1
