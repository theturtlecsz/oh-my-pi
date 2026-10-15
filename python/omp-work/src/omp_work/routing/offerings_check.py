"""Read-only daily comparison of provider offerings against the governing policy (OMP-615-s09).

``compare`` takes already-parsed owner captures (``offerings_inputs``) and the
routing policy and returns findings plus the snapshot to retain. It makes no
provider call and edits no routing file: a new listing is a finding that opens an
evaluation, never a routing change. A listing is only trusted when it carries
fresh proof for its configured provider; otherwise the lane is ``unchecked`` and
its last successful snapshot is kept. Retirement is never inferred from a gap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal, Mapping, NamedTuple

from .model_review import policy_recipes
from .offerings_inputs import (
    LaneNotices,
    Listing,
    ListingModel,
    Notices,
    OfferingsInputError,
    SourceGap,
    SourceLane,
    Sources,
)
from .policy import RoutingPolicy

FindingKind = Literal["new", "changed", "retired", "deprecated", "failing", "unchecked"]

RECIPE_PREFIXES = ("stage:", "reviewer:", "tier:", "alternate:")
NOTICE_KINDS: Mapping[str, FindingKind] = {
    "retirement": "retired",
    "deprecation": "deprecated",
    "failure": "failing",
}
URGENT_KINDS = frozenset({"retired", "deprecated", "failing"})
CLI_SOURCE = "omp models --json"

Snapshot = dict[str, dict[str, Any]]
ListingInput = Listing | SourceGap | OfferingsInputError | None


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    lane: str | None
    model: str | None
    effort: str | None
    recipes: tuple[str, ...]
    in_use: bool
    urgent: bool
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "lane": self.lane,
            "model": self.model,
            "effort": self.effort,
            "recipes": list(self.recipes),
            "in_use": self.in_use,
            "urgent": self.urgent,
            "evidence": dict(self.evidence),
        }


class Comparison(NamedTuple):
    findings: tuple[Finding, ...]
    snapshot: Snapshot


@dataclass
class _Lane:
    """Per-lane working state: how the listing fared and who uses which model."""

    source: SourceLane
    listing: Listing | None = None
    cause: str | None = None
    time: datetime | None = None
    uses: list[tuple[str, bool, str]] = field(default_factory=list)  # (selector, reviewer?, recipe)
    in_use: dict[tuple[str, str | None], set[str]] = field(default_factory=dict)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _attrs(model: ListingModel) -> dict[str, Any]:
    return {
        "name": model.name,
        "context_window": model.context_window,
        "max_tokens": model.max_tokens,
        "reasoning": model.reasoning,
        "thinking": None if model.thinking is None else list(model.thinking),
        "input": list(model.input),
        "cost": json.loads(json.dumps(dict(model.cost))),
    }


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _listing_source(lane: SourceLane) -> str:
    if lane.kind == "omp-cli":
        return f"{CLI_SOURCE}:discovery.{lane.catalog_provider}"
    return lane.path or lane.lane


def _listing_time(listing: Listing) -> datetime | None:
    proof = listing.provenance
    if proof is None:
        return None
    if "fetchedAt" in proof:
        return datetime.fromtimestamp(proof["fetchedAt"] / 1000, tz=timezone.utc)
    return proof["retrieved_at"]


def _validate_listing(
    state: _Lane, value: ListingInput, checked_at: datetime, max_age: timedelta
) -> None:
    lane = state.source
    if value is None:
        state.cause = "listing missing"
    elif isinstance(value, OfferingsInputError):
        state.cause = f"listing failed: {value}"
    elif isinstance(value, SourceGap):
        state.cause = f"listing unproven: {value.reason}"
    elif value.gaps or value.provenance is None:
        reasons = "; ".join(gap.reason for gap in value.gaps) or "no freshness proof"
        state.cause = f"listing unproven: {reasons}"
    elif value.provider != lane.catalog_provider:
        state.cause = f"listing provider {value.provider!r} is not {lane.catalog_provider!r}"
    elif not value.models:
        state.cause = "listing empty"
    else:
        moment = _listing_time(value)
        state.time = moment
        if moment is None:
            state.cause = "listing unproven: no freshness proof"
        elif moment > checked_at:
            state.cause = f"listing future-dated: {_iso(moment)}"
        elif checked_at - moment > max_age:
            state.cause = f"listing stale: retrieved {_iso(moment)}"
        else:
            state.listing = value


def _resolve(selector: str, reviewer: bool, ids: set[str] | None) -> tuple[str, str | None]:
    """Complete id first (``:cloud`` is part of an id); only then a trailing effort."""
    if ids is None or selector in ids or not reviewer or ":" not in selector:
        return selector, None
    model, effort = selector.rsplit(":", 1)
    return model, effort


def _lane_prior(previous: Mapping[str, Any], state: _Lane) -> Mapping[str, Any] | None:
    entry = previous.get(state.source.lane)
    if isinstance(entry, Mapping) and entry.get("catalog_provider") == state.source.catalog_provider:
        return entry
    return None


class _Collector:
    def __init__(self, checked_at: datetime) -> None:
        self.checked_at = checked_at
        self.findings: list[Finding] = []

    def add(
        self,
        kind: FindingKind,
        state: _Lane | None,
        model: str | None,
        effort: str | None,
        recipes: set[str],
        *,
        in_use: bool,
        source: str | None,
        time: datetime | None,
        detail: str,
        raw_sha256: str | None = None,
        listing_sha256: str | None = None,
        cause: str | None = None,
    ) -> None:
        evidence: dict[str, Any] = {
            "source": source,
            "time": None if time is None else _iso(time),
            "checked_at": _iso(self.checked_at),
            "raw_sha256": raw_sha256,
            "listing_sha256": listing_sha256,
            "detail": detail,
        }
        missing = [name for name, value in (("raw", raw_sha256), ("listing", listing_sha256)) if value is None]
        if missing:
            evidence["cause"] = cause or f"{'/'.join(missing)} bytes unavailable"
        self.findings.append(
            Finding(
                kind=kind,
                lane=None if state is None else state.source.lane,
                model=model,
                effort=effort,
                recipes=tuple(sorted(recipes)),
                in_use=in_use,
                urgent=in_use and kind in URGENT_KINDS,
                evidence=evidence,
            )
        )


def compare(
    policy: RoutingPolicy,
    sources: Sources,
    listings: Mapping[str, ListingInput],
    notices: Notices | None,
    previous: Mapping[str, Any] | None,
    checked_at: datetime,
    max_age_hours: float = 36,
) -> Comparison:
    """Compare captures with the policy; return findings and the snapshot to retain."""
    if checked_at.tzinfo is None or checked_at.utcoffset() != timedelta(0):
        raise ValueError("checked_at must be a UTC datetime")
    max_age = timedelta(hours=max_age_hours)
    prior_snapshot: Mapping[str, Any] = previous or {}
    out = _Collector(checked_at)
    states = {name: _Lane(lane) for name, lane in sources.lanes.items()}
    by_provider = {lane.catalog_provider: states[name] for name, lane in sources.lanes.items()}

    recipes, _ = policy_recipes(policy)
    for key in sorted(recipes):
        if not key.startswith(RECIPE_PREFIXES):
            continue
        recipe = recipes[key]
        if key.startswith("reviewer:"):
            identity = recipe["identity"]
            provider, _, selector = identity.partition("/")
            reviewer = True
        else:
            provider, selector, reviewer = recipe["provider"], recipe["model"], False
            identity = f"{provider}/{selector}"
        state = by_provider.get(provider) if selector else None
        if state is None:
            out.add(
                "unchecked", None, identity, None, {key}, in_use=True, source=None, time=None,
                detail=f"no source lane maps provider {provider!r}",
            )
        else:
            state.uses.append((selector, reviewer, key))

    for name, state in states.items():
        _validate_listing(state, listings.get(name), checked_at, max_age)
        prior = _lane_prior(prior_snapshot, state)
        if state.listing is not None:
            ids: set[str] | None = {model.id for model in state.listing.models}
        else:
            ids = set(prior["models"]) if prior and isinstance(prior.get("models"), Mapping) else None
        for selector, reviewer, key in state.uses:
            model, effort = _resolve(selector, reviewer, ids)
            state.in_use.setdefault((model, effort), set()).add(key)

    for name, state in states.items():
        _compare_listing(out, state, _lane_prior(prior_snapshot, state))
        if state.uses:
            _compare_notices(out, state, notices, checked_at, max_age)

    return Comparison(tuple(out.findings), _retain(states, prior_snapshot))


def _lane_recipes(state: _Lane) -> set[str]:
    return {key for keys in state.in_use.values() for key in keys}


def _compare_listing(out: _Collector, state: _Lane, prior: Mapping[str, Any] | None) -> None:
    source = _listing_source(state.source)
    listing = state.listing
    if listing is None:
        if state.uses:
            out.add(
                "unchecked", state, None, None, _lane_recipes(state), in_use=True, source=source,
                time=state.time, detail=state.cause or "listing unavailable",
                cause=state.cause,
            )
        return
    current = {model.id: _attrs(model) for model in listing.models}
    shared = {
        "source": source, "time": state.time, "raw_sha256": listing.sha256, "listing_sha256": _digest(current),
    }
    used = {model for model, _ in state.in_use}
    prior_models: Mapping[str, Any] | None = None
    if prior and isinstance(prior.get("models"), Mapping):
        prior_models = prior["models"]

    for (model, effort), keys in sorted(state.in_use.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        if model not in current:
            out.add("retired", state, model, effort, keys, in_use=True,
                    detail="in-use model absent from provider listing", **shared)
    for model_id, attrs in current.items():
        if prior_models is None:
            if model_id not in used:
                out.add("new", state, model_id, None, set(), in_use=False,
                        detail="first listing; model not in use", **shared)
            continue
        old = prior_models.get(model_id)
        if old is None:
            if model_id in used:
                for (model, effort), keys in state.in_use.items():
                    if model == model_id:
                        out.add("new", state, model_id, effort, keys, in_use=True,
                                detail="model absent from prior listing", **shared)
            else:
                out.add("new", state, model_id, None, set(), in_use=False,
                        detail="model absent from prior listing", **shared)
        elif old != attrs:
            diff = ", ".join(
                f"{attr}: {old.get(attr)!r} -> {attrs[attr]!r}" for attr in attrs if old.get(attr) != attrs[attr]
            )
            targets = [(e, k) for (m, e), k in state.in_use.items() if m == model_id] or [(None, set())]
            for effort, keys in targets:
                out.add("changed", state, model_id, effort, keys, in_use=bool(keys),
                        detail=f"changed attributes: {diff}", **shared)
    if prior_models is not None:
        for model_id in prior_models:
            if model_id not in current and model_id not in used:
                out.add("retired", state, model_id, None, set(), in_use=False,
                        detail="prior listing model absent from provider listing", **shared)


def _dimension_gap(
    dimension: Any, label: str, checked_at: datetime, max_age: timedelta
) -> str | None:
    if dimension is None:
        return f"{label} dimension absent from notices capture"
    if dimension.retrieved_at > checked_at:
        return f"{label} coverage future-dated: {_iso(dimension.retrieved_at)}"
    if checked_at - dimension.retrieved_at > max_age:
        return f"{label} coverage stale: retrieved {_iso(dimension.retrieved_at)}"
    return None


def _compare_notices(
    out: _Collector, state: _Lane, notices: Notices | None, checked_at: datetime, max_age: timedelta
) -> None:
    recipes = _lane_recipes(state)

    def gap(detail: str, source: str | None = None, time: datetime | None = None) -> None:
        out.add("unchecked", state, None, None, recipes, in_use=True, source=source, time=time,
                detail=detail, cause="notices capture bytes not retained")

    if notices is None:
        gap("notices file missing")
        return
    lane_notices: LaneNotices | None = notices.providers.get(state.source.lane)
    if lane_notices is None:
        gap("lane absent from notices capture")
        return
    for label, dimension in (("notices", lane_notices.notices), ("status", lane_notices.status)):
        problem = _dimension_gap(dimension, label, checked_at, max_age)
        if problem is not None:
            gap(problem, None if dimension is None else dimension.source,
                None if dimension is None else dimension.retrieved_at)
            continue
        listing_sha = _digest({m.id: _attrs(m) for m in state.listing.models}) if state.listing else None
        common: dict[str, Any] = {
            "source": dimension.source, "time": dimension.retrieved_at, "listing_sha256": listing_sha,
            "cause": "notices capture bytes not retained",
        }
        if label == "notices" and dimension.items:
            for item in dimension.items:
                matched = [(e, k) for (m, e), k in state.in_use.items() if m == item.model]
                effective = "unscheduled" if item.effective is None else f"effective {item.effective.isoformat()}"
                detail = f"{item.type} notice published {item.published.isoformat()}, {effective}: {item.url}"
                for effort, keys in matched or [(None, set())]:
                    out.add(NOTICE_KINDS[item.type], state, item.model, effort, keys, in_use=bool(keys),
                            detail=detail, **common)
        elif label == "status" and dimension.value in ("degraded", "outage"):
            for (model, effort), keys in sorted(state.in_use.items(), key=lambda i: (i[0][0], i[0][1] or "")):
                out.add("failing", state, model, effort, keys, in_use=True,
                        detail=f"provider status {dimension.value}", **common)


def _retain(states: Mapping[str, _Lane], previous: Mapping[str, Any]) -> Snapshot:
    snapshot: Snapshot = {name: entry for name, entry in previous.items() if isinstance(entry, dict)}
    for name, state in states.items():
        listing = state.listing
        if listing is None:
            continue
        models = {model.id: _attrs(model) for model in listing.models}
        assert state.time is not None
        snapshot[name] = {
            "catalog_provider": state.source.catalog_provider,
            "models": models,
            "evidence": {
                "source": _listing_source(state.source),
                "time": _iso(state.time),
                "raw_sha256": listing.sha256,
                "listing_sha256": _digest(models),
            },
        }
    return snapshot
