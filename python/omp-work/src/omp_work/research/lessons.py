"""Durable research lessons (R12, OMP-316).

A lesson draft either stages as a skill directory plus an FK-6 lesson dict, or
is rejected. A narrative lesson never authorizes an action. Retrieval serves
staged and narrowed lessons of one project plus every mandatory string, and
invocation and outcome counting never change a lesson's identity.

This module writes files and dicts only. It imports no knowledge authority, no
threading, and no database driver, and it starts no thread or process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from omp_work.v1.canonical import canonical_json

__all__ = [
    "KINDS",
    "LessonDraft",
    "invoke",
    "outcome",
    "retrieve",
    "revise",
    "stage",
]

KINDS = frozenset({"execution", "search", "implementation", "scientific", "narrative"})
STAGED = "staged"
NARROWED = "narrowed"
WITHDRAWN = "withdrawn"
REJECTED = "rejected"
_PRECONDITION_KEYS = frozenset({"project_id", "repository"})
_PRECONDITION_OPS = frozenset({"eq", "ne"})
_BINDING_KEYS = frozenset({"source_span", "revision", "work_item", "candidate", "evaluator"})
_BEHAVIOUR_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
}
_SPAN = re.compile(r"^(\d+):(\d+)$")


@dataclass
class LessonDraft:
    """One lesson candidate. ``kind`` is one of :data:`KINDS`."""

    lesson_id: str
    project_id: str
    kind: str
    title: str
    steps: list[str] = field(default_factory=list)
    claim: str = ""
    receipt_ids: list[str] = field(default_factory=list)
    preconditions: list[Mapping[str, Any]] = field(default_factory=list)
    binding: dict[str, str] = field(default_factory=dict)
    outcome: str = ""
    behaviour: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown_lesson_kind:{self.kind}")
        _safe_segment(self.lesson_id)


def _safe_segment(value: object) -> str:
    text = str(value)
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        raise ValueError(f"unsafe_lesson_id:{text!r}")
    return text


def _precondition(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"unknown_precondition_key:{raw!r}")
    key = raw.get("key")
    op = raw.get("op")
    if not isinstance(key, str) or key not in _PRECONDITION_KEYS:
        raise ValueError(f"unknown_precondition_key:{key!r}")
    if not isinstance(op, str) or op not in _PRECONDITION_OPS:
        raise ValueError(f"unknown_precondition_op:{op!r}")
    return {"key": key, "op": op, "value": raw.get("value")}


def _fk6_lesson(draft: LessonDraft) -> dict[str, Any]:
    return {
        "title": draft.title,
        "steps": list(draft.steps),
        "preconditions": [_precondition(item) for item in draft.preconditions],
        "claims": [{"text": draft.claim, "receipt_ids": list(draft.receipt_ids)}],
        "binding": dict(draft.binding),
        "outcome": draft.outcome,
        "behaviour": dict(draft.behaviour),
    }


def _record(draft: LessonDraft, *, status: str, revision: int) -> dict[str, Any]:
    return {
        "lesson_id": draft.lesson_id,
        "project_id": draft.project_id,
        "kind": draft.kind,
        "status": status,
        "revision": revision,
        "invocations": 0,
        "outcomes": {},
        "fk6_lesson": None if status == REJECTED else _fk6_lesson(draft),
    }


def _skill_markdown(draft: LessonDraft) -> str:
    lines = [f"# {draft.title}", ""]
    if draft.claim:
        lines += ["## Claim", "", draft.claim, ""]
    if draft.steps:
        lines += ["## Steps", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(draft.steps, 1)]
        lines += [""]
    return "\n".join(lines)


def _recipe_markdown(draft: LessonDraft) -> str:
    lines = [f"# Recipe: {draft.title}", ""]
    steps = list(draft.steps) or ([draft.claim] if draft.claim else [draft.title])
    lines += [f"{index}. {step}" for index, step in enumerate(steps, 1)]
    lines += [""]
    return "\n".join(lines)


def _regression_source(draft: LessonDraft, receipts: Mapping[str, Any]) -> str:
    """Generated test: preconditions, claimed behaviour, and citation admission.

    It re-stages the draft against its embedded receipts and asserts admission,
    then asserts a false claimed behaviour, a false project precondition, a
    false repository precondition, an unresolved citation, an unrelated PASS,
    and a receipt that proves a different claim are each refused and write no
    directory.
    """
    fields = {
        "lesson_id": draft.lesson_id,
        "project_id": draft.project_id,
        "kind": draft.kind,
        "title": draft.title,
        "steps": list(draft.steps),
        "claim": draft.claim,
        "receipt_ids": list(draft.receipt_ids),
        "preconditions": [dict(item) for item in draft.preconditions],
        "binding": dict(draft.binding),
        "outcome": draft.outcome,
        "behaviour": dict(draft.behaviour),
    }
    return "\n".join(
        [
            '"""Regression check: preconditions, claimed behaviour, and citation admission."""',
            "",
            "import copy",
            "",
            "from omp_work.research.lessons import LessonDraft, stage",
            "",
            f"FIELDS = {fields!r}",
            f"RECEIPTS = {dict(receipts)!r}",
            "",
            "",
            "def _draft(**overrides):",
            "    return LessonDraft(**{**copy.deepcopy(FIELDS), **overrides})",
            "",
            "",
            "def test_preconditions_and_claimed_behaviour_hold(tmp_path):",
            '    record = stage(_draft(), tmp_path, resolve=RECEIPTS.get)',
            '    assert record["status"] == "staged"',
            "",
            "",
            "def test_false_claimed_behaviour_is_refused(tmp_path):",
            '    behaviour = FIELDS["behaviour"]',
            '    value = behaviour["value"]',
            '    wrong = {"eq": value + 1, "le": value + 1, "ge": value - 1}.get(',
            '        behaviour["op"], value',
            "    )",
            "    receipts = copy.deepcopy(RECEIPTS)",
            "    for receipt in receipts.values():",
            '        receipt["metrics"][behaviour["metric"]] = wrong',
            "    record = stage(_draft(), tmp_path / 'out', resolve=receipts.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
            "",
            "def test_false_precondition_is_refused(tmp_path):",
            '    false = {"key": "project_id", "op": "ne", "value": FIELDS["project_id"]}',
            '    draft = _draft(preconditions=[*FIELDS["preconditions"], false])',
            "    record = stage(draft, tmp_path / 'out', resolve=RECEIPTS.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
            "",
            "def test_false_repository_precondition_is_refused(tmp_path):",
            "    receipts = copy.deepcopy(RECEIPTS)",
            "    for receipt in receipts.values():",
            '        receipt["repository"] = "actual"',
            '    false = {"key": "repository", "op": "eq", "value": "other"}',
            '    draft = _draft(preconditions=[*FIELDS["preconditions"], false])',
            "    record = stage(draft, tmp_path / 'out', resolve=receipts.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
            "",
            "def test_unresolved_citation_is_refused(tmp_path):",
            '    missing = "missing"',
            "    while missing in RECEIPTS:",
            '        missing += "-x"',
            '    draft = _draft(receipt_ids=[*FIELDS["receipt_ids"], missing])',
            "    record = stage(draft, tmp_path / 'out', resolve=RECEIPTS.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
            "",
            "def test_unrelated_pass_is_refused(tmp_path):",
            "    receipts = copy.deepcopy(RECEIPTS)",
            "    for receipt in receipts.values():",
            '        receipt["outcome"] = "PASS"',
            '        binding = dict(receipt["binding"])',
            '        binding["evaluator"] = str(binding["evaluator"]) + "-unrelated"',
            '        receipt["binding"] = binding',
            "    record = stage(_draft(), tmp_path / 'out', resolve=receipts.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
            "",
            "def test_different_claim_is_refused(tmp_path):",
            "    receipts = copy.deepcopy(RECEIPTS)",
            "    for receipt in receipts.values():",
            '        receipt["claim"] = str(receipt["claim"]) + " (different claim)"',
            "    record = stage(_draft(), tmp_path / 'out', resolve=receipts.get)",
            '    assert record["status"] == "rejected"',
            '    assert record["revision"] == 0',
            '    assert record["fk6_lesson"] is None',
            "    assert not (tmp_path / 'out').exists()",
            "",
        ]
    )


def _write_lesson(
    root: Path,
    draft: LessonDraft,
    record: dict[str, Any],
    receipts: Mapping[str, Any],
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "skill.md").write_text(_skill_markdown(draft), encoding="utf-8")
    (root / "recipe.md").write_text(_recipe_markdown(draft), encoding="utf-8")
    (root / "regression_test.py").write_text(_regression_source(draft, receipts), encoding="utf-8")
    manifest = {
        "lesson_id": draft.lesson_id,
        "project_id": draft.project_id,
        "kind": draft.kind,
        "title": draft.title,
        "status": record["status"],
        "revision": record["revision"],
        "files": ["skill.md", "recipe.md", "regression_test.py"],
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _load_receipt(resolve: Callable[[str], Any], receipt_id: str) -> Mapping[str, Any] | None:
    try:
        raw = resolve(receipt_id)
    except LookupError:
        return None
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, Mapping) else None


def _binding_ok(draft: LessonDraft, receipt: Mapping[str, Any]) -> bool:
    binding = draft.binding
    if not isinstance(binding, dict) or set(binding) != _BINDING_KEYS:
        return False
    if not all(_text(value) for value in binding.values()):
        return False
    if receipt.get("binding") != binding:
        return False
    if not _text(draft.outcome) or receipt.get("outcome") != draft.outcome:
        return False
    if not _text(draft.claim) or receipt.get("claim") != draft.claim:
        return False
    match = _SPAN.match(binding["source_span"])
    if match is None:
        return False
    start, end = int(match.group(1)), int(match.group(2))
    return canonical_json(receipt["claim"])[start:end] == draft.claim


def _behaviour_ok(draft: LessonDraft, receipt: Mapping[str, Any]) -> bool:
    behaviour = draft.behaviour
    if not isinstance(behaviour, dict):
        return False
    metric = behaviour.get("metric")
    op = behaviour.get("op")
    value = behaviour.get("value")
    if not _text(metric) or not isinstance(op, str) or op not in _BEHAVIOUR_OPS or not _number(value):
        return False
    metrics = receipt.get("metrics")
    if not isinstance(metrics, Mapping) or metric not in metrics:
        return False
    observed = metrics[metric]
    return _number(observed) and _BEHAVIOUR_OPS[op](observed, value)


def _precondition_observed(
    draft: LessonDraft, receipt: Mapping[str, Any], key: str
) -> tuple[bool, Any]:
    """Return whether ``key`` is present and the value admission compares.

    ``project_id`` is the draft's project. ``repository`` is that field on the
    receipt; a receipt that omits it does not satisfy the precondition.
    """
    if key == "project_id":
        return True, draft.project_id
    if key not in receipt:
        return False, None
    return True, receipt[key]


def _preconditions_hold(
    draft: LessonDraft,
    preconditions: Sequence[Mapping[str, Any]],
    receipts: Mapping[str, Mapping[str, Any]],
) -> bool:
    for item in preconditions:
        for receipt in receipts.values():
            present, actual = _precondition_observed(draft, receipt, item["key"])
            if not present:
                return False
            equal = actual == item["value"]
            if equal != (item["op"] == "eq"):
                return False
    return True


def _admitted(draft: LessonDraft, receipts: Mapping[str, Mapping[str, Any]]) -> bool:
    """Binding, behaviour, and preconditions. A bad precondition key or op raises."""
    if not all(
        _binding_ok(draft, receipt) and _behaviour_ok(draft, receipt)
        for receipt in receipts.values()
    ):
        return False
    return _preconditions_hold(draft, [_precondition(item) for item in draft.preconditions], receipts)


def stage(
    draft: LessonDraft,
    directory: Path | str,
    *,
    authorize: bool = False,
    resolve: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Stage a lesson, or reject it.

    ``authorize=True`` never stages: a narrative lesson cannot carry action
    authority, so the call raises :class:`ValueError` and writes nothing. A
    narrative lesson, an empty receipt list, a missing ``resolve``, or any cited
    receipt that does not resolve to a mapping is rejected without writing.
    Admission requires every cited receipt to carry the draft's binding,
    outcome and claim (with the claim at the bound source span) and to satisfy
    the claimed behaviour. ``project_id`` preconditions compare the draft;
    ``repository`` preconditions compare each receipt and fail when the key is
    absent. A bad precondition key or operator raises :class:`ValueError` and
    writes nothing. Any other miss, including a malformed binding or behaviour,
    rejects with revision 0, no FK-6 lesson, and no directory. Admitted lessons
    stage at revision 1 with zero invocations and write their skill, recipe,
    regression test, and manifest.
    """
    if authorize:
        raise ValueError("narrative_cannot_authorize")
    if draft.kind == "narrative" or not draft.receipt_ids or resolve is None:
        return _record(draft, status=REJECTED, revision=0)
    receipts: dict[str, Mapping[str, Any]] = {}
    for receipt_id in draft.receipt_ids:
        receipt = _load_receipt(resolve, receipt_id)
        if receipt is None:
            return _record(draft, status=REJECTED, revision=0)
        receipts[receipt_id] = receipt
    if not _admitted(draft, receipts):
        return _record(draft, status=REJECTED, revision=0)
    record = _record(draft, status=STAGED, revision=1)
    _write_lesson(Path(directory) / draft.lesson_id, draft, record, receipts)
    return record


