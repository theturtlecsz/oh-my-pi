"""Frozen inputs for the read-only daily model-offerings check (OMP-615-s09).

Three owner captures feed the check: a sources map (which lane is read through
``omp models --json`` and which through a listing file), a per-lane model
listing, and per-lane retirement/deprecation notices plus provider status. This
module only parses and validates those captures. It makes no provider call,
edits no routing file, and never substitutes a timestamp for absent evidence:
an unproven lane yields a :class:`SourceGap` instead of today's date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
from types import MappingProxyType
from typing import Any, Literal, Mapping

SOURCES_VERSION = "offerings-sources.v1"
NOTICES_VERSION = "offerings-notices.v1"

LANE_KINDS = frozenset({"omp-cli", "listing-file"})
COVERAGE_VALUES = frozenset({"checked", "not_published"})
NOTICE_TYPES = frozenset({"retirement", "deprecation", "failure"})
STATUS_VALUES = frozenset({"operational", "degraded", "outage"})
INPUT_MODALITIES = frozenset({"text", "image"})

CLI_PROOF_STATUS = "ok"
CLI_PROOF_SOURCE = "provider"


class OfferingsInputError(ValueError):
    """An owner capture has a shape the check cannot compare; ``field`` names it."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


def _name(where: str, key: str) -> str:
    return f"{where}.{key}" if where else key


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise OfferingsInputError(key, "duplicate JSON key")
        document[key] = value
    return document


def _reject_constant(token: str) -> None:
    raise OfferingsInputError(token, "non-finite number is not allowed")


def _decode(raw: bytes | str) -> Any:
    if isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OfferingsInputError("<root>", f"capture is not utf-8: {exc}") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise OfferingsInputError("<root>", "capture must be bytes or str")
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise OfferingsInputError("<root>", f"capture is not valid JSON: {exc}") from exc


def _digest(raw: bytes | str) -> str:
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OfferingsInputError(field, "must be an object")
    return value


def _text(record: Mapping[str, Any], key: str, where: str) -> str:
    if key not in record:
        raise OfferingsInputError(_name(where, key), "missing")
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise OfferingsInputError(_name(where, key), "must be a non-empty string")
    return value


