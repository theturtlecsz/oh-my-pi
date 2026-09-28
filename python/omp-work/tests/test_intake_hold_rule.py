from __future__ import annotations

from uuid import UUID, uuid4

from omp_work.v1.intake_hold import (
    IntakeClassification,
    IntakeFacts,
    Link,
    ScopeClass,
    classify_intake,
)

OWNER = "owner"
TASK_AGENT = "task-agent"


def _item(
    work_id: UUID | None = None,
    *,
    state: str = "OPEN",
    archived: bool = False,
    filed_by_kind: str | None = TASK_AGENT,
    description: str = "do the thing",
    intake_approved: bool = False,
) -> IntakeFacts:
    return IntakeFacts(
        work_id=work_id or uuid4(),
        state=state,
        archived=archived,
        filed_by_kind=filed_by_kind,
        description=description,
        intake_approved=intake_approved,
    )


def _parent(source: UUID, target: UUID, *, active: bool = True) -> Link:
    return Link(
        source_work_id=source, target_work_id=target, kind="parent", active=active
    )


def _blocks(source: UUID, target: UUID, *, active: bool = True) -> Link:
    return Link(
        source_work_id=source, target_work_id=target, kind="blocks", active=active
    )


def _related(source: UUID, target: UUID, *, active: bool = True) -> Link:
    return Link(
        source_work_id=source, target_work_id=target, kind="related", active=active
    )


def test_owner_kind_is_owner_filed() -> None:
    item = _item(filed_by_kind=OWNER)
    result = classify_intake([item], [])
    assert result[item.work_id] == IntakeClassification(ScopeClass.owner_filed, False)


def test_missing_kind_backfills_to_owner_filed() -> None:
    item = _item(filed_by_kind=None)
    result = classify_intake([item], [])
    assert result[item.work_id] == IntakeClassification(ScopeClass.owner_filed, False)


def test_unlinked_task_agent_item_is_held_new_scope() -> None:
    item = _item(filed_by_kind=TASK_AGENT)
    result = classify_intake([item], [])
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_flood_created_prefix_marks_owner_kind_bot_filed() -> None:
    item = _item(filed_by_kind=OWNER, description="  [flood-created] x")
    result = classify_intake([item], [])
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_child_of_owner_item_is_follow_up() -> None:
    owner = _item(filed_by_kind=OWNER)
    child = _item()
    result = classify_intake([owner, child], [_parent(child.work_id, owner.work_id)])
    assert result[child.work_id] == IntakeClassification(ScopeClass.follow_up, False)


def test_item_blocking_owner_item_is_follow_up() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    result = classify_intake([owner, item], [_blocks(item.work_id, owner.work_id)])
    assert result[item.work_id] == IntakeClassification(ScopeClass.follow_up, False)


def test_item_blocked_by_owner_item_is_follow_up() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    result = classify_intake([owner, item], [_blocks(owner.work_id, item.work_id)])
    assert result[item.work_id] == IntakeClassification(ScopeClass.follow_up, False)


def test_related_only_link_is_held() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    result = classify_intake([owner, item], [_related(item.work_id, owner.work_id)])
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_canceled_parent_is_not_approved_so_held() -> None:
    parent = _item(filed_by_kind=OWNER, state="CANCELED")
    child = _item()
    result = classify_intake([parent, child], [_parent(child.work_id, parent.work_id)])
    assert result[child.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_cancelled_parent_is_not_approved_so_held() -> None:
    parent = _item(filed_by_kind=OWNER, state="CANCELLED")
    child = _item()
    result = classify_intake([parent, child], [_parent(child.work_id, parent.work_id)])
    assert result[child.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_archived_parent_is_not_approved_so_held() -> None:
    parent = _item(filed_by_kind=OWNER, archived=True)
    child = _item()
    result = classify_intake([parent, child], [_parent(child.work_id, parent.work_id)])
    assert result[child.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_parent_that_is_itself_held_bot_item_is_not_approved() -> None:
    parent = _item(filed_by_kind=TASK_AGENT)
    child = _item()
    result = classify_intake([parent, child], [_parent(child.work_id, parent.work_id)])
    assert result[parent.work_id] == IntakeClassification(ScopeClass.new_scope, True)
    assert result[child.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_intake_approved_parent_is_approved_work() -> None:
    parent = _item(filed_by_kind=TASK_AGENT, intake_approved=True)
    child = _item()
    result = classify_intake([parent, child], [_parent(child.work_id, parent.work_id)])
    assert result[child.work_id] == IntakeClassification(ScopeClass.follow_up, False)


def test_intake_approved_unlinked_is_new_scope_not_held() -> None:
    item = _item(filed_by_kind=TASK_AGENT, intake_approved=True)
    result = classify_intake([item], [])
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, False)


def test_inactive_link_does_not_make_follow_up() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    result = classify_intake(
        [owner, item], [_parent(item.work_id, owner.work_id, active=False)]
    )
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_item_as_parent_of_owner_item_is_held() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    result = classify_intake([owner, item], [_parent(owner.work_id, item.work_id)])
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)


def test_link_to_missing_item_is_ignored() -> None:
    owner = _item(filed_by_kind=OWNER)
    item = _item()
    missing = uuid4()
    result = classify_intake(
        [owner, item],
        [
            _parent(item.work_id, missing),
            _parent(missing, owner.work_id),
        ],
    )
    assert result[item.work_id] == IntakeClassification(ScopeClass.new_scope, True)
