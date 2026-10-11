"""Research program queue, dependencies, evidence refresh, and renewal (OMP-318).

The coordinator records which campaigns were admitted and which job ids are
attached. It does not call the jobs API. This module does not spawn a process
or open a socket or an HTTP client.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from omp_work.standing_change import (
    ChangeAuthority,
    ChangeKind,
    authorize_standing_change,
)
from omp_work.standing_policy import (
    ActionRequest,
    PolicyRefused,
    StandingPolicy,
    covers,
    policy_change_kind,
    validate_policy,
)

_SCHEMA = "research-program.v1"
_RENEWAL_LEAD = timedelta(seconds=3600)

__all__ = [
    "AdmissionGate",
    "CampaignDependency",
    "CampaignRecord",
    "ProgramCoordinator",
    "QuestionItem",
    "QuestionQueue",
    "load_snapshot",
    "refresh_evidence",
    "renew_standing_policy",
]


@dataclass(frozen=True)
class QuestionItem:
    question_id: str
    question: str
    domain: str
    priority: int
    scope: str
    created_at: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.tags, tuple):
            object.__setattr__(self, "tags", tuple(self.tags))


class QuestionQueue:
    """Highest priority first. Equal priority keeps the earliest ``created_at``."""

    def __init__(self, items: Iterable[QuestionItem] = ()) -> None:
        self._items: list[QuestionItem] = list(items)

    def enqueue(self, item: QuestionItem) -> None:
        self._items.append(item)

    def dequeue(self) -> QuestionItem:
        if not self._items:
            raise ValueError("empty_queue")
        return self._items.pop(self._next_index())

    def peek(self) -> QuestionItem | None:
        if not self._items:
            return None
        return self._items[self._next_index()]

    def depth(self) -> int:
        return len(self._items)

    def drain(self) -> list[QuestionItem]:
        ordered = sorted(self._items, key=_queue_key)
        self._items.clear()
        return ordered

    def _next_index(self) -> int:
        return min(range(len(self._items)), key=lambda index: _queue_key(self._items[index]))


def _queue_key(item: QuestionItem) -> tuple[int, str]:
    return (-item.priority, item.created_at)


@dataclass(frozen=True)
class CampaignDependency:
    campaign_id: str
    depends_on: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.depends_on, tuple):
            object.__setattr__(self, "depends_on", tuple(self.depends_on))


class AdmissionGate(Protocol):
    def refuse_reason(self, now: datetime) -> str | None:
        """Return a refusal reason, or None when the campaign may be admitted."""


@dataclass
class CampaignRecord:
    campaign_id: str
    domain: str
    status: str
    job_ids: list[str] = field(default_factory=list)
    reason: str | None = None


class ProgramCoordinator:
    def __init__(
        self,
        program_id: str,
        policies: dict[str, StandingPolicy],
        active_policy_id: str,
        queue: QuestionQueue,
        dependencies: Iterable[CampaignDependency] = (),
        repos: Any = (),
    ) -> None:
        self.program_id = program_id
        self.policies = dict(policies)
        self.active_policy_id = active_policy_id
        self.queue = queue
        self.dependencies = tuple(dependencies)
        self.repos = repos
        self._required = _required_map(self.dependencies)
        _assert_acyclic(self._required)
        self.launched_ids: set[str] = set()
        self.completed_ids: set[str] = set()
        self.campaigns: dict[str, CampaignRecord] = {}
        self.evidence: dict[str, Any] | None = None

    def admit_campaign(
        self,
        campaign_id: str,
        domain: str,
        now: datetime,
        action: ActionRequest,
        gate: AdmissionGate | None = None,
    ) -> None:
        """Admit one campaign under the active standing policy.

        An expired policy is ``standing_policy_expired``. A policy that does
        not cover the action is ``standing_policy_refused``. A gate reason, a
        second launch, and an unfinished predecessor are ``ValueError``.
        """
        active = self.policies[self.active_policy_id]
        if active.expires_at is not None and _at_or_after(now, active.expires_at):
            raise PolicyRefused("standing_policy_expired")
        if not covers(active, action, self.repos, now):
            raise PolicyRefused("standing_policy_refused")
        if gate is not None:
            reason = gate.refuse_reason(now)
            if isinstance(reason, str):
                raise ValueError(reason)
        if campaign_id in self.launched_ids:
            raise ValueError("duplicate_campaign")
        required = self._required.get(campaign_id, ())
        if any(dependency not in self.completed_ids for dependency in required):
            raise ValueError("unmet_dependencies")
        self.campaigns[campaign_id] = CampaignRecord(campaign_id, domain, "admitted")
        self.launched_ids.add(campaign_id)

    def attach_job(self, campaign_id: str, job_id: str) -> None:
        self.campaigns[campaign_id].job_ids.append(job_id)

    def cancel_campaign(self, campaign_id: str, reason: str) -> list[str]:
        campaign = self.campaigns[campaign_id]
        campaign.status = "cancelled"
        campaign.reason = reason
        return list(campaign.job_ids)

    def conclude_campaign(self, campaign_id: str) -> None:
        self.campaigns[campaign_id].status = "concluded"
        self.completed_ids.add(campaign_id)

    def replace_policy(self, policy_id: str, new: StandingPolicy) -> None:
        self.policies[policy_id] = new

    @property
    def ready(self) -> tuple[str, ...]:
        """Dependency ids whose ``depends_on`` campaigns are all completed."""
        seen: set[str] = set()
        ready: list[str] = []
        for item in self.dependencies:
            if item.campaign_id in seen:
                continue
            seen.add(item.campaign_id)
            required = self._required[item.campaign_id]
            if all(dependency in self.completed_ids for dependency in required):
                ready.append(item.campaign_id)
        return tuple(ready)

    def set_evidence(self, result: dict[str, Any]) -> None:
        self.evidence = result

    def save_snapshot(self, directory: str | Path) -> None:
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self._snapshot_payload(), indent=2, sort_keys=True) + "\n"
        _write_atomic(folder / "program.json", text)

    def _snapshot_payload(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "program_id": self.program_id,
            "policies": {
                str(policy_id): _policy_to_json(policy)
                for policy_id, policy in self.policies.items()
            },
            "active_policy_id": str(self.active_policy_id),
            "queue": [_item_to_json(item) for item in self.queue._items],
            "dependencies": [
                {
                    "campaign_id": item.campaign_id,
                    "depends_on": list(item.depends_on),
                }
                for item in self.dependencies
            ],
            "campaigns": [
                {
                    "campaign_id": campaign.campaign_id,
                    "domain": campaign.domain,
                    "status": campaign.status,
                    "job_ids": list(campaign.job_ids),
                }
                for campaign in self.campaigns.values()
            ],
            "launched_ids": list(self.launched_ids),
            "completed_ids": list(self.completed_ids),
            "evidence": self.evidence,
        }


def renew_standing_policy(
    old: StandingPolicy,
    new: StandingPolicy,
    repos: Any,
    now: datetime,
    authority: ChangeAuthority,
) -> StandingPolicy:
    """Return ``new`` when it extends ``old`` inside the pre-expiry hour.

    The window is ``[old.expires_at - 3600s, old.expires_at)``. At or after
    expiry the code is ``standing_policy_expired``. Before the window the code
    is ``outside_renewal_window``. A change that is not an extend is
    ``standing_policy_renewal_refused``. A refusal from
    ``authorize_standing_change`` propagates.
    """
    if old.expires_at is None:
        raise PolicyRefused("outside_renewal_window")
    current, expires = _same_clock(now, old.expires_at)
    if current >= expires:
        raise PolicyRefused("standing_policy_expired")
    if current < expires - _RENEWAL_LEAD:
        raise PolicyRefused("outside_renewal_window")
    if policy_change_kind(old, new) != ChangeKind.extend:
        raise PolicyRefused("standing_policy_renewal_refused")
    validate_policy(new, repos)
    authorize_standing_change(ChangeKind.extend, authority)
    return new


def refresh_evidence(
    matrix: Mapping[str, Any],
    new_sources: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply supporting and contradicting sources onto a claim matrix.

    Unmentioned claims stay as they were. Support appends a passage, records
    the source id, and marks an ``unresolved`` claim ``supported``. A
    contradiction marks the claim ``contested`` and leaves older passages in
    place.
    """
    claims: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for claim in matrix["claims"]:
        copied = {
            "claim_id": claim["claim_id"],
            "status": claim["status"],
            "source_ids": list(claim["source_ids"]),
            "passages": [dict(passage) for passage in claim["passages"]],
        }
        claims.append(copied)
        by_id[copied["claim_id"]] = copied

    refreshed: list[str] = []
    contradictions: list[str] = []
    seen_refreshed: set[str] = set()
    seen_contradictions: set[str] = set()
    for source in new_sources:
        source_id = source["source_id"]
        text = source["text"]
        for claim_id in source["supports"]:
            claim = by_id.get(claim_id)
            if claim is None:
                continue
            claim["passages"].append({"source_id": source_id, "text": text})
            if source_id not in claim["source_ids"]:
                claim["source_ids"].append(source_id)
            if claim["status"] == "unresolved":
                claim["status"] = "supported"
            if claim_id not in seen_refreshed:
                seen_refreshed.add(claim_id)
                refreshed.append(claim_id)
        for claim_id in source["contradicts"]:
            claim = by_id.get(claim_id)
            if claim is None:
                continue
            claim["status"] = "contested"
            if claim_id not in seen_contradictions:
                seen_contradictions.add(claim_id)
                contradictions.append(claim_id)
            if claim_id not in seen_refreshed:
                seen_refreshed.add(claim_id)
                refreshed.append(claim_id)
    return {
        "claims": claims,
        "refreshed_claim_ids": refreshed,
        "contradiction_ids": contradictions,
    }


