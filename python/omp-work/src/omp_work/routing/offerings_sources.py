"""Byte-level fetchers for the read-only daily model-offerings check (OMP-615-s09-s03).

The check never prompts a model and never spends paid usage, so it reads each
in-use lane through the tool that already owns that provider: the local ``omp``
CLI for ``omp-cli`` lanes, and a captured listing file for ``listing-file``
lanes. Every read is one bounded, non-interactive subprocess or one byte read of
a path the owner configured, and the bytes are handed to the frozen parsers in
:mod:`omp_work.routing.offerings_inputs` — no shape is re-validated here.

Two rules a reader must not lose:

* A capture is trusted only when it proves the *exact* configured provider with
  fresh, provider-sourced discovery (the s01 CLI proof) or a successful retrieval
  proof. Anything else — a ``bundled``/``cache``/``models.dev`` fallback, a
  stale or foreign proof, a legacy capture without a discovery block, empty
  output — is a :class:`SourceGap`, never a listing and never a fabricated time.
* The injected clock records *when this process attempted the read*, full stop.
  It is never used as a retrieval time: a file lane's ``retrieved_at`` and a CLI
  lane's ``fetchedAt`` come only from the bytes, so an attempt can be logged
  without inventing the evidence the check exists to verify.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

from .offerings_inputs import (
    Listing,
    Notices,
    OfferingsInputError,
    SourceGap,
    SourceLane,
    Sources,
    parse_listing,
    parse_notices,
)

__all__ = [
    "CLI_TIMEOUT_SECONDS",
    "DEFAULT_OMP",
    "LaneCapture",
    "NoticesCapture",
    "NoticesGap",
    "fetch_all",
    "fetch_cli",
    "fetch_file",
    "load_notices",
]

CLI_TIMEOUT_SECONDS = 180.0
DEFAULT_OMP = "omp"

# Reasons are evidence, not logs: keep them bounded so a noisy CLI error cannot
# push the useful part of the cause out of the finding.
_REASON_LIMIT = 200

AttemptClock = Callable[[], datetime]
ByteReader = Callable[[str], bytes]


@dataclass(frozen=True)
class LaneCapture:
    """One lane's bytes, read once, with the instant the read was attempted.

    ``value`` is the parsed :class:`~omp_work.routing.offerings_inputs.Listing`,
    or the :class:`~omp_work.routing.offerings_inputs.SourceGap` that explains why
    the bytes could not be trusted. ``attempted_at`` is when this process began
    the read and is never a retrieval time. ``sha256`` digests exactly the bytes
    that were read (CLI stdout or the listing file), or is None when the read
    produced nothing at all (launch failure, timeout before output, missing
    file).
    """

    lane: str
    provider: str
    attempted_at: datetime
    value: Listing | SourceGap
    sha256: str | None

    def listing(self) -> Listing | None:
        """The parsed listing when the capture was trusted, else None."""
        return self.value if isinstance(self.value, Listing) else None

    def gap(self) -> SourceGap | None:
        """The evidence gap when the capture was not trusted, else None."""
        return self.value if isinstance(self.value, SourceGap) else None


@dataclass(frozen=True)
class NoticesGap:
    """The notices capture could not be read or parsed; ``path`` names it."""

    path: str
    reason: str


@dataclass(frozen=True)
class NoticesCapture:
    """The notices file's bytes, read once, retaining each dimension's provenance.

    ``value`` is the parsed :class:`~omp_work.routing.offerings_inputs.Notices`
    (whose dimensions keep their own ``source``/``retrieved_at``) or a
    :class:`NoticesGap`. ``attempted_at`` is the attempt instant, never a
    retrieval time.
    """

    path: str
    attempted_at: datetime
    value: Notices | NoticesGap
    sha256: str | None

    def notices(self) -> Notices | None:
        """The parsed notices when the capture was readable, else None."""
        return self.value if isinstance(self.value, Notices) else None


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _attempt_at(clock: AttemptClock) -> datetime:
    """Stamp one read attempt; refuse a clock that cannot label a UTC instant."""
    moment = clock()
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("attempt clock must return a UTC-aware datetime")
    return moment


def _default_read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _digest(raw: bytes | None) -> str | None:
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _bytes(value: bytes | str | None) -> bytes | None:
    """The exact bytes of a subprocess stream, or None when nothing was produced."""
    if value is None:
        return None
    return value.encode("utf-8", "replace") if isinstance(value, str) else bytes(value)


def _text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def _snippet(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= _REASON_LIMIT else collapsed[: _REASON_LIMIT - 3] + "..."


def _gap(lane: SourceLane, reason: str, attempted_at: datetime, raw: bytes | None) -> LaneCapture:
    return LaneCapture(
        lane=lane.lane,
        provider=lane.catalog_provider,
        attempted_at=attempted_at,
        value=SourceGap(lane.lane, lane.catalog_provider, reason),
        sha256=_digest(raw),
    )


def _trust(lane: SourceLane, attempted_at: datetime, raw: bytes) -> LaneCapture:
    """Parse trusted-provider bytes into a listing, or explain the gap.

    The frozen parsers decide shape and proof: a refusal (malformed shape, a
    foreign row or proof, a ``bundled``/``cache``/``models.dev`` source) and an
    absent proof both have to become a gap here, because the check must record
    missing evidence rather than fail or pass on it.
    """
    try:
        listing = parse_listing(raw, lane)
    except OfferingsInputError as error:
        return _gap(lane, f"listing refused: {error}", attempted_at, raw)
    if listing.gaps:
        return _gap(lane, listing.gaps[0].reason, attempted_at, raw)
    if not listing.models:
        return _gap(lane, "listing empty", attempted_at, raw)
    return LaneCapture(
        lane=lane.lane,
        provider=lane.catalog_provider,
        attempted_at=attempted_at,
        value=listing,
        sha256=listing.sha256,
    )


def fetch_cli(
    lane: SourceLane,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    clock: AttemptClock = _default_clock,
    omp: str = DEFAULT_OMP,
    timeout: float = CLI_TIMEOUT_SECONDS,
) -> LaneCapture:
    """Run ``omp models refresh <provider> --json --no-extensions`` once and parse it.

    The command is the non-prompting listing path only. It is launched with
    ``shell=False``, ``stdin=DEVNULL``, captured output, and a hard timeout, so
    an interactive prompt can neither read nor consume the check's stdin and a
    hung CLI cannot stall the daily run. Launch failure, timeout, a nonzero exit,
    empty output, and invalid JSON each become a :class:`SourceGap` with the
    cause and the digest of whatever bytes were produced.
    """
    if lane.kind != "omp-cli":
        raise ValueError(f"fetch_cli requires an omp-cli lane, got {lane.kind!r}")
    attempted_at = _attempt_at(clock)
    argv = [omp, "models", "refresh", lane.catalog_provider, "--json", "--no-extensions"]
    try:
        proc = run(  # nosec B603 - fixed argv, no shell, listing path only
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as expired:
        raw = _bytes(expired.stdout)
        return _gap(lane, f"cli timed out after {timeout}s", attempted_at, raw)
    except OSError as error:
        return _gap(lane, f"cli could not start: {error}", attempted_at, None)

    raw = _bytes(proc.stdout)
    if proc.returncode != 0:
        detail = _snippet(_text(proc.stderr)) or _snippet(_text(proc.stdout))
        reason = f"cli exited {proc.returncode}" + (f": {detail}" if detail else "")
        return _gap(lane, reason, attempted_at, raw)
    if raw is None or not raw.strip():
        return _gap(lane, "cli produced no output", attempted_at, raw)
    return _trust(lane, attempted_at, raw)


def fetch_file(
    lane: SourceLane,
    *,
    read_bytes: ByteReader = _default_read_bytes,
    clock: AttemptClock = _default_clock,
) -> LaneCapture:
    """Read one configured listing file once and parse it against its provider.

    The file's own ``retrieval`` proof stays the only retrieval time; the clock
    labels the attempt. An unreadable file, empty content, or bytes the parsers
    refuse become a :class:`SourceGap`, with the digest of the raw bytes whenever
    they were read.
    """
    if lane.kind != "listing-file":
        raise ValueError(f"fetch_file requires a listing-file lane, got {lane.kind!r}")
    if lane.path is None:
        raise ValueError(f"listing-file lane {lane.lane!r} has no path")
    attempted_at = _attempt_at(clock)
    try:
        raw = read_bytes(lane.path)
    except OSError as error:
        return _gap(lane, f"listing file unreadable: {error}", attempted_at, None)
    if not raw.strip():
        return _gap(lane, "listing file empty", attempted_at, raw)
    return _trust(lane, attempted_at, raw)


def fetch_all(
    sources: Sources,
    *,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    read_bytes: ByteReader = _default_read_bytes,
    clock: AttemptClock = _default_clock,
    omp: str = DEFAULT_OMP,
    timeout: float = CLI_TIMEOUT_SECONDS,
) -> Mapping[str, LaneCapture]:
    """Read every configured in-use lane exactly once, keyed by lane name.

    Lanes are read in declaration order and never revisited: one CLI run or one
    file read per lane, so a lane that is already unavailable cannot be
    accidentally retried into a different answer later in the pass.
    """
    captures: dict[str, LaneCapture] = {}
    for name, lane in sources.lanes.items():
        if lane.kind == "omp-cli":
            captures[name] = fetch_cli(lane, run=run, clock=clock, omp=omp, timeout=timeout)
        else:
            captures[name] = fetch_file(lane, read_bytes=read_bytes, clock=clock)
    return MappingProxyType(captures)


def load_notices(
    sources: Sources,
    *,
    read_bytes: ByteReader = _default_read_bytes,
    clock: AttemptClock = _default_clock,
) -> NoticesCapture:
    """Read the configured notices capture once, retaining its provenance.

    A readable capture parses to :class:`~omp_work.routing.offerings_inputs.Notices`
    whose dimensions keep the ``source`` and ``retrieved_at`` the owner recorded,
    so the comparison can age each dimension on its own evidence. An unreadable
    or malformed capture is a :class:`NoticesGap`, with the digest of the raw
    bytes whenever they were read.
    """
    path = sources.notices
    attempted_at = _attempt_at(clock)
    try:
        raw = read_bytes(path)
    except OSError as error:
        return NoticesCapture(path, attempted_at, NoticesGap(path, f"notices unreadable: {error}"), None)
    try:
        notices = parse_notices(raw)
    except OfferingsInputError as error:
        return NoticesCapture(path, attempted_at, NoticesGap(path, f"notices refused: {error}"), _digest(raw))
    return NoticesCapture(path, attempted_at, notices, _digest(raw))