def _optional_text(record: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in record or record[key] is None:
        return None
    value = record[key]
    if not isinstance(value, str) or not value.strip():
        raise OfferingsInputError(_name(where, key), "must be a non-empty string")
    return value


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise OfferingsInputError(field, "must be an ISO-8601 UTC timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfferingsInputError(field, f"invalid timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise OfferingsInputError(field, "must carry a UTC offset")
    return parsed


def _calendar_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise OfferingsInputError(field, "must be an ISO calendar date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise OfferingsInputError(field, f"invalid date: {exc}") from exc


# --- sources ----------------------------------------------------------------


@dataclass(frozen=True)
class SourceLane:
    """One provider lane: how its listing is read and which catalog provider it is."""

    lane: str
    kind: Literal["omp-cli", "listing-file"]
    catalog_provider: str
    path: str | None


@dataclass(frozen=True)
class Sources:
    version: str
    notices: str
    lanes: Mapping[str, SourceLane]


def parse_sources(raw: bytes | str) -> Sources:
    """Parse ``offerings-sources.v1``; refuse duplicates and kind/path mismatches.

    Two lanes mapping the same ``catalog_provider`` are refused: the configured
    ID is the comparison key, so it must be unique. ``path`` is file-only,
    required for ``listing-file`` and absent for ``omp-cli``.
    """
    data = _object(_decode(raw), "<root>")
    version = _text(data, "version", "")
    if version != SOURCES_VERSION:
        raise OfferingsInputError("version", f"expected {SOURCES_VERSION!r}, got {version!r}")
    notices = _text(data, "notices", "")
    if "lanes" not in data:
        raise OfferingsInputError("lanes", "missing")
    lanes_raw = _object(data["lanes"], "lanes")
    if not lanes_raw:
        raise OfferingsInputError("lanes", "must declare at least one lane")

    lanes: dict[str, SourceLane] = {}
    by_provider: dict[str, str] = {}
    for name, value in lanes_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise OfferingsInputError("lanes", "lane name must be a non-empty string")
        where = f"lanes.{name}"
        record = _object(value, where)
        kind = _text(record, "kind", where)
        if kind not in LANE_KINDS:
            raise OfferingsInputError(_name(where, "kind"), f"must be one of {sorted(LANE_KINDS)}")
        provider = _text(record, "catalog_provider", where)
        path = _optional_text(record, "path", where)
        if kind == "listing-file":
            if path is None:
                raise OfferingsInputError(_name(where, "path"), "required for a listing-file lane")
        elif path is not None:
            raise OfferingsInputError(_name(where, "path"), "only a listing-file lane may set path")
        if provider in by_provider:
            raise OfferingsInputError(
                _name(where, "catalog_provider"),
                f"duplicate provider mapping for {provider!r} (already used by {by_provider[provider]!r})",
            )
        by_provider[provider] = name
        lanes[name] = SourceLane(name, kind, provider, path)
    return Sources(version=version, notices=notices, lanes=MappingProxyType(lanes))


# --- listing ----------------------------------------------------------------


@dataclass(frozen=True)
class ListingModel:
    """One listing row: the provider plus the attributes compared against policy."""

    provider: str
    id: str
    name: str
    context_window: int | None
    max_tokens: int | None
    reasoning: bool
    thinking: tuple[str, ...] | None
    input: tuple[str, ...]
    cost: Mapping[str, Any]


@dataclass(frozen=True)
class SourceGap:
    """A lane whose proof is absent; the check records it instead of inventing a time."""

    lane: str
    provider: str
    reason: str


@dataclass(frozen=True)
class Listing:
    lane: str
    provider: str
    models: tuple[ListingModel, ...]
    provenance: Mapping[str, Any] | None
    gaps: tuple[SourceGap, ...]
    sha256: str


def _attribute(record: Mapping[str, Any], key: str, where: str) -> Any:
    field = _name(where, key)
    if key not in record:
        raise OfferingsInputError(field, "missing")
    value = record[key]
    if key == "name":
        if not isinstance(value, str) or not value.strip():
            raise OfferingsInputError(field, "must be a non-empty string")
    elif key in ("contextWindow", "maxTokens"):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise OfferingsInputError(field, "must be an integer or null")
    elif key == "reasoning":
        if not isinstance(value, bool):
            raise OfferingsInputError(field, "must be a boolean")
    elif key == "thinking":
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(item, str) for item in value)
        ):
            raise OfferingsInputError(field, "must be a list of effort strings or null")
    elif key == "input":
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item in INPUT_MODALITIES for item in value
        ):
            raise OfferingsInputError(field, f"must be a list drawn from {sorted(INPUT_MODALITIES)}")
    elif key == "cost":
        if not isinstance(value, dict):
            raise OfferingsInputError(field, "must be an object")
    return value


def _listing_model(record: Mapping[str, Any], where: str, provider: str) -> ListingModel:
    row_provider = _text(record, "provider", where)
    if row_provider != provider:
        raise OfferingsInputError(
            _name(where, "provider"),
            f"must equal the configured provider {provider!r}, got {row_provider!r}",
        )
    model_id = _text(record, "id", where)
    thinking_raw = _attribute(record, "thinking", where)
    return ListingModel(
        provider=row_provider,
        id=model_id,
        name=_attribute(record, "name", where),
        context_window=_attribute(record, "contextWindow", where),
        max_tokens=_attribute(record, "maxTokens", where),
        reasoning=_attribute(record, "reasoning", where),
        thinking=None if thinking_raw is None else tuple(thinking_raw),
        input=tuple(_attribute(record, "input", where)),
        cost=MappingProxyType(_attribute(record, "cost", where)),
    )


def _cli_proof(record: Mapping[str, Any], lane: SourceLane) -> dict[str, Any]:
    where = f"discovery.{lane.catalog_provider}"
    provider = _text(record, "provider", where)
    if provider != lane.catalog_provider:
        raise OfferingsInputError(
            _name(where, "provider"),
            f"must equal {lane.catalog_provider!r}, got {provider!r}",
        )
    status = _text(record, "status", where)
    if status != CLI_PROOF_STATUS:
        raise OfferingsInputError(_name(where, "status"), f"must be {CLI_PROOF_STATUS!r} for a live listing")
    stale = record.get("stale")
    if stale is not False:
        raise OfferingsInputError(_name(where, "stale"), "must be false")
    source = _text(record, "source", where)
    if source != CLI_PROOF_SOURCE:
        raise OfferingsInputError(_name(where, "source"), f"must be {CLI_PROOF_SOURCE!r}")
    fetched = record.get("fetchedAt")
    if not isinstance(fetched, int) or isinstance(fetched, bool) or fetched < 0:
        raise OfferingsInputError(_name(where, "fetchedAt"), "must be epoch milliseconds as a non-negative integer")
    return {"provider": provider, "status": status, "stale": stale, "source": source, "fetchedAt": fetched}