def load_snapshot(directory: str | Path) -> ProgramCoordinator:
    """Rebuild a coordinator from ``program.json``.

    Repository records are not part of the snapshot. A campaign id that was
    launched still raises ``duplicate_campaign``.
    """
    data = json.loads((Path(directory) / "program.json").read_text(encoding="utf-8"))
    policies: dict[str, StandingPolicy] = {}
    for raw in data["policies"].values():
        policy = _policy_from_json(raw)
        policies[str(policy.policy_id)] = policy
    coordinator = ProgramCoordinator(
        program_id=data["program_id"],
        policies=policies,
        active_policy_id=data["active_policy_id"],
        queue=QuestionQueue(_item_from_json(raw) for raw in data["queue"]),
        dependencies=tuple(
            CampaignDependency(raw["campaign_id"], tuple(raw["depends_on"]))
            for raw in data["dependencies"]
        ),
    )
    for raw in data["campaigns"]:
        coordinator.campaigns[raw["campaign_id"]] = CampaignRecord(
            campaign_id=raw["campaign_id"],
            domain=raw["domain"],
            status=raw["status"],
            job_ids=list(raw["job_ids"]),
        )
    coordinator.launched_ids = set(data["launched_ids"])
    coordinator.completed_ids = set(data["completed_ids"])
    coordinator.evidence = data["evidence"]
    return coordinator


