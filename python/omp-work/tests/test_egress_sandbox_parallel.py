"""Parallel file runners must not trigger false-positive helper detections."""

from __future__ import annotations

from contextlib import suppress
import json
import multiprocessing as mp
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any
import xml.etree.ElementTree as ET

import pytest

import test_egress_sandbox as sandbox_tests


def _runner_main(
    runner_id: int,
    test_file: str,
    tmp_path: Path,
    ready_event: Any,
    stop_event: Any,
) -> None:
    """Execute test file repeatedly in background until stop_event is set."""
    log_path = tmp_path / f"runner_{runner_id}.log"
    with open(log_path, "a") as log_file:
        os.dup2(log_file.fileno(), 1)
        os.dup2(log_file.fileno(), 2)
        sys.stdout = log_file
        sys.stderr = log_file

        class ReadinessPlugin:
            def pytest_runtest_logstart(
                self, nodeid: str, location: tuple[str, int | None, str]
            ) -> None:
                if not ready_event.is_set():
                    ready_event.set()

        plugin = ReadinessPlugin()
        round_num = 0
        try:
            while round_num < 40 and not stop_event.is_set():
                round_num += 1
                t_start = time.monotonic()
                junit_path = tmp_path / f"junit_runner_{runner_id}_round_{round_num}.xml"
                args = [
                    test_file,
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={str(junit_path)}",
                ]
                rc = pytest.main(args, plugins=[plugin])
                t_end = time.monotonic()

                errors = 0
                failures = 0
                skipped = 0
                tests = 0
                if junit_path.exists():
                    try:
                        tree = ET.parse(junit_path)
                        root = tree.getroot()
                        for ts in root.iter("testsuite"):
                            errors += int(ts.attrib.get("errors", 0))
                            failures += int(ts.attrib.get("failures", 0))
                            skipped += int(ts.attrib.get("skipped", 0))
                            tests += int(ts.attrib.get("tests", 0))
                    except Exception:
                        errors += 1
                else:
                    errors += 1

                record = {
                    "runner_id": runner_id,
                    "round": round_num,
                    "rc": int(rc),
                    "errors": errors,
                    "failures": failures,
                    "skips": skipped,
                    "tests": tests,
                    "start": t_start,
                    "end": t_end,
                    "duration": t_end - t_start,
                    "junit_path": str(junit_path),
                }
                tmp_file = tmp_path / f".tmp_result_{runner_id}_{round_num}.json"
                result_file = tmp_path / f"result_runner_{runner_id}_round_{round_num}.json"
                tmp_file.write_text(json.dumps(record))
                tmp_file.replace(result_file)

                if rc != 0 or errors > 0 or failures > 0 or skipped > 0:
                    sys.exit(1)

                if stop_event.is_set():
                    break
        except Exception as exc:
            err_file = tmp_path / f"error_runner_{runner_id}.json"
            err_file.write_text(json.dumps({"error": str(exc)}))
            sys.exit(1)


