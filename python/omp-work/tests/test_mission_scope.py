"""D29 mission-scope rules: status transitions and material-change cases."""

from __future__ import annotations

from typing import Any, Callable

import pytest

from omp_work.mission_scope import (
    ALLOWED_TRANSITIONS,
    MISSION_STATUSES,
    TERMINAL_STATUSES,
    material_cases,
    outside_envelope,
    transition_allowed,
)

_BUDGET: dict[str, Any] = {
    "usd": "10.00",
    "tokens": 1000,
    "wall_clock_seconds": 3600,
    "max_subagents": 4,
}

_DRAFT_KEYS = (
    "project_id",
    "objective",
    "acceptance_criteria",
    "repositories",
    "requested_capabilities",
    "approval_classes",
    "budget_policy",
)

# Spec matrix. Abandoned is added for every non-terminal; terminals have no exits.
_SPEC_TARGETS: dict[str, set[str]] = {
    "draft": {"awaiting_confirmation", "blocked", "abandoned"},
    "awaiting_confirmation": {"approved", "blocked", "abandoned"},
    "approved": {"running", "paused", "blocked", "awaiting_confirmation", "abandoned"},
    "running": {"paused", "blocked", "completed", "failed", "awaiting_confirmation", "abandoned"},
    "paused": {"running", "blocked", "awaiting_confirmation", "abandoned"},
    "blocked": {"awaiting_confirmation", "running", "paused", "failed", "abandoned"},
    "completed": set(),
    "failed": set(),
    "abandoned": set(),
}


def _budget(**overrides: Any) -> dict[str, Any]:
    policy = dict(_BUDGET)
    policy.update(overrides)
    return policy


def _draft(**overrides: Any) -> dict[str, Any]:
    draft: dict[str, Any] = {
        "project_id": "project-1",
        "objective": "Ship the widget",
        "acceptance_criteria": ["criterion one", "criterion two"],
        "repositories": ["repo-a", "repo-b"],
        "requested_capabilities": ["read", "test"],
        "approval_classes": ["tier-1"],
        "budget_policy": _budget(),
        "constraints": ["keep the public API"],
    }
    draft.update(overrides)
    return draft


def test_status_constants() -> None:
    assert MISSION_STATUSES == (
        "draft",
        "awaiting_confirmation",
        "approved",
        "running",
        "paused",
        "blocked",
        "completed",
        "failed",
        "abandoned",
    )
    assert TERMINAL_STATUSES == ("completed", "failed", "abandoned")
    assert set(TERMINAL_STATUSES) <= set(MISSION_STATUSES)
    assert len(set(MISSION_STATUSES)) == 9


def test_every_status_pair() -> None:
    assert set(MISSION_STATUSES) == set(_SPEC_TARGETS)
    assert set(ALLOWED_TRANSITIONS) == set(_SPEC_TARGETS)
    for current, targets in _SPEC_TARGETS.items():
        assert set(ALLOWED_TRANSITIONS[current]) == targets
        for target in _SPEC_TARGETS:
            assert transition_allowed(current, target) is (target in targets)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("draft", "nope"),
        ("nope", "running"),
        ("nope", "nope"),
        ("COMPLETED", "abandoned"),
        ("", "draft"),
    ],
)
def test_unknown_status_is_refused(current: str, target: str) -> None:
    assert transition_allowed(current, target) is False


def test_each_material_case_alone() -> None:
    approved = _draft()
    assert material_cases(approved, _draft(
        acceptance_criteria=["criterion one", "criterion two", "criterion three"],
    )) == ("a",)
    assert material_cases(approved, _draft(acceptance_criteria=["criterion two"])) == ("b",)
    assert material_cases(approved, _draft(project_id="project-2")) == ("c",)
    assert material_cases(approved, _draft(
        repositories=["repo-a", "repo-b", "repo-c"],
    )) == ("c",)
    assert material_cases(approved, _draft(
        requested_capabilities=["read", "test", "write"],
    )) == ("d",)
    assert material_cases(approved, _draft(
        approval_classes=["tier-1", "tier-3"],
    )) == ("d",)
    assert material_cases(approved, _draft(budget_policy=None)) == ("e",)
    assert material_cases(approved, _draft(objective="Ship a different widget")) == ("f",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("usd", "10.01"),
        ("usd", "10.001"),
        ("tokens", 1001),
        ("wall_clock_seconds", 3601),
        ("max_subagents", 5),
    ],
)
def test_each_higher_budget_field_is_e(field: str, value: Any) -> None:
    assert material_cases(_draft(), _draft(budget_policy=_budget(**{field: value}))) == ("e",)


def test_usd_higher_uses_decimal_not_text_order() -> None:
    approved = _draft(budget_policy=_budget(usd="9.00"))
    revised = _draft(budget_policy=_budget(usd="10.00"))
    assert material_cases(approved, revised) == ("e",)


def test_equal_usd_scale_is_not_higher() -> None:
    assert material_cases(_draft(), _draft(budget_policy=_budget(usd="10.000"))) == ()


def test_in_scope_revision_has_no_material_case() -> None:
    revised = _draft(
        acceptance_criteria=["criterion two", "criterion one"],
        constraints=["keep the public API stable"],
        repositories=["repo-a"],
        budget_policy=_budget(
            usd="9.00",
            tokens=800,
            wall_clock_seconds=1800,
            max_subagents=1,
        ),
        objective="  Ship\tthe   widget\n",
    )
    assert material_cases(_draft(), revised) == ()
    assert outside_envelope(()) is False


