"""OMP-615-s09-s03: lane fetchers read bytes once and never invent evidence.

Failure mode defended: the daily check reads each in-use lane through the tool
that already owns that provider (the local ``omp`` CLI, or a captured listing
file) and hands the bytes to the frozen parsers. If a fetcher ran a different
command, retried a lane, treated a fallback (``bundled``/``cache``/``models.dev``)
or a legacy capture as a live listing, or let its clock stand in for a retrieval
time, the check would report a comparison against evidence that was never
proven. These tests drive a real stub executable to pin the argv, and each
fallback/error branch to prove it becomes a ``SourceGap`` with a bounded cause
and the digest of whatever bytes were produced.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess

import pytest

from omp_work.routing.offerings_inputs import Listing, Notices, SourceGap, parse_sources
from omp_work.routing.offerings_sources import (
    CLI_TIMEOUT_SECONDS,
    LaneCapture,
    NoticesCapture,
    NoticesGap,
    fetch_all,
    fetch_cli,
    fetch_file,
    load_notices,
)

ATTEMPT = datetime(2026, 10, 15, 6, 30, tzinfo=timezone.utc)
LISTING_TIME_MS = 1760400000000
FILE_TIME = "2026-10-14T00:00:00+00:00"

SOURCES = {
    "version": "offerings-sources.v1",
    "notices": "unused-by-lane-fetchers.json",
    "lanes": {
        "gemini_flash": {"kind": "omp-cli", "catalog_provider": "gemini_flash"},
        "kimi": {
            "kind": "listing-file",
            "catalog_provider": "kimi",
            "path": "kimi-listing.json",
        },
    },
}

CLI_LISTING = {
    "models": [
        {
            "provider": "gemini_flash",
            "id": "gemini-3.8-flash-high",
            "name": "Gemini 3.8 Flash High",
            "contextWindow": 1000000,
            "maxTokens": 65536,
            "reasoning": True,
            "thinking": ["low", "high"],
            "input": ["text", "image"],
            "cost": {"input": 0.3, "output": 1.2},
        }
    ],
    "discovery": {
        "gemini_flash": {
            "provider": "gemini_flash",
            "status": "ok",
            "stale": False,
            "source": "provider",
            "fetchedAt": LISTING_TIME_MS,
        }
    },
}

FILE_LISTING = {
    "models": [
        {
            "provider": "kimi",
            "id": "kimi-code/k3-256k",
            "name": "Kimi K3 256k",
            "contextWindow": 256000,
            "maxTokens": 32768,
            "reasoning": True,
            "thinking": None,
            "input": ["text"],
            "cost": {"input": 0, "output": 0},
        }
    ],
    "retrieval": {
        "provider": "kimi",
        "source": "https://platform.kimi.ai/models",
        "retrieved_at": FILE_TIME,
        "success": True,
    },
}

NOTICES = {
    "version": "offerings-notices.v1",
    "providers": {
        "gemini_flash": {
            "notices": {
                "coverage": "checked",
                "source": "https://ai.google.dev/gemini-api/docs/deprecations",
                "retrieved_at": FILE_TIME,
                "items": [],
            }
        }
    },
}

STUB = """\
#!/usr/bin/env python3
import json, os, pathlib, sys

pathlib.Path(os.environ["STUB_ARGV"]).write_text(json.dumps(sys.argv))
out = os.environ.get("STUB_STDOUT")
if out is not None:
    sys.stdout.write(out)
