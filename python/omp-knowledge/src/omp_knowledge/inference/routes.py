"""Declared inference routes with explicit health probing and fallback.

A route file declares, per role, a primary and an optional fallback profile.
Each profile pins its provider (``llama.cpp`` or ``order``), its accelerator, and
— for the embedding role — the embedding identity whose ``generation_id`` must
match between a primary and its fallback. A probe is one ``GET /health``: an
``order`` route is healthy without dialing anything. Resolution returns the
profile it selected, why it moved past the primary, and raises
``RouteUnavailable`` when neither resolves.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, ValidationError, field_validator, model_validator

from omp_work.v1.models import StrictModel

from omp_knowledge.errors import KnowledgeError
from omp_knowledge.inference.embedding import LOOPBACK_HOSTS, EmbeddingProfile

PROBE_TIMEOUT_S = 5.0

RouteRole = Literal["embedding", "reranker", "generator"]
RouteProvider = Literal["llama.cpp", "order"]
Accelerator = Literal["gpu", "cpu"]
RouteSelection = Literal["primary", "fallback"]


class RouteConfigError(KnowledgeError):
    """400: route file malformed/contradictory, or an incompatible fallback."""

    code = "route_config_invalid"
    status_code = 400


class RouteUnavailable(KnowledgeError):
    """503: no declared route for the role resolves to a healthy profile."""

    code = "route_unavailable"
    status_code = 503

    def __init__(
        self, message: str = "route_unavailable: no healthy profile for role"
    ) -> None:
        super().__init__(message)


def _loopback_endpoint(endpoint: str) -> str:
    host = urlsplit(endpoint).hostname
    if host is None or host.lower() not in LOOPBACK_HOSTS:
        raise ValueError(
            f"loopback host required (127.0.0.1, localhost, ::1); refusing endpoint {endpoint!r}"
        )
    return endpoint.rstrip("/")


class RouteProfile(StrictModel):
    """One declared inference endpoint. A ``llama.cpp`` route names its model and
    loopback endpoint; an ``order`` route is a local deterministic ordering with
    neither property.
    """

    name: str = Field(min_length=1)
    provider: RouteProvider
    model: str | None = None
    endpoint: str | None = None
    accelerator: Accelerator
    embedding_profile: EmbeddingProfile | None = None

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, endpoint: str | None) -> str | None:
        if endpoint is None:
            return None
        return _loopback_endpoint(endpoint)

    @model_validator(mode="after")
    def _check_provider_shape(self) -> RouteProfile:
        if self.provider == "llama.cpp":
            if not self.model:
                raise ValueError("a llama.cpp route requires a model")
            if self.endpoint is None:
                raise ValueError("a llama.cpp route requires an endpoint")
            return self
        if self.model is not None or self.endpoint is not None:
            raise ValueError("an order route declares no model and no endpoint")
        return self


class RouteEntry(StrictModel):
    """One role's primary profile and its optional fallback."""

    role: RouteRole
    primary: RouteProfile
    fallback: RouteProfile | None = None


class RouteSet(StrictModel):
    """The declared route table, in file order."""

    routes: tuple[RouteEntry, ...]

    def entry(self, role: str) -> RouteEntry | None:
        for entry in self.routes:
            if entry.role == role:
                return entry
        return None


class RouteHealth(StrictModel):
    """The outcome of one probe: identity, health, a detail for the reason, latency."""

    name: str
    ok: bool
    detail: str
    latency_ms: float


class ResolvedRoute(StrictModel):
    """The profile a role resolved to, and why it left the primary."""

    role: RouteRole
    profile: RouteProfile
    used: RouteSelection
    reason: str


def load_routes(path: str | Path) -> RouteSet:
    """Read and validate a route file, refusing every contradictory declaration."""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RouteConfigError(f"route_config_invalid: {exc}") from exc
    try:
        route_set = RouteSet.model_validate(payload)
    except ValidationError as exc:
        raise RouteConfigError(f"route_config_invalid: {exc}") from exc
    _validate_roles(route_set)
    return route_set