def _file_proof(record: Mapping[str, Any], lane: SourceLane) -> dict[str, Any]:
    where = "retrieval"
    provider = _text(record, "provider", where)
    if provider != lane.catalog_provider:
        raise OfferingsInputError(
            _name(where, "provider"),
            f"must equal {lane.catalog_provider!r}, got {provider!r}",
        )
    source = _text(record, "source", where)
    retrieved_at = _utc(record.get("retrieved_at"), _name(where, "retrieved_at"))
    success = record.get("success")
    if success is not True:
        raise OfferingsInputError(_name(where, "success"), "must be true")
    return {"provider": provider, "source": source, "retrieved_at": retrieved_at, "success": success}


def _listing_proof(data: Mapping[str, Any], lane: SourceLane) -> tuple[Mapping[str, Any] | None, SourceGap | None]:
    if lane.kind == "omp-cli":
        key, proof_reader, reason = "discovery", _cli_proof, "no cli discovery proof for provider"
    else:
        key, proof_reader, reason = "retrieval", _file_proof, "no file retrieval proof"
    if key not in data:
        return None, SourceGap(lane.lane, lane.catalog_provider, reason)
    container = _object(data[key], key)
    if lane.kind == "omp-cli":
        if lane.catalog_provider not in container:
            return None, SourceGap(lane.lane, lane.catalog_provider, reason)
        record = _object(container[lane.catalog_provider], f"{key}.{lane.catalog_provider}")
    else:
        record = container
    return MappingProxyType(proof_reader(record, lane)), None


def parse_listing(raw: bytes | str, lane: SourceLane) -> Listing:
    """Parse one lane's ``omp models --json`` shape against its configured provider.

    Every row's ``provider`` must equal ``lane.catalog_provider`` and model ids
    must be unique. CLI lanes need a live ``discovery[ID]`` proof; listing-file
    lanes need a ``retrieval`` proof. Absent proof becomes a :class:`SourceGap`,
    never a fabricated retrieval time.
    """
    digest = _digest(raw)
    data = _object(_decode(raw), "<root>")
    if "models" not in data:
        raise OfferingsInputError("models", "missing")
    models_raw = data["models"]
    if not isinstance(models_raw, list):
        raise OfferingsInputError("models", "must be a list")

    models: list[ListingModel] = []
    seen: set[str] = set()
    for index, row in enumerate(models_raw):
        where = f"models[{index}]"
        record = _object(row, where)
        model = _listing_model(record, where, lane.catalog_provider)
        if model.id in seen:
            raise OfferingsInputError(_name(where, "id"), f"duplicate model id {model.id!r}")
        seen.add(model.id)
        models.append(model)

    provenance, gap = _listing_proof(data, lane)
    gaps = () if gap is None else (gap,)
    return Listing(
        lane=lane.lane,
        provider=lane.catalog_provider,
        models=tuple(models),
        provenance=provenance,
        gaps=gaps,
        sha256=digest,
    )


# --- notices ----------------------------------------------------------------


@dataclass(frozen=True)
class NoticeItem:
    model: str
    type: Literal["retirement", "deprecation", "failure"]
    url: str
    published: date
    effective: date | None


@dataclass(frozen=True)
class NoticesDimension:
    """Retirement/deprecation/failure coverage; ``items`` is None when not published."""

    coverage: Literal["checked", "not_published"]
    source: str
    retrieved_at: datetime
    items: tuple[NoticeItem, ...] | None


@dataclass(frozen=True)
class StatusDimension:
    """Provider status coverage; ``value`` is None when not published."""

    coverage: Literal["checked", "not_published"]
    source: str
    retrieved_at: datetime
    value: Literal["operational", "degraded", "outage"] | None


