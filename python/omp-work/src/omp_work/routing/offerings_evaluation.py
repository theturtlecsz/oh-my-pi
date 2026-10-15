"""OMP-615-s09-s04: immutable evaluation records for the read-only daily offerings check.

``run_offerings_check`` joins the frozen parsers, the byte fetchers and the
comparison into one daily act: read the governing policy and the owner's
captures, compare, and append one immutable evaluation record. The record carries
the snapshot the comparison retained (last successful listing per lane, never a
current gap) and today's findings, each labelled ``urgent_review``,
``check_gap`` or ``pending_qualification``. Nothing outside ``record_dir`` is
ever written: routing, the policy file and every capture are read-only.

A record is ``evaluation-<creation_ns>-<uuid4>.json``, created exclusively
(``O_EXCL``) so two runs can never overwrite each other; when an in-use model is
retiring, deprecated or failing, or a lane could not be checked, the same stem
also carries ``owner-report-<creation_ns>-<uuid4>.json`` for the immediate
review.

The previous record is the whole prior world: the newest ``evaluation-*.json``,
ordered by its creation nanoseconds and then by filename. It is validated before
any provider read or record write — version, timestamps, digests,
provider-qualified snapshot attributes and provenance, and findings schema. A
readable record that fails validation is a hard input error; the check never
falls back to an older one, because a fabricated baseline would silently erase
retirements.

Exit contract: ``0`` when the record carries nothing for the owner to act on,
``3`` when an owner report was written (urgent review or check gap), ``2`` for
input errors — an unusable sources file, a malformed readable history, an
unusable age, a failed write. Input errors print one diagnostic to stderr and
leave stdout empty.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, NoReturn
from uuid import UUID, uuid4

from ..operations.config import OperationsConfig
from .model_review import RUBRIC_PATH, SCHEDULE_KEYS
from .offerings_check import Finding, compare
from .offerings_inputs import OfferingsInputError, parse_sources
from .offerings_sources import DEFAULT_OMP, fetch_all, load_notices
from .policy import RoutingPolicy, load_policy

EVALUATION_VERSION = "offerings-evaluation.v1"
OWNER_REPORT_VERSION = "offerings-owner-report.v1"

STATUS_URGENT_REVIEW = "urgent_review"
STATUS_CHECK_GAP = "check_gap"
STATUS_PENDING_QUALIFICATION = "pending_qualification"

STATUSES = (STATUS_URGENT_REVIEW, STATUS_CHECK_GAP, STATUS_PENDING_QUALIFICATION)
ACTIONABLE = frozenset({STATUS_URGENT_REVIEW, STATUS_CHECK_GAP})

DEFAULT_MAX_AGE_HOURS = 36.0

EVALUATION_PREFIX = "evaluation-"
OWNER_REPORT_PREFIX = "owner-report-"
POLICY_FILENAME = "routing-policy.v1.json"

# The schema boundary for a readable prior record. These kind/attribute names
# mirror the comparison's output shape; they are validated here, not derived
# from it, so a comparison change cannot silently widen what history accepts.
FINDING_KINDS = frozenset({"new", "changed", "retired", "deprecated", "failing", "unchecked"})
SNAPSHOT_ATTRIBUTES = ("name", "context_window", "max_tokens", "reasoning", "thinking", "input", "cost")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EVALUATION_NAME = re.compile(r"^evaluation-(\d+)-([0-9a-fA-F-]{36})\.json$")


class OfferingsEvaluationError(ValueError):
    """The check cannot produce a trustworthy record; the message names the cause."""


@dataclass(frozen=True)
class CheckOutcome:
    """What the CLI prints and the code it exits with."""

    report: dict[str, Any]
    exit_code: int


class EvaluationRecord:
    """A previous evaluation: its name's creation nanoseconds and its document."""

    __slots__ = ("creation_ns", "document")

    def __init__(self, creation_ns: int | None, document: dict[str, Any]) -> None:
        self.creation_ns = creation_ns
        self.document = document

    @property
    def snapshot(self) -> dict[str, Any]:
        return self.document["snapshot"]


def _fail(reason: str) -> NoReturn:
    raise OfferingsEvaluationError(reason)


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{field} must be a JSON object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> None:
    if value is not None:
        _text(value, field)