def test_whitespace_equivalent_criteria_and_casefold_objective_are_in_scope() -> None:
    revised = _draft(
        acceptance_criteria=["  criterion   two \n", "criterion\tone"],
        objective="  SHIP   the   widget ",
    )
    assert material_cases(_draft(), revised) == ()


def test_criterion_case_change_is_replacement() -> None:
    revised = _draft(acceptance_criteria=["Criterion one", "criterion two"])
    assert material_cases(_draft(), revised) == ("a", "b")


def test_narrowing_repositories_capabilities_and_classes_is_in_scope() -> None:
    revised = _draft(
        repositories=["repo-b"],
        requested_capabilities=["read"],
        approval_classes=[],
    )
    assert material_cases(_draft(), revised) == ()


def test_adding_a_budget_is_not_a_raise() -> None:
    assert material_cases(_draft(budget_policy=None), _draft()) == ()


def test_material_codes_are_sorted() -> None:
    revised = _draft(
        project_id="project-2",
        objective="Something else",
        acceptance_criteria=["criterion two", "criterion three"],
        repositories=["repo-a", "repo-b", "repo-c"],
        requested_capabilities=["read", "test", "write"],
        budget_policy=_budget(tokens=1001),
    )
    assert material_cases(_draft(), revised) == ("a", "b", "c", "d", "e", "f")


def test_approved_none_is_new() -> None:
    assert material_cases(None, _draft()) == ("new",)
    assert material_cases(None, None) == ("new",)
    assert material_cases(None, {"budget_policy": "nope"}) == ("new",)


@pytest.mark.parametrize("approved", [[], "draft", 0, False, ""])
def test_approved_wrong_type_is_undecidable(approved: Any) -> None:
    assert material_cases(approved, _draft()) == ("undecidable",)


def _drop(key: str) -> Callable[[dict[str, Any]], None]:
    def mutate(draft: dict[str, Any]) -> None:
        del draft[key]

    return mutate


def _set(key: str, value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(draft: dict[str, Any]) -> None:
        draft[key] = value

    return mutate


_MALFORMED: list[Callable[[dict[str, Any]], None]] = [
    _drop(key) for key in _DRAFT_KEYS
] + [
    _set("project_id", 1),
    _set("objective", None),
    _set("objective", ["Ship the widget"]),
    _set("acceptance_criteria", "criterion one"),
    _set("acceptance_criteria", ["criterion one", 2]),
    _set("acceptance_criteria", {"criterion one"}),
    _set("repositories", "repo-a"),
    _set("repositories", ["repo-a", None]),
    _set("requested_capabilities", None),
    _set("requested_capabilities", "read"),
    _set("approval_classes", "tier-1"),
    _set("approval_classes", ["tier-1", 3]),
    _set("budget_policy", "10.00"),
    _set("budget_policy", []),
    _set("budget_policy", {"tokens": 1, "wall_clock_seconds": 1, "max_subagents": 1}),
    _set("budget_policy", {**_BUDGET, "usd": 10}),
    _set("budget_policy", {**_BUDGET, "usd": "nope"}),
    _set("budget_policy", {**_BUDGET, "usd": "NaN"}),
    _set("budget_policy", {**_BUDGET, "usd": "Infinity"}),
    _set("budget_policy", {**_BUDGET, "tokens": True}),
    _set("budget_policy", {**_BUDGET, "tokens": "1000"}),
    _set("budget_policy", {**_BUDGET, "tokens": 1.5}),
    _set("budget_policy", {**_BUDGET, "wall_clock_seconds": None}),
    _set("budget_policy", {**_BUDGET, "max_subagents": False}),
]


@pytest.mark.parametrize("mutate", _MALFORMED)
@pytest.mark.parametrize("side", ["approved", "revised"])
def test_missing_key_or_wrong_type_is_undecidable(
    mutate: Callable[[dict[str, Any]], None],
    side: str,
) -> None:
    approved = _draft()
    revised = _draft()
    mutate(approved if side == "approved" else revised)
    assert material_cases(approved, revised) == ("undecidable",)


@pytest.mark.parametrize("revised", [None, [], "draft", 1])
def test_revised_wrong_type_is_undecidable(revised: Any) -> None:
    assert material_cases(_draft(), revised) == ("undecidable",)


def test_one_bad_field_hides_other_material_cases() -> None:
    revised = _draft(
        project_id="project-2",
        acceptance_criteria=["criterion one", "criterion two", "criterion three"],
    )
    revised["objective"] = None
    assert material_cases(_draft(), revised) == ("undecidable",)


@pytest.mark.parametrize(
    ("code", "outside"),
    [
        ("a", False),
        ("b", False),
        ("c", True),
        ("d", False),
        ("e", False),
        ("f", False),
        ("new", True),
        ("undecidable", True),
    ],
)
def test_outside_envelope_per_code(code: str, outside: bool) -> None:
    assert outside_envelope((code,)) is outside


def test_outside_envelope_is_true_when_an_owner_code_is_present() -> None:
    assert outside_envelope(("a", "b", "d", "e", "f")) is False
    assert outside_envelope(("a", "c")) is True
    assert outside_envelope(("f", "undecidable")) is True
    assert outside_envelope(material_cases(None, _draft())) is True
    assert outside_envelope(material_cases(_draft(), _draft(project_id="other"))) is True
    assert outside_envelope(material_cases(_draft(), None)) is True
    assert outside_envelope(material_cases(_draft(), _draft(objective="else"))) is False
