"""OMP-615-s08-s01-s02: the model-policy assertion inventory stays complete and classified.

Failure mode defended: ``docs/model-policy-governance.md`` is the base for
reconciling model/role assertions. If a priority.json role chain (slow, judge,
...) loses its active-selection row, a doctrine assertion shares a scope with
another, the resolver is labelled syntax-only, a catalog or docs/models.md row
is claimed as executable authority, or an unresolved row has no owner/reason,
the reconciliation silently skips real routing behavior. The qualifier below
reports each such omission or misclassification; the tests mutate parsed rows
to prove it does.
"""

from __future__ import annotations

import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_DOC = _REPO / "docs" / "model-policy-governance.md"
_PRIORITY = _REPO / "packages" / "coding-agent" / "src" / "priority.json"

_COLUMNS = [
    "ID",
    "Source/selector",
    "Assertion",
    "Authority",
    "Caller",
    "Scope",
    "Disposition",
    "Disagreement",
    "Owner",
    "Evidence",
]
_AUTHORITIES = {
    "executable",
    "doctrine",
    "agent-definition",
    "configuration",
    "catalog",
    "syntax",
}
_DISPOSITIONS = {"verified", "unresolved", "extension"}
_DOCTRINE_SOURCE = "session-system/agents/omp-AGENTS.md"
_DOCTRINE_TOPICS = {
    "default-worker",
    "slow-triggers",
    "deep-adjudication",
    "intake-deep-exclusion",
    "audit-role-ownership",
    "owner-assignments",
    "no-automatic-replacement",
}
_RESOLVER = "packages/coding-agent/src/config/model-resolver.ts"
_NON_EXECUTABLE_PREFIXES = ("docs/models.md", "packages/catalog/")

Row = dict[str, str]


def parse_rows(text: str) -> list[Row]:
    rows: list[Row] = []
    header_seen = False
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells == _COLUMNS:
            header_seen = True
            continue
        if not header_seen or set("".join(cells)) <= set("-: "):
            continue
        if len(cells) != len(_COLUMNS):
            rows.append({"ID": cells[0], "_malformed": "1"})
            continue
        rows.append(dict(zip(_COLUMNS, cells)))
    return rows


def _path(ref: str) -> str:
    return ref.split("#", 1)[0].strip()


def qualify(rows: list[Row], priority: dict[str, list[str]], repo: Path) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for row in rows:
        rid = row.get("ID", "?")
        if "_malformed" in row:
            problems.append(f"{rid}: malformed row (wrong column count)")
            continue
        if rid in seen:
            problems.append(f"{rid}: duplicate ID")
        seen.add(rid)
        for col in ("Source/selector", "Authority", "Caller", "Evidence", "Scope"):
            if not row[col]:
                problems.append(f"{rid}: missing {col}")
        if row["Authority"] and row["Authority"] not in _AUTHORITIES:
            problems.append(f"{rid}: unknown authority {row['Authority']!r}")
        if row["Disposition"] not in _DISPOSITIONS:
            problems.append(f"{rid}: unknown disposition {row['Disposition']!r}")
        for col in ("Source/selector", "Evidence"):
            if row[col] and not (repo / _path(row[col])).exists():
                problems.append(f"{rid}: {col} path does not exist: {_path(row[col])}")
        if row["Disposition"] == "unresolved":
            if "Chris" not in row["Owner"]:
                problems.append(f"{rid}: unresolved row lacks owner Chris")
            if not row["Disagreement"]:
                problems.append(f"{rid}: unresolved row lacks reason")
        src = _path(row["Source/selector"])
        if src.startswith(_NON_EXECUTABLE_PREFIXES) and row["Authority"] in {
            "executable",
            "doctrine",
            "agent-definition",
        }:
            problems.append(f"{rid}: {src} is configuration/catalog authority, not {row['Authority']}")

    active = [r for r in rows if "_malformed" not in r and r["Scope"] == "active-selection"]
    for key, chain in priority.items():
        selector = f"packages/coding-agent/src/priority.json#{key}"
        covering = [r for r in active if r["Source/selector"] == selector]
        if not covering:
            problems.append(f"priority.json chain {key!r}: no active-selection row")
        elif chain and not any(f"`{chain[0]}`" in r["Assertion"] for r in covering):
            problems.append(f"priority.json chain {key!r}: no row asserts head `{chain[0]}`")

    doctrine = [r for r in rows if "_malformed" not in r and _path(r["Source/selector"]) == _DOCTRINE_SOURCE]
    scopes: dict[str, str] = {}
    for row in doctrine:
        scope = row["Scope"]
        if row["Authority"] == "doctrine" and scope in scopes:
            problems.append(f"{row['ID']}: doctrine scope {scope!r} shared with {scopes[scope]}")
        scopes[scope] = row["ID"]
    covered = {s.removeprefix("doctrine/") for s in scopes if s.startswith("doctrine/")}
    for topic in sorted(_DOCTRINE_TOPICS - covered):
        problems.append(f"doctrine topic {topic!r}: no row")

    resolver = [r for r in rows if "_malformed" not in r and r["Source/selector"].startswith(_RESOLVER)]
    if not any(r["Authority"] == "executable" for r in resolver):
        problems.append("model-resolver.ts: no executable-selection row")
    if not any(r["Authority"] == "syntax" for r in resolver):
        problems.append("model-resolver.ts: no syntax row")

    parsed = [r for r in rows if "_malformed" not in r]
    if not any(r["Scope"] == "preflight" and "auditor-runner.ts" in r["Source/selector"] for r in parsed):
        problems.append("auditor-runner.ts: no preflight row")
    scope_values = {r["Scope"] for r in rows if "_malformed" not in r}
    for needed in ("historical", "experimental", "catalog"):
        if needed not in scope_values:
            problems.append(f"scope {needed!r}: no row")
    return problems


