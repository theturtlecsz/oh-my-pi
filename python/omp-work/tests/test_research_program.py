"""R15 program queue, dependencies, renewal, evidence refresh, and snapshot."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from omp_work.research.program import (
    CampaignDependency,
    ProgramCoordinator,
    QuestionItem,
    QuestionQueue,
    load_snapshot,
    refresh_evidence,
    renew_standing_policy,
)
from omp_work.standing_change import ChangeAuthority, StandingChangeRefused
from omp_work.standing_policy import ActionRequest, PolicyRefused, StandingPolicy

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _policy(
    policy_id: str,
    limit: str,
    expires_at: datetime | None,
    decision_id: str = "dec-1",
) -> StandingPolicy:
    return StandingPolicy(
        policy_id=policy_id,
        action_class="spend_beyond_threshold",
        money_limit_usd=Decimal(limit),
        expires_at=expires_at,
        decision_id=decision_id,
    )


def _action(amount: str = "1.00") -> ActionRequest:
    return ActionRequest(
        action_class="spend_beyond_threshold",
        amount_usd=Decimal(amount),
    )


def _question(question_id: str, priority: int, created_at: str, tags: tuple[str, ...] = ()) -> QuestionItem:
    return QuestionItem(
        question_id=question_id,
        question=question_id,
        domain="engineering",
        priority=priority,
        scope="repo",
        created_at=created_at,
        tags=tags,
    )


def _owner(decision_id: str) -> ChangeAuthority:
    return ChangeAuthority(
        requested_by_kind="owner",
        decision_id=decision_id,
        answered_by_kind="owner",
    )


def _renewal_pair() -> tuple[datetime, StandingPolicy, StandingPolicy]:
    expires = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    old = _policy("pol", "10.00", expires, decision_id="dec-old")
    new = _policy("pol", "10.00", expires + timedelta(days=1), decision_id="dec-new")
    return expires, old, new


def test_question_queue_dequeues_highest_priority_then_earliest() -> None:
    queue = QuestionQueue()
    lower = _question("q-low", 1, "2026-10-02T00:00:02Z")
    higher = _question("q-high", 2, "2026-10-02T00:00:03Z")
    queue.enqueue(lower)
    queue.enqueue(higher)
    assert queue.depth() == 2
    assert queue.peek() == higher
    assert queue.dequeue() == higher
    assert queue.dequeue() == lower

    tied = QuestionQueue()
    later = _question("late", 2, "2026-10-02T00:00:03Z")
    earlier = _question("early", 2, "2026-10-02T00:00:02Z")
    tied.enqueue(later)
    tied.enqueue(earlier)
    assert tied.dequeue() == earlier
    assert tied.drain() == [later]
    assert tied.depth() == 0
    assert tied.peek() is None
    with pytest.raises(ValueError, match="^empty_queue$"):
        tied.dequeue()


def test_dependency_cycle_is_refused() -> None:
    with pytest.raises(ValueError, match="^dependency_cycle$"):
        ProgramCoordinator(
            "program-1",
            {"spend": _policy("spend", "10.00", None)},
            "spend",
            QuestionQueue(),
            (
                CampaignDependency("A", ("B",)),
                CampaignDependency("B", ("A",)),
            ),
        )


def test_dependent_campaign_waits_until_predecessor_is_concluded() -> None:
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", None)},
        "spend",
        QuestionQueue(),
        (
            CampaignDependency("A", ()),
            CampaignDependency("B", ("A",)),
        ),
    )
    assert coordinator.ready == ("A",)
    with pytest.raises(ValueError, match="^unmet_dependencies$"):
        coordinator.admit_campaign("B", "engineering", NOW, _action())
    coordinator.admit_campaign("A", "engineering", NOW, _action())
    with pytest.raises(ValueError, match="^unmet_dependencies$"):
        coordinator.admit_campaign("B", "engineering", NOW, _action())
    coordinator.conclude_campaign("A")
    assert coordinator.campaigns["A"].status == "concluded"
    assert coordinator.completed_ids == {"A"}
    assert coordinator.ready == ("A", "B")
    coordinator.admit_campaign("B", "engineering", NOW, _action())
    assert coordinator.campaigns["B"].status == "admitted"
    assert coordinator.launched_ids == {"A", "B"}


def test_renewal_returns_the_extended_policy_inside_the_window() -> None:
    expires, old, new = _renewal_pair()
    authority = _owner(str(new.decision_id))
    at_open = renew_standing_policy(
        old,
        new,
        (),
        expires - timedelta(seconds=3600),
        authority,
    )
    assert at_open == new
    still_open = renew_standing_policy(
        old,
        new,
        (),
        expires - timedelta(seconds=1),
        authority,
    )
    assert still_open == new
    assert still_open.expires_at is not None
    assert old.expires_at is not None
    assert still_open.expires_at > old.expires_at


def test_renewal_refuses_expiry_early_window_and_a_task_agent() -> None:
    expires, old, new = _renewal_pair()
    authority = _owner(str(new.decision_id))
    with pytest.raises(PolicyRefused, match="^standing_policy_expired$") as expired:
        renew_standing_policy(old, new, (), expires, authority)
    assert expired.value.code == "standing_policy_expired"
    with pytest.raises(PolicyRefused, match="^standing_policy_expired$") as after:
        renew_standing_policy(old, new, (), expires + timedelta(seconds=1), authority)
    assert after.value.code == "standing_policy_expired"

    with pytest.raises(PolicyRefused, match="^outside_renewal_window$") as early:
        renew_standing_policy(
            old,
            new,
            (),
            expires - timedelta(seconds=3601),
            authority,
        )
    assert early.value.code == "outside_renewal_window"

    widened = _policy("pol", "11.00", expires + timedelta(days=1), decision_id="dec-new")
    with pytest.raises(PolicyRefused, match="^standing_policy_renewal_refused$") as refused:
        renew_standing_policy(old, widened, (), expires - timedelta(minutes=30), authority)
    assert refused.value.code == "standing_policy_renewal_refused"

    worker = ChangeAuthority(
        requested_by_kind="task-agent",
        decision_id=new.decision_id,
        answered_by_kind="owner",
    )
    with pytest.raises(StandingChangeRefused) as caught:
        renew_standing_policy(old, new, (), expires - timedelta(minutes=30), worker)
    assert caught.value.code == "worker_not_permitted"


def test_expired_active_policy_refuses_admission() -> None:
    expires = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", expires)},
        "spend",
        QuestionQueue(),
    )
    coordinator.admit_campaign("early", "engineering", expires - timedelta(seconds=1), _action())
    assert coordinator.campaigns["early"].status == "admitted"
    with pytest.raises(PolicyRefused, match="^standing_policy_expired$") as caught:
        coordinator.admit_campaign("late", "engineering", expires, _action())
    assert caught.value.code == "standing_policy_expired"
    assert "late" not in coordinator.launched_ids


def test_uncovered_action_and_admission_gate_refuse_before_launch() -> None:
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", None)},
        "spend",
        QuestionQueue(),
    )
    with pytest.raises(PolicyRefused, match="^standing_policy_refused$") as refused:
        coordinator.admit_campaign("over", "engineering", NOW, _action("10.01"))
    assert refused.value.code == "standing_policy_refused"
    assert coordinator.launched_ids == set()

    class Quota:
        def refuse_reason(self, now: datetime) -> str | None:
            assert now == NOW
            return "over_quota"

    with pytest.raises(ValueError, match="^over_quota$"):
        coordinator.admit_campaign("gated", "engineering", NOW, _action(), gate=Quota())
    assert coordinator.launched_ids == set()

    class Open:
        def refuse_reason(self, now: datetime) -> str | None:
            return None

    coordinator.admit_campaign("open", "engineering", NOW, _action(), gate=Open())
    assert coordinator.campaigns["open"].status == "admitted"


def test_second_admit_is_a_duplicate() -> None:
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", None)},
        "spend",
        QuestionQueue(),
    )
    coordinator.admit_campaign("A", "engineering", NOW, _action())
    with pytest.raises(ValueError, match="^duplicate_campaign$"):
        coordinator.admit_campaign("A", "engineering", NOW, _action())
    assert coordinator.launched_ids == {"A"}
    assert coordinator.campaigns["A"].status == "admitted"


def test_cancel_campaign_returns_attached_jobs() -> None:
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", None)},
        "spend",
        QuestionQueue(),
    )
    coordinator.admit_campaign("A", "engineering", NOW, _action())
    coordinator.attach_job("A", "job-1")
    coordinator.attach_job("A", "job-2")
    assert coordinator.cancel_campaign("A", "stopped") == ["job-1", "job-2"]
    assert coordinator.campaigns["A"].status == "cancelled"
    with pytest.raises(ValueError, match="^duplicate_campaign$"):
        coordinator.admit_campaign("A", "engineering", NOW, _action())


def test_replace_policy_changes_one_policy_only() -> None:
    queue = QuestionQueue()
    queue.enqueue(_question("q", 1, "2026-10-02T00:00:02Z"))
    other = _policy("other", "20.00", None, decision_id="dec-other")
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": _policy("spend", "10.00", None), "other": other},
        "spend",
        queue,
    )
    coordinator.admit_campaign("A", "engineering", NOW, _action())
    coordinator.replace_policy("spend", _policy("spend", "15.00", None))
    assert queue.depth() == 1
    assert coordinator.queue.depth() == 1
    assert coordinator.policies["other"] is other
    assert coordinator.policies["other"].money_limit_usd == Decimal("20.00")
    assert coordinator.policies["spend"].money_limit_usd == Decimal("15.00")
    assert coordinator.launched_ids == {"A"}
    assert coordinator.campaigns["A"].status == "admitted"


def test_refresh_evidence_supports_then_contests() -> None:
    matrix = {
        "claims": [
            {
                "claim_id": "c1",
                "status": "unresolved",
                "source_ids": ["s0"],
                "passages": [{"source_id": "s0", "text": "old passage"}],
            },
            {
                "claim_id": "c2",
                "status": "unresolved",
                "source_ids": [],
                "passages": [{"source_id": "s9", "text": "untouched"}],
            },
        ]
    }
    supported = refresh_evidence(
        matrix,
        [
            {
                "source_id": "s1",
                "text": "new support",
                "supports": ["c1"],
                "contradicts": [],
            }
        ],
    )
    assert matrix["claims"][0]["status"] == "unresolved"
    assert matrix["claims"][0]["passages"] == [{"source_id": "s0", "text": "old passage"}]
    claim = supported["claims"][0]
    assert claim["claim_id"] == "c1"
    assert claim["status"] == "supported"
    assert claim["passages"] == [
        {"source_id": "s0", "text": "old passage"},
        {"source_id": "s1", "text": "new support"},
    ]
    assert claim["source_ids"] == ["s0", "s1"]
    assert supported["claims"][1] == matrix["claims"][1]
    assert supported["refreshed_claim_ids"] == ["c1"]
    assert supported["contradiction_ids"] == []

    contested = refresh_evidence(
        supported,
        [
            {
                "source_id": "s2",
                "text": "conflict",
                "supports": [],
                "contradicts": ["c1"],
            }
        ],
    )
    claim = contested["claims"][0]
    assert claim["status"] == "contested"
    assert claim["passages"] == [
        {"source_id": "s0", "text": "old passage"},
        {"source_id": "s1", "text": "new support"},
    ]
    assert contested["claims"][1]["status"] == "unresolved"
    assert contested["contradiction_ids"] == ["c1"]
    assert "c1" in contested["refreshed_claim_ids"]
    assert "c2" not in contested["refreshed_claim_ids"]


def test_snapshot_restores_launches_queue_evidence_and_refuses_relaunched_id(
    tmp_path: Path,
) -> None:
    expires = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
    active = _policy("spend", "42.50", expires)
    other = _policy("other", "7.00", None, decision_id="dec-other")
    queue = QuestionQueue()
    low = _question("q-low", 1, "2026-10-02T00:00:02Z", tags=("keep",))
    high = _question("q-high", 2, "2026-10-02T00:00:03Z")
    queue.enqueue(low)
    queue.enqueue(high)
    coordinator = ProgramCoordinator(
        "program-1",
        {"spend": active, "other": other},
        "spend",
        queue,
        (
            CampaignDependency("A", ()),
            CampaignDependency("B", ("A",)),
        ),
    )
    coordinator.admit_campaign("A", "engineering", NOW, _action())
    coordinator.conclude_campaign("A")
    coordinator.admit_campaign("B", "engineering", NOW, _action("5.00"))
    coordinator.attach_job("B", "job-b")
    evidence = refresh_evidence(
        {
            "claims": [
                {
                    "claim_id": "c1",
                    "status": "unresolved",
                    "source_ids": ["s0"],
                    "passages": [{"source_id": "s0", "text": "old passage"}],
                }
            ]
        },
        [
            {
                "source_id": "s1",
                "text": "new support",
                "supports": ["c1"],
                "contradicts": [],
            }
        ],
    )
    coordinator.set_evidence(evidence)
    coordinator.save_snapshot(tmp_path)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["program.json"]
    raw = json.loads((tmp_path / "program.json").read_text(encoding="utf-8"))
    assert raw["schema"] == "research-program.v1"
    assert raw["policies"]["spend"]["money_limit_usd"] == "42.50"
    assert isinstance(raw["policies"]["spend"]["money_limit_usd"], str)
    assert raw["policies"]["spend"]["expires_at"] == expires.isoformat()
    assert raw["policies"]["other"]["expires_at"] is None
    assert raw["policies"]["other"]["money_limit_usd"] == "7.00"
    assert isinstance(raw["policies"]["spend"]["repositories"], list)
    assert {item["question_id"] for item in raw["queue"]} == {"q-low", "q-high"}
    assert set(raw["launched_ids"]) == {"A", "B"}
    assert set(raw["completed_ids"]) == {"A"}

    restored = load_snapshot(tmp_path)
    assert restored.launched_ids == {"A", "B"}
    assert restored.completed_ids == {"A"}
    assert restored.policies["spend"] == active
    assert restored.policies["spend"].money_limit_usd == Decimal("42.50")
    assert restored.policies["other"] == other
    assert restored.queue.depth() == 2
    restored_items = restored.queue.drain()
    assert [item.question_id for item in restored_items] == ["q-high", "q-low"]
    assert restored_items[1].tags == ("keep",)
    assert restored.evidence == evidence
    assert restored.campaigns["A"].status == "concluded"
    assert restored.campaigns["B"].status == "admitted"
    assert restored.campaigns["B"].job_ids == ["job-b"]
    with pytest.raises(ValueError, match="^duplicate_campaign$"):
        restored.admit_campaign("B", "engineering", NOW, _action("5.00"))
    with pytest.raises(ValueError, match="^duplicate_campaign$"):
        restored.admit_campaign("A", "engineering", NOW, _action())
