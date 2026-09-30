"""Per-job probe sleep encoded in the job id (OMP-475-s01). No postgres."""

from __future__ import annotations

import math
import re
import time
from uuid import uuid4

import pytest

from omp_work.jobs.probe import (
    ProbeHandler,
    probe_job_id,
    probe_sleep_seconds,
)

_PLAIN_PROBE = re.compile(
    r"^probe-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_run_and_observe_sleep_encoded_duration() -> None:
    handler = ProbeHandler(sleep_seconds=0)
    job_id = probe_job_id(0.3)
    assert job_id.startswith("probe-sleep300-")
    assert probe_sleep_seconds(job_id) == 0.3
    job = {"job_id": job_id}

    started = time.monotonic()
    ran = handler.run(None, job)
    assert time.monotonic() - started >= 0.3

    started = time.monotonic()
    observed = handler.observe(None, job)
    assert time.monotonic() - started >= 0.3

    assert ran.outcome == "succeeded"
    assert observed is not None
    assert observed.outcome == "succeeded"


def test_plain_and_malformed_ids_fall_back_to_configured_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)
    handler = ProbeHandler(sleep_seconds=0.2)
    ids = (probe_job_id(0), f"probe-sleepX-{uuid4()}")

    for job_id in ids:
        assert probe_sleep_seconds(job_id) is None
        slept.clear()
        ran = handler.run(None, {"job_id": job_id})
        assert ran.outcome == "succeeded"
        assert slept == [0.2]

        slept.clear()
        observed = handler.observe(None, {"job_id": job_id})
        assert observed is not None
        assert observed.outcome == "succeeded"
        assert slept == [0.2]


@pytest.mark.parametrize("bad", [-1, math.nan, math.inf, -math.inf])
def test_probe_job_id_rejects_non_finite(bad: float) -> None:
    with pytest.raises(ValueError):
        probe_job_id(bad)


def test_probe_job_id_zero_has_no_sleep_part() -> None:
    job_id = probe_job_id(0)
    assert _PLAIN_PROBE.fullmatch(job_id)
    assert "sleep" not in job_id
    assert probe_sleep_seconds(job_id) is None
    assert "sleep" not in probe_job_id()