def _load() -> tuple[list[Row], dict[str, list[str]]]:
    return parse_rows(_DOC.read_text()), json.loads(_PRIORITY.read_text())


def test_inventory_is_complete_and_classified() -> None:
    rows, priority = _load()
    assert rows
    assert qualify(rows, priority, _REPO) == []


def test_every_priority_chain_key_is_parsed() -> None:
    _, priority = _load()
    assert {"slow", "judge"} <= set(priority)


def test_deleted_slow_and_judge_rows_are_reported() -> None:
    rows, priority = _load()
    kept = [r for r in rows if not r["Source/selector"].endswith(("priority.json#slow", "priority.json#judge"))]
    problems = qualify(kept, priority, _REPO)
    assert any("'slow'" in p for p in problems)
    assert any("'judge'" in p for p in problems)


def test_changed_chain_head_is_reported() -> None:
    rows, priority = _load()
    changed = {**priority, "slow": ["openai-codex/gpt-9-new", *priority["slow"]]}
    assert any("head `openai-codex/gpt-9-new`" in p for p in qualify(rows, changed, _REPO))


def test_deleted_doctrine_coverage_is_reported() -> None:
    rows, priority = _load()
    kept = [r for r in rows if r["Scope"] != "doctrine/slow-triggers"]
    assert any("'slow-triggers'" in p for p in qualify(kept, priority, _REPO))


def test_shared_doctrine_scope_is_reported() -> None:
    rows, priority = _load()
    mutated = [dict(r) for r in rows]
    for r in mutated:
        if r["Scope"] == "doctrine/deep-adjudication":
            r["Scope"] = "doctrine/slow-triggers"
    assert any("shared with" in p for p in qualify(mutated, priority, _REPO))


def test_duplicate_id_and_missing_fields_are_reported() -> None:
    rows, priority = _load()
    mutated = [dict(r) for r in rows]
    mutated.append(dict(mutated[0]))
    mutated[1]["Evidence"] = ""
    mutated[2]["Caller"] = ""
    mutated[3]["Authority"] = ""
    mutated[4]["Source/selector"] = ""
    problems = qualify(mutated, priority, _REPO)
    for expected in ("duplicate ID", "missing Evidence", "missing Caller", "missing Authority", "missing Source/selector"):
        assert any(expected in p for p in problems), expected


def test_unresolved_row_needs_chris_and_reason() -> None:
    rows, priority = _load()
    mutated = [dict(r) for r in rows]
    target = next(r for r in mutated if r["Disposition"] == "unresolved")
    target["Owner"] = "-"
    target["Disagreement"] = ""
    problems = qualify(mutated, priority, _REPO)
    assert any("lacks owner Chris" in p for p in problems)
    assert any("lacks reason" in p for p in problems)


def test_resolver_cannot_be_syntax_only() -> None:
    rows, priority = _load()
    mutated = [dict(r) for r in rows]
    for r in mutated:
        if r["Source/selector"].startswith(_RESOLVER) and r["Authority"] == "executable":
            r["Authority"] = "syntax"
    assert any("no executable-selection row" in p for p in qualify(mutated, priority, _REPO))


def test_catalog_row_claiming_executable_authority_is_reported() -> None:
    rows, priority = _load()
    mutated = [dict(r) for r in rows]
    next(r for r in mutated if r["Source/selector"].startswith("docs/models.md"))["Authority"] = "executable"
    next(r for r in mutated if r["Source/selector"].startswith("packages/catalog/"))["Authority"] = "doctrine"
    problems = qualify(mutated, priority, _REPO)
    assert sum("configuration/catalog authority" in p for p in problems) == 2


def test_missing_auditor_preflight_is_reported() -> None:
    rows, priority = _load()
    kept = [r for r in rows if r["Scope"] != "preflight"]
    assert any("auditor-runner.ts: no preflight row" in p for p in qualify(kept, priority, _REPO))
