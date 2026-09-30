"""OMP-431: PostgreSQL integration tests for the project egress policy store."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from omp_work.egress_policy import EgressRecord, ProjectEgress
from omp_work.egress_store import StoreEgressRecorder
from omp_work.project_store import ProjectAuthorityRefused
from omp_work.standing_change import ChangeAuthority
from omp_work.standing_policy import StandingPolicy
from omp_work.v1.store import PostgresWorkStore
from test_workflow_service import OWNER, _grant

pytest_plugins = ["test_workflow_service"]

pytestmark = pytest.mark.skipif(
    os.environ.get("OMP_WORK_POSTGRES_INTEGRATION") != "1",
    reason="set OMP_WORK_POSTGRES_INTEGRATION=1",
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
PROXY = "proxy.internal.test:8443"
REGISTRY = "https://registry.example.test"
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
    project_id = store.ensure_project(
        workspace_id, OWNER, "egress", "Egress", "surface"
    )
    store.update_profile(workspace_id, OWNER, project_id, repositories=REPOS)
    return store, workspace_id, project_id


def _owner(decision_id: UUID | None, answered: str | None = "owner") -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind=answered,
    )


def _egress(
    *,
    registries: tuple[str, ...] = (),
    remotes: tuple[str, ...] = (),
    decision_id: UUID | str | None,
) -> ProjectEgress:
    return ProjectEgress(
        registries=registries, remotes=remotes, decision_id=decision_id
    )


def _rows(service, sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            **service.config.connection_kwargs("postgres"), row_factory=dict_row
        ) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(sql, params)
        return list(cur.fetchall())


def _policy_rows(
    service, workspace_id: UUID, project_id: UUID
) -> list[dict[str, object]]:
    return _rows(
        service,
        "SELECT registries, remotes, decision_id, active"
        " FROM omp_work.project_egress_policies"
        " WHERE workspace_id=%s AND project_id=%s ORDER BY record_id",
        (workspace_id, project_id),
    )


def _record(
    workspace_id: UUID,
    project_id: UUID,
    *,
    mission_id: UUID | None,
    worker_id: str,
    host: str,
    at: datetime = NOW,
) -> EgressRecord:
    return EgressRecord(
        workspace_id=str(workspace_id),
        project_id=str(project_id),
        mission_id=None if mission_id is None else str(mission_id),
        worker_id=worker_id,
        stage="repository",
        channel="git",
        protocol="https",
        host=host,
        ip="203.0.113.9",
        port=443,
        method="GET",
        url=f"https://{host}/o/r.git/info/refs?service=git-upload-pack",
        klass="remote",
        outcome="allowed",
        code=None,
        policy_id=None,
        at=at,
    )


def test_owner_create_and_widen_store_each_version(service) -> None:
    store, workspace_id, project_id = _open(service)
    create_decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(
            registries=(REGISTRY,),
            remotes=("https://example.test/alpha.git",),
            decision_id=create_decision,
        ),
        _owner(create_decision),
    )
    rows = _policy_rows(service, workspace_id, project_id)
    assert len(rows) == 1
    assert rows[0]["active"] is True
    assert rows[0]["registries"] == [REGISTRY]
    assert rows[0]["remotes"] == ["https://example.test/alpha.git"]
    assert rows[0]["decision_id"] == create_decision

    widen_decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(
            registries=(REGISTRY, "https://registry2.example.test"),
            remotes=("https://example.test/alpha.git",),
            decision_id=widen_decision,
        ),
        _owner(widen_decision),
    )
    rows = _policy_rows(service, workspace_id, project_id)
    assert len(rows) == 2
    # The widened version is active and the version it replaced is not.
    active = [row for row in rows if row["active"]]
    assert len(active) == 1
    assert active[0]["decision_id"] == widen_decision
    assert active[0]["registries"] == [REGISTRY, "https://registry2.example.test"]

    policy = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)
    assert policy.registries == frozenset(
        {("registry.example.test", 443), ("registry2.example.test", 443)}
    )
    assert policy.remotes == frozenset({("https", "example.test", 443, "/alpha")})
    assert policy.decision_id == str(widen_decision)


@pytest.mark.parametrize(
    ("label", "requested", "answered", "code"),
    [
        ("unanswered", "owner", None, "owner_signature_required"),
        ("client-answered", "client", "client", "owner_signature_required"),
        ("automation-requested", "automation", "owner", "worker_not_permitted"),
        ("task-agent-requested", "task-agent", "owner", "worker_not_permitted"),
        ("model-requested", "model", "owner", "unknown_actor"),
    ],
)
def test_widen_refused_stores_nothing(
    service, label: str, requested: str, answered: str | None, code: str
) -> None:
    store, workspace_id, project_id = _open(service)
    base_decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(registries=(REGISTRY,), decision_id=base_decision),
        _owner(base_decision),
    )
    before = _policy_rows(service, workspace_id, project_id)

    decision = uuid4()
    authority = ChangeAuthority(
        requested_by_kind=requested,
        decision_id=decision,
        answered_by_kind=answered,
    )
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_project_egress(
            workspace_id,
            OWNER,
            project_id,
            _egress(
                registries=(REGISTRY, "https://registry2.example.test"),
                decision_id=decision,
            ),
            authority,
        )
    assert exc_info.value.code == code, label
    assert _policy_rows(service, workspace_id, project_id) == before


def test_client_narrow_is_stored_and_keeps_the_decision(service) -> None:
    store, workspace_id, project_id = _open(service)
    base_decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(
            registries=(REGISTRY, "https://registry2.example.test"),
            decision_id=base_decision,
        ),
        _owner(base_decision),
    )

    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(registries=(REGISTRY,), decision_id=base_decision),
        ChangeAuthority(requested_by_kind="client"),
    )
    rows = _policy_rows(service, workspace_id, project_id)
    active = [row for row in rows if row["active"]]
    assert len(active) == 1
    assert active[0]["registries"] == [REGISTRY]
    # A narrow keeps the decision that opened the standing record.
    assert active[0]["decision_id"] == base_decision


def test_unknown_remote_and_invalid_registry_are_refused(service) -> None:
    store, workspace_id, project_id = _open(service)
    decision = uuid4()

    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_project_egress(
            workspace_id,
            OWNER,
            project_id,
            _egress(remotes=("https://evil.test/alpha.git",), decision_id=decision),
            _owner(decision),
        )
    assert exc_info.value.code == "unknown_remote"
    assert _policy_rows(service, workspace_id, project_id) == []

    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_project_egress(
            workspace_id,
            OWNER,
            project_id,
            _egress(
                registries=("https://registry.example.test/v2",), decision_id=decision
            ),
            _owner(decision),
        )
    assert exc_info.value.code == "invalid_registry"
    assert _policy_rows(service, workspace_id, project_id) == []


def test_standing_network_access_loads_only_after_an_owner_put(service) -> None:
    store, workspace_id, project_id = _open(service)
    before = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)
    assert before.standing == ()

    worker = ChangeAuthority(
        requested_by_kind="task-agent",
        decision_id=uuid4(),
        answered_by_kind="owner",
    )
    with pytest.raises(ProjectAuthorityRefused) as exc_info:
        store.put_standing_policy(
            workspace_id,
            OWNER,
            project_id,
            StandingPolicy(
                policy_id=uuid4(),
                action_class="network_access",
                destinations=("https://extra.example.test",),
                decision_id=worker.decision_id,
            ),
            worker,
        )
    assert exc_info.value.code == "worker_not_permitted"
    still = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)
    assert still.standing == ()

    decision = uuid4()
    policy_id = uuid4()
    store.put_standing_policy(
        workspace_id,
        OWNER,
        project_id,
        StandingPolicy(
            policy_id=policy_id,
            action_class="network_access",
            destinations=("https://extra.example.test",),
            decision_id=decision,
        ),
        _owner(decision),
    )
    loaded = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)
    assert [item.policy_id for item in loaded.standing] == [policy_id]
    assert loaded.standing[0].destinations == ("https://extra.example.test",)


def test_checkout_contents_do_not_widen_the_policy(service, tmp_path: Path) -> None:
    store, workspace_id, project_id = _open(service)
    decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(
            registries=(REGISTRY,),
            remotes=("https://example.test/alpha.git",),
            decision_id=decision,
        ),
        _owner(decision),
    )
    before = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)

    # A project working copy whose npm, lockfile and submodule config all name
    # an unlisted host, plus a git remote pointing there.
    origin = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.test"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=clone, check=True)
    (clone / ".npmrc").write_text("registry=https://evil.test/\n")
    (clone / "package-lock.json").write_text(
        '{"packages": {"node_modules/x": {"resolved": "https://evil.test/x.tgz"}}}\n'
    )
    (clone / ".gitmodules").write_text(
        '[submodule "x"]\n\tpath = x\n\turl = https://evil.test/x.git\n'
    )
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "config"], cwd=clone, check=True)
    subprocess.run(
        ["git", "remote", "add", "evil", "https://evil.test/x.git"],
        cwd=clone,
        check=True,
    )

    after = store.load_egress_policy(workspace_id, OWNER, project_id, NOW, PROXY)
    assert after == before


def test_records_keep_mission_and_worker(service) -> None:
    store, workspace_id, project_id = _open(service)
    mission_id = uuid4()
    other_mission = uuid4()
    recorder = StoreEgressRecorder(store, workspace_id, OWNER)
    recorder.record(
        _record(
            workspace_id,
            project_id,
            mission_id=mission_id,
            worker_id="w-1",
            host="a.test",
            at=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
        )
    )
    recorder.record(
        _record(
            workspace_id,
            project_id,
            mission_id=other_mission,
            worker_id="w-2",
            host="b.test",
            at=datetime(2026, 9, 29, 12, 1, tzinfo=timezone.utc),
        )
    )

    records = store.list_egress_records(workspace_id, OWNER, project_id)
    assert [(row["mission_id"], row["worker_id"]) for row in records] == [
        (str(mission_id), "w-1"),
        (str(other_mission), "w-2"),
    ]
    only = store.list_egress_records(workspace_id, OWNER, project_id, mission_id)
    assert [row["worker_id"] for row in only] == ["w-1"]


def test_record_keeps_a_mission_that_has_no_row(service) -> None:
    store, workspace_id, project_id = _open(service)
    ghost = uuid4()
    store.record_egress(
        workspace_id,
        OWNER,
        _record(
            workspace_id, project_id, mission_id=ghost, worker_id="w-1", host="a.test"
        ),
    )
    assert _rows(
        service,
        "SELECT mission_id FROM omp_work.egress_records"
        " WHERE workspace_id=%s AND project_id=%s",
        (workspace_id, project_id),
    ) == [{"mission_id": ghost}]


def test_content_columns_are_not_updatable(service) -> None:
    store, workspace_id, project_id = _open(service)
    decision = uuid4()
    store.put_project_egress(
        workspace_id,
        OWNER,
        project_id,
        _egress(registries=(REGISTRY,), decision_id=decision),
        _owner(decision),
    )
    store.record_egress(
        workspace_id,
        OWNER,
        _record(
            workspace_id, project_id, mission_id=None, worker_id="w-1", host="a.test"
        ),
    )
    denied = (
        (
            "UPDATE omp_work.project_egress_policies SET registries = %s"
            " WHERE workspace_id = %s",
            (["https://evil.test"], workspace_id),
        ),
        (
            "UPDATE omp_work.egress_records SET outcome = %s WHERE workspace_id = %s",
            ("refused", workspace_id),
        ),
    )
    for sql, params in denied:
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            psycopg.connect(**service.config.connection_kwargs("omp_work_app")) as conn,
        ):
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true),"
                    " set_config('omp.actor_id', %s, true)",
                    (str(workspace_id), str(OWNER)),
                )
                cur.execute(sql, params)
    rows = _policy_rows(service, workspace_id, project_id)
    assert rows[0]["registries"] == [REGISTRY]
    assert _rows(
        service,
        "SELECT outcome FROM omp_work.egress_records WHERE workspace_id=%s",
        (workspace_id,),
    ) == [{"outcome": "allowed"}]
