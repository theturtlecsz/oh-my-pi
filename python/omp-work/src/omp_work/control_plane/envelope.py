"""Command envelope classification, proposal binding, and the tier gate (OMP-421).

An incoming command is classified into an action class from its type and the
scope that authorizes it (``WorkService._scopes``), bound onto the LLM proposal
that asked for it, and then gated by tier: tier 1 runs unattended, tier 2 needs
a covering standing policy, and tier 3 (or any unlisted class) needs the owner's
signature. ``action_tiers.tier_of`` is never used here: a class must be listed
explicitly, so an unlisted class fails closed instead of defaulting to tier 3.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

from omp_work.action_tiers import TIER1, TIER2, TIER3
from omp_work.control_plane.gate import (
    Check,
    CheckContext,
    Proposal,
    Refusal,
    verify_owner_authorization,
)
from omp_work.standing_policy import ActionRequest, StandingPolicy, covers

__all__ = [
    "bind_command",
    "classify_command",
    "tier_gate",
]

# Scope -> class for a command with no type-specific override.
_SCOPE_CLASSES: dict[str, str] = {
    "work.mutate": "update_mission_state",
    "work.execute": "update_mission_state",
    "work.close": "update_mission_state",
    "work.approve": "publish_as_owner",
    "work.stop": "pause_resume",
}

# A new or materially changed mission is a scope broadening (D29), whatever the
# scope that authorizes the command itself.
_BROADEN_SCOPE_COMMANDS: frozenset[str] = frozenset(
    {"submit_mission", "revise_mission", "approve_mission"}
)

_MISSION_RUNNING: frozenset[str] = frozenset({"running", "paused", "blocked"})
_MISSION_TERMINAL: frozenset[str] = frozenset({"completed", "failed"})


def _command_mission_id(command: Any) -> Any:
    payload = getattr(command, "payload", None)
    return getattr(payload, "mission_id", None)


def _bindable_mission_id(command: Any) -> UUID | None:
    """Return the command's mission id in the form a ``Proposal`` can hold.

    Mission commands carry a ``UUID``; a decision record's ``mission_id`` is a
    free-form string. ``Proposal.mission_id`` is a UUID and coerces strings in
    its constructor, so a value it cannot represent binds as ``None`` instead
    of raising out of ``bind_command``.
    """
    value = _command_mission_id(command)
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _mission_target_status(command: Any) -> Any:
    payload = getattr(command, "payload", None)
    return getattr(payload, "target_status", None)


def classify_command(command: Any, scope: str) -> str | None:
    """Return the action class a command is classified as under ``scope``.

    ``scope`` is passed in (``WorkService._scopes``) so this module never
    imports the v1 contract. Returns ``None`` when the command is unclassified:
    an abandoned mission, or a scope that grants no classified action.
    """
    command_type = getattr(command, "type", None)

    if command_type in _BROADEN_SCOPE_COMMANDS:
        return "broaden_scope"
    if command_type == "release_stop":
        return "disable_safeguards"
    if command_type == "set_mission_status":
        target = _mission_target_status(command)
        if target in _MISSION_RUNNING:
            return "pause_resume"
        if target in _MISSION_TERMINAL:
            return "update_mission_state"
        return None

    return _SCOPE_CLASSES.get(scope)


def bind_command(proposal: Proposal, envelope: Any, scope: str) -> Proposal | Refusal:
    """Bind a proposal to the command it requested, or refuse the mismatch.

    The proposal may assert an operation, workspace, command type, action class,
    and mission id; every asserted value must agree with the command. An
    unasserted value (``None``) is filled from the command. On success the
    returned proposal carries the command's type, classified action class, and
    mission id; otherwise the refusal raises no decision — the proposal itself
    is malformed, not an authorization question.
    """
    command = getattr(envelope, "command", None)
    command_type = getattr(command, "type", None)
    workspace_id = getattr(envelope, "workspace_id", None)
    action_class = classify_command(command, scope)
    mission_id = _bindable_mission_id(command)

    mismatch = (
        proposal.operation != "command"
        or proposal.workspace_id != workspace_id
        or (proposal.command_type is not None and proposal.command_type != command_type)
        or (proposal.action_class is not None and proposal.action_class != action_class)
        or (proposal.mission_id is not None and proposal.mission_id != mission_id)
    )
    if mismatch:
        return Refusal(
            check="command_binding",
            code="classification_mismatch",
            raise_decision=False,
        )

    return replace(
        proposal,
        command_type=command_type,
        action_class=action_class,
        mission_id=mission_id,
    )


def _bounds_equal(left: StandingPolicy, right: StandingPolicy) -> bool:
    return (
        set(left.repositories) == set(right.repositories)
        and set(left.destinations) == set(right.destinations)
        and set(left.branch_patterns) == set(right.branch_patterns)
        and set(left.resource_types) == set(right.resource_types)
        and left.money_limit_usd == right.money_limit_usd
    )


def _tier_gate(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    # The class is matched raw: stripping it would move a padded string that no
    # tier lists (" modify_files") into a tier and let it skip the owner gate.
    action_class = proposal.action_class

    if action_class in TIER1:
        return None

    if action_class in TIER3:
        code = verify_owner_authorization(proposal, ctx)
        if code is None:
            return None
        return Refusal(
            check="tier_gate",
            code=code,
            raise_decision=True,
            action_class=action_class,
        )

    if action_class in TIER2:
        return _tier2_gate(proposal, ctx)

    # None, blank, or a class no tier lists: D40 fail closed — only the owner's
    # authorization lets it proceed, and it is decided as an unlisted action.
    code = verify_owner_authorization(proposal, ctx)
    if code is None:
        return None
    return Refusal(
        check="tier_gate",
        code="unclassified_action",
        raise_decision=True,
        action_class="unlisted",
    )


def _tier2_gate(proposal: Proposal, ctx: CheckContext) -> Refusal | None:
    action: ActionRequest | None = proposal.action
    if action is None:
        return Refusal(
            check="tier_gate", code="standing_policy_missing", raise_decision=True
        )

    facts = ctx.facts
    covering = [
        policy
        for policy in facts.standing_policies
        if covers(policy, action, facts.repositories, facts.now)
    ]
    if not covering:
        return Refusal(
            check="tier_gate", code="standing_policy_missing", raise_decision=True
        )

    first = covering[0]
    if any(not _bounds_equal(first, other) for other in covering[1:]):
        return Refusal(
            check="tier_gate", code="policy_conflict", raise_decision=True
        )
    return None


tier_gate = Check(name="tier_gate", fn=_tier_gate)