@dataclass(frozen=True)
class LaneNotices:
    """One lane's covered dimensions; a missing dimension stays None for comparison."""

    lane: str
    notices: NoticesDimension | None
    status: StatusDimension | None


@dataclass(frozen=True)
class Notices:
    version: str
    providers: Mapping[str, LaneNotices]


def _coverage(record: Mapping[str, Any], field: str) -> tuple[str, str, datetime]:
    coverage = _text(record, "coverage", field)
    if coverage not in COVERAGE_VALUES:
        raise OfferingsInputError(_name(field, "coverage"), f"must be one of {sorted(COVERAGE_VALUES)}")
    source = _text(record, "source", field)
    retrieved_at = _utc(record.get("retrieved_at"), _name(field, "retrieved_at"))
    return coverage, source, retrieved_at


def _notice_item(record: Mapping[str, Any], where: str) -> NoticeItem:
    model = _text(record, "model", where)
    notice_type = _text(record, "type", where)
    if notice_type not in NOTICE_TYPES:
        raise OfferingsInputError(_name(where, "type"), f"must be one of {sorted(NOTICE_TYPES)}")
    url = _text(record, "url", where)
    published = _calendar_date(record.get("published"), _name(where, "published"))
    if "effective" not in record:
        raise OfferingsInputError(_name(where, "effective"), "missing")
    effective = None if record["effective"] is None else _calendar_date(
        record["effective"], _name(where, "effective")
    )
    return NoticeItem(model=model, type=notice_type, url=url, published=published, effective=effective)


def _notices_dimension(record: Mapping[str, Any], field: str) -> NoticesDimension:
    coverage, source, retrieved_at = _coverage(record, field)
    if coverage == "not_published":
        if "items" in record:
            raise OfferingsInputError(_name(field, "items"), "must be absent when coverage is not_published")
        return NoticesDimension(coverage, source, retrieved_at, None)
    if "items" not in record:
        raise OfferingsInputError(_name(field, "items"), "must be a list when coverage is checked")
    items_raw = record["items"]
    if not isinstance(items_raw, list):
        raise OfferingsInputError(_name(field, "items"), "must be a list when coverage is checked")
    items = tuple(_notice_item(_object(item, f"{field}.items[{index}]"), f"{field}.items[{index}]")
                  for index, item in enumerate(items_raw))
    return NoticesDimension(coverage, source, retrieved_at, items)


def _status_dimension(record: Mapping[str, Any], field: str) -> StatusDimension:
    coverage, source, retrieved_at = _coverage(record, field)
    if coverage == "not_published":
        if "value" in record:
            raise OfferingsInputError(_name(field, "value"), "must be absent when coverage is not_published")
        return StatusDimension(coverage, source, retrieved_at, None)
    value = _text(record, "value", field)
    if value not in STATUS_VALUES:
        raise OfferingsInputError(_name(field, "value"), f"must be one of {sorted(STATUS_VALUES)}")
    return StatusDimension(coverage, source, retrieved_at, value)


def parse_notices(raw: bytes | str) -> Notices:
    """Parse ``offerings-notices.v1`` per-lane coverage.

    A lane or a notices/status dimension absent from the capture stays absent,
    so a comparison can tell it apart from ``checked`` with no published items.
    """
    data = _object(_decode(raw), "<root>")
    version = _text(data, "version", "")
    if version != NOTICES_VERSION:
        raise OfferingsInputError("version", f"expected {NOTICES_VERSION!r}, got {version!r}")
    if "providers" not in data:
        raise OfferingsInputError("providers", "missing")
    providers_raw = _object(data["providers"], "providers")

    providers: dict[str, LaneNotices] = {}
    for name, value in providers_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise OfferingsInputError("providers", "lane name must be a non-empty string")
        where = f"providers.{name}"
        record = _object(value, where)
        notices = (
            _notices_dimension(_object(record["notices"], _name(where, "notices")), _name(where, "notices"))
            if "notices" in record else None
        )
        status = (
            _status_dimension(_object(record["status"], _name(where, "status")), _name(where, "status"))
            if "status" in record else None
        )
        providers[name] = LaneNotices(name, notices, status)
    return Notices(version=version, providers=MappingProxyType(providers))