def _validate_roles(route_set: RouteSet) -> None:
    seen: set[str] = set()
    for entry in route_set.routes:
        if entry.role in seen:
            raise RouteConfigError(
                f"route_config_invalid: duplicate role {entry.role!r}"
            )
        seen.add(entry.role)
        for position, profile in (
            ("primary", entry.primary),
            ("fallback", entry.fallback),
        ):
            if profile is None:
                continue
            _validate_profile(entry.role, position, profile)
        if (
            entry.role == "embedding"
            and entry.fallback is not None
            and entry.fallback.embedding_profile is not None
            and entry.primary.embedding_profile is not None
            and entry.fallback.embedding_profile.generation_id
            != entry.primary.embedding_profile.generation_id
        ):
            raise RouteConfigError(
                "route_incompatible: embedding fallback "
                f"{entry.fallback.name!r} generation "
                f"{entry.fallback.embedding_profile.generation_id} does not match primary "
                f"{entry.primary.name!r} generation "
                f"{entry.primary.embedding_profile.generation_id}",
                code="route_incompatible",
            )


def _validate_profile(role: str, position: str, profile: RouteProfile) -> None:
    if profile.provider == "order" and role != "reranker":
        raise RouteConfigError(
            f"route_config_invalid: the order provider is only valid for the reranker "
            f"role, got {role!r} {position} route {profile.name!r}"
        )
    if role == "embedding":
        if profile.embedding_profile is None:
            raise RouteConfigError(
                f"route_config_invalid: the embedding {position} route {profile.name!r} "
                "requires an embedding profile"
            )
        if profile.embedding_profile.model != profile.model:
            raise RouteConfigError(
                f"route_config_invalid: the embedding {position} route {profile.name!r} "
                f"profile model {profile.embedding_profile.model!r} does not match its "
                f"route model {profile.model!r}"
            )
    elif profile.embedding_profile is not None:
        raise RouteConfigError(
            f"route_config_invalid: the {role!r} {position} route {profile.name!r} must "
            "not declare an embedding profile"
        )


def _health(
    profile: RouteProfile, started: float, *, ok: bool, detail: str
) -> RouteHealth:
    return RouteHealth(
        name=profile.name,
        ok=ok,
        detail=detail,
        latency_ms=round((time.monotonic() - started) * 1000.0, 3),
    )


def probe(profile: RouteProfile, timeout_s: float = PROBE_TIMEOUT_S) -> RouteHealth:
    """GET ``{endpoint}/health``; healthy iff 200. An order profile is always healthy."""
    if profile.provider == "order":
        return RouteHealth(name=profile.name, ok=True, detail="order", latency_ms=0.0)
    started = time.monotonic()
    # Empty proxy map keeps a loopback probe from following http_proxy off-box.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{profile.endpoint}/health", method="GET")
    try:
        with opener.open(request, timeout=timeout_s) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        return _health(profile, started, ok=False, detail=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _health(profile, started, ok=False, detail=f"unreachable: {exc}")
    return _health(
        profile, started, ok=status == 200, detail="ok" if status == 200 else f"HTTP {status}"
    )


def resolve(
    route_set: RouteSet,
    role: str,
    prober: Callable[[RouteProfile], RouteHealth] = probe,
) -> ResolvedRoute:
    """Select the first healthy profile for ``role``; refuse when none is declared
    or none is healthy.
    """
    entry = route_set.entry(role)
    if entry is None:
        raise RouteUnavailable(
            f"route_unavailable: no route declared for role {role!r}"
        )
    primary_health = prober(entry.primary)
    if primary_health.ok:
        return ResolvedRoute(
            role=entry.role, profile=entry.primary, used="primary", reason=""
        )
    if entry.fallback is not None and prober(entry.fallback).ok:
        return ResolvedRoute(
            role=entry.role,
            profile=entry.fallback,
            used="fallback",
            reason=primary_health.detail,
        )
    raise RouteUnavailable(
        f"route_unavailable: no healthy route for role {entry.role!r} "
        f"(primary {entry.primary.name!r}: {primary_health.detail})"
    )


__all__ = [
    "Accelerator",
    "PROBE_TIMEOUT_S",
    "ResolvedRoute",
    "RouteConfigError",
    "RouteEntry",
    "RouteHealth",
    "RouteProfile",
    "RouteProvider",
    "RouteRole",
    "RouteSelection",
    "RouteSet",
    "RouteUnavailable",
    "load_routes",
    "probe",
    "resolve",
]