@pytest.mark.skipif(sandbox_tests._unshare_probe_fails(), reason="unshare probe failed")
def test_twenty_scans_with_two_file_runners(tmp_path: Path) -> None:
    test_file = str(Path(sandbox_tests.__file__).resolve())
    test_start = time.monotonic()

    ctx = mp.get_context("fork")
    ready1 = ctx.Event()
    ready2 = ctx.Event()
    stop_event = ctx.Event()

    p1 = ctx.Process(target=_runner_main, args=(0, test_file, tmp_path, ready1, stop_event))
    p2 = ctx.Process(target=_runner_main, args=(1, test_file, tmp_path, ready2, stop_event))

    def _check_time() -> None:
        if (time.monotonic() - test_start) > 600.0:
            raise AssertionError("Total run exceeded 600s bound")

    def _check_failed_rounds() -> list[dict[str, Any]]:
        records = []
        for res_path in sorted(tmp_path.glob("result_runner_*_round_*.json")):
            try:
                rec = json.loads(res_path.read_text())
                records.append(rec)
                if (
                    rec.get("rc", 0) != 0
                    or rec.get("errors", 0) > 0
                    or rec.get("failures", 0) > 0
                    or rec.get("skips", 0) > 0
                ):
                    raise AssertionError(f"Runner round failed or skipped: {rec}")
            except (json.JSONDecodeError, OSError):
                pass
        for err_path in tmp_path.glob("error_runner_*.json"):
            raise AssertionError(f"Runner error reported: {err_path.read_text()}")
        return records

    scans: list[dict[str, Any]] = []
    try:
        p1.start()
        p2.start()

        # Bound readiness to 30s
        t_ready_start = time.monotonic()
        for ready, p in ((ready1, p1), (ready2, p2)):
            rem = 30.0 - (time.monotonic() - t_ready_start)
            if rem <= 0 or not ready.wait(timeout=rem):
                raise AssertionError("Runners failed to signal readiness within 30s bound")
            if not p.is_alive():
                raise AssertionError("Runner process died during readiness wait")

        for scan_idx in range(20):
            _check_time()
            if not p1.is_alive() or not p2.is_alive():
                _check_failed_rounds()
                raise AssertionError(f"Runner early exit before scan {scan_idx + 1}")
            _check_failed_rounds()

            t_scan_start = time.monotonic()
            sandbox_tests.test_no_proc_cmdline_names_egress_sandbox_helper()
            t_scan_end = time.monotonic()

            scans.append({
                "scan_index": scan_idx + 1,
                "start": t_scan_start,
                "end": t_scan_end,
                "duration": t_scan_end - t_scan_start,
            })

            _check_time()
            if not p1.is_alive() or not p2.is_alive():
                _check_failed_rounds()
                raise AssertionError(f"Runner early exit after scan {scan_idx + 1}")
            _check_failed_rounds()

        assert len(scans) == 20, f"Expected 20 scans, completed {len(scans)}"

    finally:
        # Signal stop, join with bounds, terminate/kill only started PIDs individually if needed, then reap
        stop_event.set()
        for p in (p1, p2):
            if p.is_alive():
                rem = max(1.0, 600.0 - (time.monotonic() - test_start))
                p.join(timeout=min(rem, 60.0))
            if p.is_alive():
                pid = p.pid
                p.terminate()
                p.join(timeout=5.0)
                if p.is_alive() and pid is not None:
                    with suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)
                    p.join(timeout=5.0)

    # Validate process exit statuses
    assert p1.exitcode == 0, f"Runner 0 exited with status {p1.exitcode}"
    assert p2.exitcode == 0, f"Runner 1 exited with status {p2.exitcode}"

    # Verify rounds from both runners
    all_records = _check_failed_rounds()
    runner0_records = [r for r in all_records if r["runner_id"] == 0]
    runner1_records = [r for r in all_records if r["runner_id"] == 1]
    assert len(runner0_records) >= 1, "Runner 0 did not complete at least one full-file round"
    assert len(runner1_records) >= 1, "Runner 1 did not complete at least one full-file round"

    for r in all_records:
        assert r["rc"] == 0, f"Round {r['round']} in runner {r['runner_id']} had non-zero exit code {r['rc']}"
        assert r["errors"] == 0, f"Round {r['round']} in runner {r['runner_id']} had {r['errors']} errors"
        assert r["failures"] == 0, f"Round {r['round']} in runner {r['runner_id']} had {r['failures']} failures"
        assert r["skips"] == 0, f"Round {r['round']} in runner {r['runner_id']} had {r['skips']} skips"
        assert r["tests"] > 0, f"Round {r['round']} in runner {r['runner_id']} ran 0 tests"

    # Compute overlap times
    overlap_metrics = []
    for s in scans:
        s_start = s["start"]
        s_end = s["end"]
        scan_overlaps = []
        for r in all_records:
            r_start = r["start"]
            r_end = r["end"]
            overlap = max(0.0, min(s_end, r_end) - max(s_start, r_start))
            if overlap > 0:
                scan_overlaps.append({
                    "runner_id": r["runner_id"],
                    "round": r["round"],
                    "overlap_seconds": overlap,
                })
        overlap_metrics.append({
            "scan_index": s["scan_index"],
            "overlaps": scan_overlaps,
        })

    # Save scan metrics and overlap times in tmp_path
    metrics = {
        "scan_count": len(scans),
        "scans": scans,
        "runners": {
            "0": runner0_records,
            "1": runner1_records,
        },
        "overlap_times": overlap_metrics,
    }
    (tmp_path / "scan_metrics.json").write_text(json.dumps(metrics, indent=2))
    (tmp_path / "scans.json").write_text(json.dumps(scans, indent=2))
    (tmp_path / "overlap_times.json").write_text(json.dumps(overlap_metrics, indent=2))