def _required_map(
    dependencies: tuple[CampaignDependency, ...],
) -> dict[str, tuple[str, ...]]:
    required: dict[str, tuple[str, ...]] = {}
    for item in dependencies:
        required[item.campaign_id] = required.get(item.campaign_id, ()) + tuple(item.depends_on)
    return required


def _assert_acyclic(required: Mapping[str, tuple[str, ...]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            raise ValueError("dependency_cycle")
        visiting.add(node)
        for nxt in required.get(node, ()):
            walk(nxt)
        visiting.remove(node)
        visited.add(node)

    for node in required:
        walk(node)


def _same_clock(now: datetime, other: datetime) -> tuple[datetime, datetime]:
    if other.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    elif other.tzinfo is None and now.tzinfo is not None:
        other = other.replace(tzinfo=timezone.utc)
    return now, other


def _at_or_after(now: datetime, expires_at: datetime) -> bool:
    current, expires = _same_clock(now, expires_at)
    return current >= expires


def _policy_to_json(policy: StandingPolicy) -> dict[str, Any]:
    return {
        "policy_id": str(policy.policy_id),
        "action_class": policy.action_class,
        "repositories": list(policy.repositories),
        "destinations": list(policy.destinations),
        "branch_patterns": list(policy.branch_patterns),
        "resource_types": list(policy.resource_types),
        "money_limit_usd": None if policy.money_limit_usd is None else format(policy.money_limit_usd, "f"),
        "expires_at": None if policy.expires_at is None else policy.expires_at.isoformat(),
        "decision_id": None if policy.decision_id is None else str(policy.decision_id),
    }


def _policy_from_json(raw: Mapping[str, Any]) -> StandingPolicy:
    money = raw["money_limit_usd"]
    return StandingPolicy(
        policy_id=raw["policy_id"],
        action_class=raw["action_class"],
        repositories=tuple(raw["repositories"]),
        destinations=tuple(raw["destinations"]),
        branch_patterns=tuple(raw["branch_patterns"]),
        resource_types=tuple(raw["resource_types"]),
        money_limit_usd=None if money is None else Decimal(money),
        expires_at=raw["expires_at"],
        decision_id=raw["decision_id"],
    )


def _item_to_json(item: QuestionItem) -> dict[str, Any]:
    return {
        "question_id": item.question_id,
        "question": item.question,
        "domain": item.domain,
        "priority": item.priority,
        "scope": item.scope,
        "created_at": item.created_at,
        "tags": list(item.tags),
    }


def _item_from_json(raw: Mapping[str, Any]) -> QuestionItem:
    return QuestionItem(
        question_id=raw["question_id"],
        question=raw["question"],
        domain=raw["domain"],
        priority=raw["priority"],
        scope=raw["scope"],
        created_at=raw["created_at"],
        tags=tuple(raw["tags"]),
    )


def _write_atomic(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
