"""Tests for standing change authority and authorization rules (OMP-418)."""

from __future__ import annotations

import pytest

from omp_work.standing_change import (
    ChangeAuthority,
    ChangeKind,
    StandingChangeRefused,
    authorize_standing_change,
    owner_signed,
)


def test_owner_signed() -> None:
    auth_owner = ChangeAuthority(
        requested_by_kind="owner",
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    assert owner_signed(auth_owner) is True

    auth_client = ChangeAuthority(
        requested_by_kind="client",
        decision_id="dec-1",
        answered_by_kind="client",
    )
    assert owner_signed(auth_client) is False

    auth_none = ChangeAuthority(
        requested_by_kind="owner",
        decision_id=None,
        answered_by_kind=None,
    )
    assert owner_signed(auth_none) is False


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
def test_create_widen_extend_owner_ok(kind: ChangeKind) -> None:
    auth = ChangeAuthority(
        requested_by_kind="owner",
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    # Must not raise
    authorize_standing_change(kind, auth)
    authorize_standing_change(kind.value, auth)


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
def test_create_widen_extend_client_requester_with_owner_signature_ok(kind: ChangeKind) -> None:
    auth = ChangeAuthority(
        requested_by_kind="client",
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    authorize_standing_change(kind, auth)


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
def test_create_widen_extend_client_answer_refused(kind: ChangeKind) -> None:
    auth = ChangeAuthority(
        requested_by_kind="client",
        decision_id="dec-1",
        answered_by_kind="client",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth)
    assert exc_info.value.code == "owner_signature_required"


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
@pytest.mark.parametrize("worker_kind", ["automation", "task-agent"])
def test_create_widen_extend_workers_refused(kind: ChangeKind, worker_kind: str) -> None:
    auth = ChangeAuthority(
        requested_by_kind=worker_kind,
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth)
    assert exc_info.value.code == "worker_not_permitted"


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
def test_create_widen_extend_no_decision_refused(kind: ChangeKind) -> None:
    auth_none = ChangeAuthority(
        requested_by_kind="owner",
        decision_id=None,
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth_none)
    assert exc_info.value.code == "decision_required"

    auth_empty = ChangeAuthority(
        requested_by_kind="owner",
        decision_id="",
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth_empty)
    assert exc_info.value.code == "decision_required"


@pytest.mark.parametrize("kind", [ChangeKind.create, ChangeKind.widen, ChangeKind.extend])
def test_create_widen_extend_unknown_actor_refused(kind: ChangeKind) -> None:
    auth = ChangeAuthority(
        requested_by_kind="stranger",
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth)
    assert exc_info.value.code == "unknown_actor"


@pytest.mark.parametrize("kind", [ChangeKind.narrow, ChangeKind.revoke])
@pytest.mark.parametrize("actor", ["owner", "client", "automation", "task-agent"])
def test_narrow_revoke_all_known_actors_ok(kind: ChangeKind, actor: str) -> None:
    auth = ChangeAuthority(
        requested_by_kind=actor,
        decision_id=None,
        answered_by_kind=None,
    )
    # No decision or owner signature required for narrow or revoke
    authorize_standing_change(kind, auth)


@pytest.mark.parametrize("kind", [ChangeKind.narrow, ChangeKind.revoke])
def test_narrow_revoke_unknown_actor_refused(kind: ChangeKind) -> None:
    auth = ChangeAuthority(
        requested_by_kind="unknown_kind",
        decision_id=None,
        answered_by_kind=None,
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change(kind, auth)
    assert exc_info.value.code == "unknown_actor"


def test_unknown_change_kind_refused() -> None:
    auth = ChangeAuthority(
        requested_by_kind="owner",
        decision_id="dec-1",
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as exc_info:
        authorize_standing_change("bogus_kind", auth)
    assert exc_info.value.code == "unknown_kind"
