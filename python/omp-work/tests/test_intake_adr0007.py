"""OMP-521-s01: ADR 0007 pins the neurosymbolic intake vocabulary.

Failure mode defended: a reader of
``docs/adr/0007-neurosymbolic-intake-vocabulary-and-authority.md`` codes an
NSI-1 schema against an intake taxonomy, status model, or authority rule the
ADR no longer states — a category is renamed, a lifecycle state or transition
disappears, the ratification/readiness separation collapses, or the authority
matrix grants the model or the Markdown projection power over canonical
state. The test parses the ADR and requires the structure, the eleven
categories, the ten lifecycle states with their transition table, the
readiness/ratification separation, the five-actor authority matrix, the
unknown and waiver semantics, and the client-contract conformance line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ADR = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "adr"
    / "0007-neurosymbolic-intake-vocabulary-and-authority.md"
)

_CATEGORIES = (
    "functional_scope_and_behavior",
    "domain_and_data_model",
    "interaction_and_ux_flow",
    "non_functional_qualities",
    "integrations_and_external_dependencies",
    "edge_cases_and_failure_handling",
    "constraints_and_tradeoffs",
    "terminology",
    "completion_signals",
    "placeholders_and_ambiguities",
    "scope_boundaries",
)

_STATES = (
    "collecting",
    "reasoning",
    "needs_spec",
    "conflicted",
    "ready_for_ratification",
    "ratified",
    "published",
    "stale",
    "invalid",
    "superseded",
)

_TRANSITIONS = (
    ("collecting", "reasoning", "rule engine"),
    ("reasoning", "needs_spec", "rule engine"),
    ("reasoning", "conflicted", "rule engine"),
    ("reasoning", "ready_for_ratification", "rule engine"),
    ("needs_spec", "collecting", "owner"),
    ("conflicted", "collecting", "owner"),
    ("ready_for_ratification", "ratified", "owner"),
    ("ratified", "published", "rule engine"),
    ("published", "stale", "rule engine"),
    ("stale", "reasoning", "rule engine"),
)

_AUTHORITY_ACTORS = (
    "Owner",
    "Repository/tool",
    "Model",
    "Rule engine",
    "Markdown projection",
)


def _text() -> str:
    return _ADR.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    lines = text.splitlines()
    try:
        start = lines.index(heading)
    except ValueError:
        pytest.fail(f"{_ADR} has no {heading!r} section")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## ") and not lines[index].startswith("### "):
            end = index
            break
    return "\n".join(lines[start:end])


def test_adr0007_structure() -> None:
    lines = _text().splitlines()
    assert lines[0] == (
        "# ADR 0007: Neurosymbolic intake vocabulary and authority"
    ), f"{_ADR}: title line changed"
    assert any(line.startswith("**Status:**") for line in lines), (
        f"{_ADR}: status line is missing"
    )
    assert any(line.startswith("**Date:**") for line in lines), (
        f"{_ADR}: date line is missing"
    )
    headings = [line for line in lines if line.startswith("## ")]
    assert headings[:2] == ["## Context", "## Decision"], (
        f"{_ADR}: Context and Decision must be the first two sections"
    )
    for subsection in (
        "### 1. Intake categories",
        "### 2. Vocabulary",
        "### 3. Status model",
        "### 4. Authority matrix",
        "### 5. Unknown semantics",
        "### 6. Waiver policy",
        "### 7. Client contract conformance",
    ):
        assert subsection in lines, f"{_ADR}: {subsection!r} is missing"


def test_adr0007_categories() -> None:
    section = _section(_text(), "### 1. Intake categories")
    found = re.findall(r"^\d+\.\s+`([a-z_]+)`", section, re.MULTILINE)
    assert found == list(_CATEGORIES), (
        f"{_ADR}: the numbered category list must be exactly the eleven "
        f"canonical categories in order, got {found}"
    )


def test_adr0007_vocabulary_terms() -> None:
    section = _section(_text(), "### 2. Vocabulary")
    for term in (
        "Entities",
        "Edges",
        "Claims",
        "Constraints",
        "Evidence spans",
        "Source provenance",
    ):
        assert f"**{term}**" in section, (
            f"{_ADR}: vocabulary term {term!r} is missing"
        )


def test_adr0007_status_model_states_and_transitions() -> None:
    section = _section(_text(), "### 3. Status model")
    for state in _STATES:
        assert f"`{state}`" in section, (
            f"{_ADR}: lifecycle state {state!r} is missing"
        )
    rows = re.findall(r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", section, re.MULTILINE)
    parsed = [
        (
            tuple(s.strip() for s in sources.split(",")),
            target.strip(),
            actor.strip(),
        )
        for sources, target, actor in rows
        if sources.strip() != "From"
    ]
    for sources, target, actor in _TRANSITIONS:
        assert any(
            row == ((sources,), target, actor) for row in parsed
        ), (
            f"{_ADR}: transition {sources} -> {target} by {actor} is missing "
            "from the transition table"
        )
    assert any(
        "invalid" in target and "owner" in actor.lower() for _, target, actor in parsed
    ), f"{_ADR}: owner-only invalidation transition is missing"
    assert any(
        "superseded" in target and "owner" in actor.lower()
        for _, target, actor in parsed
    ), f"{_ADR}: owner supersession transition is missing"


def test_adr0007_readiness_ratification_separation() -> None:
    section = _section(_text(), "### 3. Status model")
    assert "READY_FOR_RATIFICATION" in section, (
        f"{_ADR}: pre-ratification readiness assessment is missing"
    )
    assert "ELIGIBLE_FOR_PUBLICATION" in section, (
        f"{_ADR}: post-ratification readiness receipt is missing"
    )
    assert re.search(
        r"and\s+only\s+the\s+owner\s+performs\s+it", section
    ), f"{_ADR}: ratification must be owner-only"
    assert "publication receipt" in section, (
        f"{_ADR}: publication receipt is missing"
    )
    assert "without owner ratification" in section, (
        f"{_ADR}: the no-publication-without-ratification rule is missing"
    )


def test_adr0007_authority_matrix() -> None:
    section = _section(_text(), "### 4. Authority matrix")
    rows = re.findall(r"^\| ([^|]+) \| ([^|]+) \| ([^|]+) \|$", section, re.MULTILINE)
    actors = [actor.strip() for actor, _, _ in rows]
    for actor in _AUTHORITY_ACTORS:
        assert actor in actors, (
            f"{_ADR}: authority matrix row for {actor!r} is missing"
        )
    may_not = {actor.strip(): ban.strip() for actor, _, ban in rows}
    model_ban = may_not["Model"]
    for ban in ("Admit claims", "waive gates", "become canonical state"):
        assert ban in model_ban, (
            f"{_ADR}: Model row must forbid {ban!r}"
        )
    assert "reparsed into canonical state" in may_not["Markdown projection"], (
        f"{_ADR}: Markdown projection must not be reparsed into canonical state"
    )


def test_adr0007_unknown_semantics() -> None:
    section = _section(_text(), "### 5. Unknown semantics")
    assert "open-world" in section, f"{_ADR}: open-world rule is missing"
    assert "three-valued" in section, f"{_ADR}: three-valued rule is missing"
    assert "unknown" in section and "not false" in section, (
        f"{_ADR}: 'unknown is not false' rule is missing"
    )
    assert re.search(r"[Mm]issing .* unknown|missing.*unknown", section), (
        f"{_ADR}: 'missing is unknown' rule is missing"
    )


def test_adr0007_waiver_policy() -> None:
    section = _section(_text(), "### 6. Waiver policy")
    assert "Only the owner grants a waiver" in section, (
        f"{_ADR}: owner-only waiver rule is missing"
    )
    assert "explicit" in section and "reason" in section, (
        f"{_ADR}: waivers must carry an explicit reason"
    )
    assert "revision" in section, (
        f"{_ADR}: waivers must pin the record revision"
    )
    assert "Critical gaps cannot be waived" in section, (
        f"{_ADR}: the unwaivable-critical-gaps rule is missing"
    )


def test_adr0007_client_contract_conformance() -> None:
    section = _section(_text(), "### 7. Client contract conformance")
    assert "mission.intake" in section, (
        f"{_ADR}: the mission.intake capability name is missing"
    )
    assert "No new CLI commands" in section, (
        f"{_ADR}: the no-new-commands rule is missing"
    )