sys.stderr.write(os.environ.get("STUB_STDERR", ""))
raise SystemExit(int(os.environ.get("STUB_EXIT", "0")))
"""


def lane(name: str):
    return parse_sources(json.dumps(SOURCES)).lanes[name]


def encoded(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fixed_clock() -> datetime:
    return ATTEMPT


def completed(
    argv: list[str], returncode: int = 0, stdout: bytes | None = None, stderr: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def recording_run(proc: subprocess.CompletedProcess[bytes], calls: list[list[str]]):
    def run(argv, **kwargs):
        calls.append(list(argv))
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["capture_output"] is True
        return proc

    return run


# --- the command itself -----------------------------------------------------


def test_fetch_cli_runs_the_exact_command_through_a_real_stub_executable(tmp_path: Path, monkeypatch):
    # A real executable, launched by the real subprocess.run: proves the argv a
    # live CLI would actually receive, not just the argv a mock was handed.
    stub = tmp_path / "omp-stub"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    argv_path = tmp_path / "argv.json"
    monkeypatch.setenv("STUB_ARGV", str(argv_path))
    monkeypatch.setenv("STUB_STDOUT", json.dumps(CLI_LISTING))

    capture = fetch_cli(lane("gemini_flash"), omp=str(stub), clock=fixed_clock)

    assert capture.listing() is not None
    assert json.loads(argv_path.read_text()) == [
        str(stub),
        "models",
        "refresh",
        "gemini_flash",
        "--json",
        "--no-extensions",
    ]


def test_fetch_cli_rejects_a_non_cli_lane():
    with pytest.raises(ValueError):
        fetch_cli(lane("kimi"))


# --- trusted captures keep the bytes' evidence, not the clock's -------------


def test_cli_capture_keeps_digest_and_byte_provider_time_over_attempt_time():
    raw = encoded(CLI_LISTING)
    calls: list[list[str]] = []
    capture = fetch_cli(
        lane("gemini_flash"), run=recording_run(completed([], stdout=raw), calls), clock=fixed_clock
    )

    listing = capture.listing()
    assert isinstance(listing, Listing)
    assert listing.provenance is not None
    assert listing.provenance["source"] == "provider"
    assert listing.provenance["fetchedAt"] == LISTING_TIME_MS
    assert capture.sha256 == digest(raw)
    # The clock labels the attempt; it never becomes the retrieval time.
    assert capture.attempted_at == ATTEMPT
    assert listing.provenance["fetchedAt"] != int(ATTEMPT.timestamp() * 1000)
    assert calls == [["omp", "models", "refresh", "gemini_flash", "--json", "--no-extensions"]]


def test_file_capture_reads_once_and_keeps_file_retrieval_provenance(tmp_path: Path):
    raw = encoded(FILE_LISTING)
    reads: list[str] = []

    def read_bytes(path: str) -> bytes:
        reads.append(path)
        return raw

    capture = fetch_file(lane("kimi"), read_bytes=read_bytes, clock=fixed_clock)

    listing = capture.listing()
    assert isinstance(listing, Listing)
    assert listing.provenance is not None
    assert listing.provenance["source"] == "https://platform.kimi.ai/models"
    assert listing.provenance["retrieved_at"] == datetime.fromisoformat(FILE_TIME)
    assert capture.sha256 == digest(raw)
    assert capture.attempted_at == ATTEMPT
    assert reads == ["kimi-listing.json"]


def test_file_capture_reads_the_configured_path(tmp_path: Path):
    listing_file = tmp_path / "kimi.json"
    listing_file.write_bytes(encoded(FILE_LISTING))
    lane_file = parse_sources(
        json.dumps(
            {
                "version": "offerings-sources.v1",
                "notices": "unused.json",
                "lanes": {"kimi": {"kind": "listing-file", "catalog_provider": "kimi", "path": str(listing_file)}},
            }
        )
    ).lanes["kimi"]

    capture = fetch_file(lane_file, clock=fixed_clock)

    assert capture.listing() is not None
    assert capture.sha256 == digest(listing_file.read_bytes())


def test_fetch_file_rejects_a_non_file_lane():
    with pytest.raises(ValueError):
        fetch_file(lane("gemini_flash"))


# --- legacy, fallback and unproven captures are gaps ------------------------


def test_legacy_json_without_a_discovery_block_is_a_gap():
    raw = encoded({"models": CLI_LISTING["models"]})
    capture = fetch_cli(lane("gemini_flash"), run=recording_run(completed([], stdout=raw), []), clock=fixed_clock)

    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert gap.provider == "gemini_flash"
    assert capture.sha256 == digest(raw)
    assert capture.listing() is None


@pytest.mark.parametrize("source", ["bundled", "cache", "models.dev"])
def test_fallback_discovery_source_is_a_gap(source):
    payload = json.loads(json.dumps(CLI_LISTING))
    payload["discovery"]["gemini_flash"]["source"] = source
    raw = encoded(payload)
    capture = fetch_cli(lane("gemini_flash"), run=recording_run(completed([], stdout=raw), []), clock=fixed_clock)

    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert source in gap.reason or "source" in gap.reason
    assert capture.sha256 == digest(raw)


def test_configured_discovery_without_a_source_is_a_gap():
    # The configured-discovery path records status/fetchedAt but no `source`;
    # absent must be an evidence gap, never assumed to be provider-sourced.
    payload = json.loads(json.dumps(CLI_LISTING))
    del payload["discovery"]["gemini_flash"]["source"]
    capture = fetch_cli(
        lane("gemini_flash"), run=recording_run(completed([], stdout=encoded(payload)), []), clock=fixed_clock
    )
    assert capture.gap() is not None


@pytest.mark.parametrize(
    ("mutate", "label"),
    [
        (lambda proof: proof.__setitem__("provider", "kimi"), "wrong provider"),
        (lambda proof: proof.__setitem__("stale", True), "stale"),
        (lambda proof: proof.__setitem__("status", "unavailable"), "unavailable status"),
        (lambda proof: proof.__setitem__("source", "cache"), "cache source"),
        (lambda proof: proof.pop("fetchedAt"), "missing fetchedAt"),
        (lambda proof: proof.__setitem__("fetchedAt", -1), "negative fetchedAt"),
    ],
)
def test_untrusted_cli_proofs_become_gaps(mutate, label):
    payload = json.loads(json.dumps(CLI_LISTING))
    mutate(payload["discovery"]["gemini_flash"])
    raw = encoded(payload)
    capture = fetch_cli(lane("gemini_flash"), run=recording_run(completed([], stdout=raw), []), clock=fixed_clock)

    gap = capture.gap()
    assert isinstance(gap, SourceGap), label
    assert capture.sha256 == digest(raw)


def test_cli_listing_with_no_rows_is_a_gap():
    payload = json.loads(json.dumps(CLI_LISTING))
    payload["models"] = []
    capture = fetch_cli(
        lane("gemini_flash"),
        run=recording_run(completed([], stdout=encoded(payload)), []),
        clock=fixed_clock,
    )
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "empty" in gap.reason


# --- error paths carry a cause and, when present, a digest ------------------


def test_empty_output_is_a_gap_with_no_listing():
    for stdout in (b"", b"   \n"):
        capture = fetch_cli(
            lane("gemini_flash"), run=recording_run(completed([], stdout=stdout), []), clock=fixed_clock
        )
        gap = capture.gap()
        assert isinstance(gap, SourceGap)
        assert "no output" in gap.reason


def test_nonzero_exit_is_a_gap_carrying_the_digest_of_stdout():
    raw = encoded(CLI_LISTING)
    capture = fetch_cli(
        lane("gemini_flash"),
        run=recording_run(completed([], returncode=2, stdout=raw, stderr=b"boom"), []),
        clock=fixed_clock,
    )
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "exited 2" in gap.reason and "boom" in gap.reason
    assert capture.sha256 == digest(raw)


def test_nonzero_exit_without_stdout_has_no_digest():
    capture = fetch_cli(
        lane("gemini_flash"),
        run=recording_run(completed([], returncode=1, stderr=b"refused"), []),
        clock=fixed_clock,
    )
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert capture.sha256 is None


def test_timeout_is_a_gap_with_the_digest_of_partial_output():
    def run(argv, **kwargs):
        assert kwargs["timeout"] == CLI_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"], output=b"partial")

    capture = fetch_cli(lane("gemini_flash"), run=run, clock=fixed_clock)
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "timed out" in gap.reason
    assert capture.sha256 == digest(b"partial")


def test_launch_failure_is_a_gap_with_no_digest():
    def run(argv, **kwargs):
        raise FileNotFoundError("omp: not found")

    capture = fetch_cli(lane("gemini_flash"), run=run, clock=fixed_clock)
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "could not start" in gap.reason
    assert capture.sha256 is None


def test_invalid_json_is_a_gap_carrying_the_digest():
    raw = b"<html>rate limited</html>"
    capture = fetch_cli(lane("gemini_flash"), run=recording_run(completed([], stdout=raw), []), clock=fixed_clock)
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "not valid JSON" in gap.reason
    assert capture.sha256 == digest(raw)


def test_foreign_provider_rows_are_a_gap():
    payload = json.loads(json.dumps(CLI_LISTING))
    payload["models"][0]["provider"] = "kimi"
    capture = fetch_cli(
        lane("gemini_flash"), run=recording_run(completed([], stdout=encoded(payload)), []), clock=fixed_clock
    )
    gap = capture.gap()
    assert isinstance(gap, SourceGap)
    assert "provider" in gap.reason


def test_unreadable_or_empty_listing_file_is_a_gap(tmp_path: Path):
    def missing(path: str) -> bytes:
        raise FileNotFoundError(path)

    missing_capture = fetch_file(lane("kimi"), read_bytes=missing, clock=fixed_clock)
    missing_gap = missing_capture.gap()
    assert isinstance(missing_gap, SourceGap)
    assert "unreadable" in missing_gap.reason
    assert missing_capture.sha256 is None

    empty_capture = fetch_file(lane("kimi"), read_bytes=lambda path: b"\n", clock=fixed_clock)
    empty_gap = empty_capture.gap()
    assert isinstance(empty_gap, SourceGap)
    assert "empty" in empty_gap.reason
    assert empty_capture.sha256 == digest(b"\n")


def test_attempt_clock_must_label_a_utc_instant():
    naive = datetime(2026, 10, 15, 6, 30)
    with pytest.raises(ValueError):
        fetch_cli(lane("gemini_flash"), run=recording_run(completed([], stdout=b"{}"), []), clock=lambda: naive)
    with pytest.raises(ValueError):
        fetch_file(lane("kimi"), read_bytes=lambda path: b"{}", clock=lambda: ATTEMPT.astimezone(timezone(timedelta(hours=2))))


# --- fetch_all reads each configured lane exactly once ---------------------


def test_fetch_all_reads_each_lane_once_and_keys_by_lane_name():
    runs: list[list[str]] = []
    reads: list[str] = []
    cli_raw = encoded(CLI_LISTING)
    file_raw = encoded(FILE_LISTING)

    captures = fetch_all(
        parse_sources(json.dumps(SOURCES)),
        run=recording_run(completed([], stdout=cli_raw), runs),
        read_bytes=lambda path: (reads.append(path), file_raw)[1],
        clock=fixed_clock,
    )

    assert set(captures) == {"gemini_flash", "kimi"}
    assert isinstance(captures["gemini_flash"], LaneCapture)
    assert captures["gemini_flash"].listing() is not None
    assert captures["kimi"].listing() is not None
    assert runs == [["omp", "models", "refresh", "gemini_flash", "--json", "--no-extensions"]]
    assert reads == ["kimi-listing.json"]


def test_fetch_all_keeps_each_lane_gap_independent():
    runs: list[list[str]] = []
    cli_raw = encoded({"models": []})
    captures = fetch_all(
        parse_sources(json.dumps(SOURCES)),
        run=recording_run(completed([], stdout=cli_raw), runs),
        read_bytes=lambda path: encoded(FILE_LISTING),
        clock=fixed_clock,
    )
    assert captures["gemini_flash"].gap() is not None
    assert captures["kimi"].listing() is not None


# --- notices capture retains per-dimension provenance ----------------------


def test_load_notices_parses_and_retains_dimension_provenance():
    raw = encoded(NOTICES)
    reads: list[str] = []

    def read_bytes(path: str) -> bytes:
        reads.append(path)
        return raw

    capture = load_notices(parse_sources(json.dumps(SOURCES)), read_bytes=read_bytes, clock=fixed_clock)

    notices = capture.notices()
    assert isinstance(notices, Notices)
    dimension = notices.providers["gemini_flash"].notices
    assert dimension is not None
    assert dimension.source == "https://ai.google.dev/gemini-api/docs/deprecations"
    assert dimension.retrieved_at == datetime.fromisoformat(FILE_TIME)
    assert dimension.coverage == "checked" and dimension.items == ()
    assert capture.sha256 == digest(raw)
    assert capture.attempted_at == ATTEMPT
    assert reads == ["unused-by-lane-fetchers.json"]


def test_load_notices_missing_or_malformed_is_a_gap():
    def missing(path: str) -> bytes:
        raise FileNotFoundError(path)

    unreadable = load_notices(parse_sources(json.dumps(SOURCES)), read_bytes=missing, clock=fixed_clock)
    assert isinstance(unreadable.value, NoticesGap)
    assert unreadable.sha256 is None

    malformed = load_notices(
        parse_sources(json.dumps(SOURCES)), read_bytes=lambda path: b'{"version": "nope"}', clock=fixed_clock
    )
    assert isinstance(malformed.value, NoticesGap)
    assert "refused" in malformed.value.reason
    assert malformed.sha256 == digest(b'{"version": "nope"}')
    assert malformed.notices() is None


def test_notices_gap_is_reported_at_the_capture_path_not_a_lane():
    capture: NoticesCapture = load_notices(
        parse_sources(json.dumps(SOURCES)), read_bytes=lambda path: b"not json", clock=fixed_clock
    )
    assert isinstance(capture.value, NoticesGap)
    assert capture.value.path == "unused-by-lane-fetchers.json"
    assert capture.notices() is None