def _flag(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        _fail(f"{field} must be a boolean")


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{field} must be an integer")
    return value


def _optional_integer(value: Any, field: str) -> None:
    if value is not None:
        _integer(value, field)


def _hex64(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        _fail(f"{field} must be a lowercase sha256 hex digest")
    return value


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        _fail(f"{field} must be an ISO-8601 UTC timestamp string")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        _fail(f"{field} is not an ISO-8601 timestamp: {error}")
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        _fail(f"{field} must carry a UTC offset")
    return moment


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def default_record_dir() -> Path:
    """Where evaluation records live by default: the work-ledger state dir."""
    return OperationsConfig.defaults().state_dir / "offerings"


def resolve_omp() -> str:
    """The CLI that owns ``omp-cli`` lanes; a missing binary only gaps those lanes."""
    return shutil.which(DEFAULT_OMP) or DEFAULT_OMP


def policy_path() -> Path:
    """The authoritative routing policy this check binds its records to."""
    return Path(__file__).with_name(POLICY_FILENAME)


# --- previous-record validation ---------------------------------------------


def _validate_attributes(attributes: Any, field: str) -> None:
    record = _object(attributes, field)
    for key in SNAPSHOT_ATTRIBUTES:
        if key not in record:
            _fail(f"{field} is missing {key}")
    _text(record["name"], f"{field}.name")
    _optional_integer(record["context_window"], f"{field}.context_window")
    _optional_integer(record["max_tokens"], f"{field}.max_tokens")
    _flag(record["reasoning"], f"{field}.reasoning")
    thinking = record["thinking"]
    if thinking is not None and not (
        isinstance(thinking, list) and all(isinstance(item, str) for item in thinking)
    ):
        _fail(f"{field}.thinking must be a list of effort strings or null")
    if not isinstance(record["input"], list) or not all(isinstance(item, str) for item in record["input"]):
        _fail(f"{field}.input must be a list of modality strings")
    _object(record["cost"], f"{field}.cost")


def _validate_snapshot(snapshot: Any) -> None:
    for lane, entry in _object(snapshot, "snapshot").items():
        where = f"snapshot.{lane}"
        _text(lane, "snapshot lane name")
        record = _object(entry, where)
        _text(record.get("catalog_provider"), f"{where}.catalog_provider")
        for model_id, attributes in _object(record.get("models"), f"{where}.models").items():
            _text(model_id, f"{where}.models id")
            _validate_attributes(attributes, f"{where}.models.{model_id}")
        evidence = _object(record.get("evidence"), f"{where}.evidence")
        _text(evidence.get("source"), f"{where}.evidence.source")
        _utc(evidence.get("time"), f"{where}.evidence.time")
        raw = evidence.get("raw_sha256")
        if raw is not None:
            _hex64(raw, f"{where}.evidence.raw_sha256")
        _hex64(evidence.get("listing_sha256"), f"{where}.evidence.listing_sha256")


def _validate_findings(findings: Any) -> None:
    if not isinstance(findings, list):
        _fail("findings must be a list")
    for index, item in enumerate(findings):
        where = f"findings[{index}]"
        record = _object(item, where)
        if record.get("kind") not in FINDING_KINDS:
            _fail(f"{where}.kind must be one of {sorted(FINDING_KINDS)}")
        if record.get("status") not in STATUSES:
            _fail(f"{where}.status must be one of {sorted(STATUSES)}")
        for field in ("lane", "model", "effort"):
            _optional_text(record.get(field), f"{where}.{field}")
        recipes = record.get("recipes")
        if not isinstance(recipes, list) or not all(isinstance(recipe, str) for recipe in recipes):
            _fail(f"{where}.recipes must be a list of strings")
        _flag(record.get("in_use"), f"{where}.in_use")
        _flag(record.get("urgent"), f"{where}.urgent")
        _object(record.get("evidence"), f"{where}.evidence")


def validate_evaluation(record: Any, *, name_ns: int | None = None) -> dict[str, Any]:
    """Validate one readable prior record; raise for anything the check cannot trust.

    ``name_ns`` is the creation nanoseconds encoded in the file name. When the
    name encodes one, the record's own ``created_ns`` must equal it, so the
    ordering the check selects by is the ordering the record proves.
    """
    document = _object(record, "evaluation")
    if document.get("version") != EVALUATION_VERSION:
        _fail(f"version must be {EVALUATION_VERSION!r}")
    created = _integer(document.get("created_ns"), "created_ns")
    if created < 0:
        _fail("created_ns must be a non-negative integer")
    if name_ns is not None and created != name_ns:
        _fail(f"created_ns {created} does not match the record name {name_ns}")
    _utc(document.get("checked_at"), "checked_at")
    _hex64(document.get("policy_sha256"), "policy_sha256")
    _hex64(document.get("task_set_digest"), "task_set_digest")
    if document.get("rubric") != RUBRIC_PATH:
        _fail(f"rubric must be {RUBRIC_PATH!r}")
    _hex64(document.get("sources_digest"), "sources_digest")
    _validate_snapshot(document.get("snapshot"))
    _validate_findings(document.get("findings"))
    return document


def _name_creation_ns(name: str) -> int | None:
    match = _EVALUATION_NAME.fullmatch(name)
    if match is None:
        return None
    try:
        UUID(match.group(2))
    except ValueError:
        return None
    return int(match.group(1))


def discover_previous(record_dir: Path) -> EvaluationRecord | None:
    """Load the newest evaluation record, or None when history is empty.

    Candidates are the ``evaluation-*.json`` entries, ordered by the creation
    nanoseconds in their name then by filename; the greatest is the previous
    world. A missing directory is empty history. The chosen record is validated
    in full and a failure propagates: an older, well-formed record is never
    substituted for a malformed newest one.
    """
    if not record_dir.exists():
        return None
    if not record_dir.is_dir():
        _fail(f"record path is not a directory: {record_dir}")
    candidates: list[tuple[int, str, Path]] = []
    for child in record_dir.iterdir():
        if not child.is_file():
            continue
        if not (child.name.startswith(EVALUATION_PREFIX) and child.name.endswith(".json")):
            continue
        creation_ns = _name_creation_ns(child.name)
        candidates.append((-1 if creation_ns is None else creation_ns, child.name, child))
    if not candidates:
        return None
    creation_ns, name, path = max(candidates)
    try:
        document = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        _fail(f"previous evaluation {name!r} is unreadable: {error}")
    try:
        validate_evaluation(document, name_ns=None if creation_ns < 0 else creation_ns)
    except OfferingsEvaluationError as error:
        _fail(f"previous evaluation {name!r} is malformed: {error}")
    return EvaluationRecord(None if creation_ns < 0 else creation_ns, document)


# --- classification and writing ---------------------------------------------


def classify(finding: Finding) -> str:
    """One status per finding: the immediate paths first, else the weekly queue."""
    if finding.urgent:
        return STATUS_URGENT_REVIEW
    if finding.kind == "unchecked":
        return STATUS_CHECK_GAP
    return STATUS_PENDING_QUALIFICATION


def _write_exclusive(path: Path, document: dict[str, Any]) -> None:
    """Create ``path`` for the first time only; a name clash is an error, not a merge."""
    payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except BaseException:
        # os.open created this path; a failed write must not leave it half-written.
        path.unlink(missing_ok=True)
        raise


def _owner_report_document(
    *,
    evaluation: str,
    checked_at: datetime,
    policy_sha256: str,
    task_set_digest: str,
    schedule: Any,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    steps = schedule if isinstance(schedule, dict) else {}
    return {
        "version": OWNER_REPORT_VERSION,
        "checked_at": _iso(checked_at),
        "evaluation": evaluation,
        "policy_sha256": policy_sha256,
        "task_set_digest": task_set_digest,
        "rubric": RUBRIC_PATH,
        "procedure": {
            "rubric": RUBRIC_PATH,
            "task_set_digest": task_set_digest,
            "steps": {name: steps.get(name) for name in SCHEDULE_KEYS},
        },
        "findings": [finding for finding in findings if finding["status"] in ACTIONABLE],
    }


def run_offerings_check(
    sources_path: str | Path,
    *,
    record_dir: str | Path | None = None,
    omp: str | None = None,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    policy: RoutingPolicy | None = None,
    clock: Callable[[], datetime] | None = None,
) -> CheckOutcome:
    """Compare the captures with the policy and append one immutable evaluation.

    Every input — policy, sources file, previous record — is read and validated
    before a single provider byte is fetched or a single record is created, so a
    rejected input never leaves a partial record behind. ``clock`` labels the
    fetch attempts and fixes the comparison instant; ``creation_ns`` is always the
    wall clock, bumped above the previous record so successive runs are ordered.
    """
    if isinstance(max_age_hours, bool) or not isinstance(max_age_hours, (int, float)):
        _fail("max_age_hours must be a number")
    if not math.isfinite(max_age_hours) or max_age_hours <= 0:
        _fail(f"max_age_hours must be positive and finite, got {max_age_hours!r}")

    governing = load_policy() if policy is None else policy
    review = governing.raw.get("review")
    if not isinstance(review, dict):
        _fail("governing policy carries no review metadata")
    if review.get("rubric") != RUBRIC_PATH:
        _fail(f"governing policy rubric must be {RUBRIC_PATH!r}")
    task_set_digest = _hex64(review.get("task_set_digest"), "policy review.task_set_digest")

    source_file = Path(sources_path)
    try:
        raw = source_file.read_bytes()
    except OSError as error:
        _fail(f"sources unreadable: {error}")
    try:
        sources = parse_sources(raw)
    except OfferingsInputError as error:
        _fail(f"sources refused: {error}")
    sources_digest = hashlib.sha256(raw).hexdigest()

    if record_dir is None:
        try:
            records = default_record_dir()
        except (OSError, ValueError) as error:
            _fail(f"default record directory unavailable: {error}")
    else:
        records = Path(record_dir)
    previous = discover_previous(records)

    now = _default_clock if clock is None else clock
    checked_at = now()
    if (
        not isinstance(checked_at, datetime)
        or checked_at.tzinfo is None
        or checked_at.utcoffset() != timedelta(0)
    ):
        _fail("checked_at must be a UTC datetime")

    try:
        policy_sha256 = hashlib.sha256(policy_path().read_bytes()).hexdigest()
    except OSError as error:
        _fail(f"governing policy unreadable: {error}")

    captures = fetch_all(sources, clock=now, omp=resolve_omp() if omp is None else omp)
    notices = load_notices(sources, clock=now)
    comparison = compare(
        governing,
        sources,
        {name: capture.value for name, capture in captures.items()},
        notices.notices(),
        None if previous is None else previous.snapshot,
        checked_at,
        max_age_hours,
    )

    findings: list[dict[str, Any]] = []
    counts: dict[str, Any] = {"total": 0, **dict.fromkeys(STATUSES, 0)}
    for found in comparison.findings:
        item = found.as_dict()
        item["status"] = classify(found)
        findings.append(item)
        counts["total"] += 1
        counts[item["status"]] += 1

    # The creation instant is wall clock, bumped strictly above the previous
    # record so two runs at the same checked_at stay ordered by name.
    creation_ns = time.time_ns()
    if previous is not None and previous.creation_ns is not None and creation_ns <= previous.creation_ns:
        creation_ns = previous.creation_ns + 1
    stem = f"{creation_ns}-{uuid4()}"
    evaluation = f"{EVALUATION_PREFIX}{stem}.json"
    actionable = counts[STATUS_URGENT_REVIEW] + counts[STATUS_CHECK_GAP]
    owner_report = f"{OWNER_REPORT_PREFIX}{stem}.json" if actionable else None
    document = {
        "version": EVALUATION_VERSION,
        "created_ns": creation_ns,
        "checked_at": _iso(checked_at),
        "policy_sha256": policy_sha256,
        "task_set_digest": task_set_digest,
        "rubric": RUBRIC_PATH,
        "sources_digest": sources_digest,
        "snapshot": comparison.snapshot,
        "findings": findings,
    }
    report_document = None
    if owner_report is not None:
        report_document = _owner_report_document(
            evaluation=evaluation,
            checked_at=checked_at,
            policy_sha256=policy_sha256,
            task_set_digest=task_set_digest,
            schedule=review.get("schedule"),
            findings=findings,
        )

    created: list[Path] = []
    try:
        records.mkdir(parents=True, exist_ok=True)
        _write_exclusive(records / evaluation, document)
        created.append(records / evaluation)
        if report_document is not None and owner_report is not None:
            _write_exclusive(records / owner_report, report_document)
            created.append(records / owner_report)
    except OSError as error:
        # An input error must leave no partial record; only paths this run just
        # created are removed, never a pre-existing file that caused a name clash.
        for path in created:
            path.unlink(missing_ok=True)
        _fail(f"evaluation record could not be written: {error}")

    return CheckOutcome(
        report={"evaluation": evaluation, "owner_report": owner_report, "counts": counts},
        exit_code=3 if owner_report is not None else 0,
    )