def revise(
    record: dict[str, Any],
    *,
    action: str,
    contradicting_receipt: Any,
    precondition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Narrow or withdraw a staged lesson against a contradicting receipt.

    ``narrow`` appends the precondition, keeps the prior revision as
    ``supersedes``, records the contradicting receipt, and moves to the next
    revision with status ``narrowed``. ``withdraw`` records the contradicting
    receipt and status ``withdrawn``.
    """
    if action == "narrow":
        if precondition is None:
            raise ValueError("narrow_requires_precondition")
        fk6 = record.get("fk6_lesson")
        if not isinstance(fk6, dict):
            raise ValueError("lesson_has_no_fk6_lesson")
        preconditions = fk6.setdefault("preconditions", [])
        preconditions.append(_precondition(precondition))
        record["supersedes"] = record.get("revision")
        record["revision"] = int(record.get("revision", 1)) + 1
        record["status"] = NARROWED
        record["contradicting_receipt"] = contradicting_receipt
        return record
    if action == "withdraw":
        record["status"] = WITHDRAWN
        record["contradicting_receipt"] = contradicting_receipt
        return record
    raise ValueError(f"unknown_revise_action:{action}")


def retrieve(
    records: Sequence[dict[str, Any]],
    *,
    project_id: str,
    limit: int,
    mandatory: Sequence[str],
) -> list[Any]:
    """Serve the project's staged and narrowed lessons, then every mandatory string.

    At most ``limit`` lessons return; rejected and withdrawn lessons and lessons
    of another project never do. The mandatory strings always return in order,
    even for a zero limit, and sit outside the lesson cap.
    """
    found = [
        record
        for record in records
        if record.get("project_id") == project_id
        and record.get("status") in {STAGED, NARROWED}
    ][: max(int(limit), 0)]
    return [*found, *mandatory]


def invoke(record: dict[str, Any]) -> dict[str, Any]:
    """Count one invocation. Nothing else changes."""
    record["invocations"] = int(record.get("invocations", 0)) + 1
    return record


def outcome(record: dict[str, Any], name: str) -> dict[str, Any]:
    """Count one named outcome. Nothing else changes."""
    outcomes = record.setdefault("outcomes", {})
    outcomes[name] = int(outcomes.get(name, 0)) + 1
    return record
