"""Scripted answers for interactive RPC extension UI requests.

``UiScript.load`` reads a JSON list of rules. The first rule whose ``method``
and ``title`` equal the request is the answer. A rule never matches a
different title, and nothing in this module approves a request on its own.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_INTERACTIVE = frozenset({"select", "confirm", "input", "editor"})
_VALUE_METHODS = frozenset({"select", "input", "editor"})


class UiScript:
    """Ordered UI rules. ``match`` returns the first exact method and title hit."""

    def __init__(self, rules: tuple[dict[str, Any], ...]) -> None:
        self.rules = rules

    @classmethod
    def load(cls, path: str | Path) -> UiScript:
        """Load ``[{method, title, answer}]`` from ``path``.

        ``answer`` is ``{confirmed: bool}`` (confirm only), ``{value: str}``
        (select, input, or editor), or ``{cancel: true}``.
        """

        file = Path(path)
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read ui script {file}: {exc}") from exc
        if not isinstance(document, list):
            raise ValueError("ui script must be a JSON list of rules")
        return cls.from_rules(document)

    @classmethod
    def from_rules(cls, rules: Any) -> UiScript:
        if not isinstance(rules, (list, tuple)):
            raise ValueError("ui script rules must be a list")
        return cls(tuple(_parse_rule(rule, index) for index, rule in enumerate(rules)))

    def match(self, method: str, title: str | None) -> dict[str, Any] | None:
        for rule in self.rules:
            if rule["method"] == method and rule["title"] == title:
                return rule
        return None


def _parse_rule(raw: object, index: int) -> dict[str, Any]:
    label = f"ui script rules[{index}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(raw) - {"method", "title", "answer"})
    missing = sorted({"method", "title", "answer"} - set(raw))
    if unknown or missing:
        raise ValueError(f"{label} keys must be method, title, and answer")
    method = raw["method"]
    title = raw["title"]
    if not isinstance(method, str) or method not in _INTERACTIVE:
        raise ValueError(f"{label}.method must be select, confirm, input, or editor")
    if not isinstance(title, str):
        raise ValueError(f"{label}.title must be a string")
    return {"method": method, "title": title, "answer": _parse_answer(raw["answer"], method, f"{label}.answer")}


def _parse_answer(raw: object, method: str, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    keys = set(raw)
    if keys == {"cancel"}:
        if raw["cancel"] is not True:
            raise ValueError(f"{label}.cancel must be true")
        return {"cancel": True}
    if keys == {"confirmed"}:
        if method != "confirm":
            raise ValueError(f"{label} confirmed is only valid for confirm")
        if not isinstance(raw["confirmed"], bool):
            raise ValueError(f"{label}.confirmed must be a boolean")
        return {"confirmed": raw["confirmed"]}
    if keys == {"value"}:
        if method not in _VALUE_METHODS:
            raise ValueError(f"{label} value is only valid for select, input, or editor")
        if not isinstance(raw["value"], str):
            raise ValueError(f"{label}.value must be a string")
        return {"value": raw["value"]}
    raise ValueError(f"{label} must be {{confirmed}}, {{value}}, or {{cancel: true}}")
