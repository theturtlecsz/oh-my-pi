"""Report skipped work-ledger nodes and the check that executed them.

Reads receipts written by ``omp_work.skip_accounting``. It does not collect or
run pytest.

CLI::

    python -m omp_work.skip_accounting_report \\
        --unit FILE --alternate FILE --json FILE --markdown FILE [--check]

``--alternate`` may be repeated. Both output files are written even when the
report is not clean. ``--check`` exits 1 when a unit-skipped node is uncovered,
a check failed, or evidence is malformed, incomplete, or stale.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .skip_accounting import (
    Receipt,
    _executed_unskipped,
    _rejection,
    _skipped_phases,
    fingerprint,
    read_receipt,
    reconcile,
)

HISTORICAL_COMMIT = "5c57a261d6"
HISTORICAL_UNIT_PASSED = 4054
HISTORICAL_UNIT_SKIPPED = 5156
HISTORICAL_INTEGRATION_PASSED = 858

# Receipt check names that correspond to the historical integration total.
INTEGRATION_CHECKS = frozenset(
    {"postgres", "integration", "pg", "work-ledger integration", "work-ledger-integration"}
)

JAIL_FILES = (
    "python/omp-work/tests/test_orchestrator_verifier_jail.py",
    "python/omp-work/tests/test_research_runner.py",
    "python/omp-work/tests/test_research_m1_investigation.py",
    "python/omp-work/tests/test_orchestrator_verifier_launch.py",
    "python/omp-work/tests/test_egress_sandbox_procfs.py",
)

FLOOD_TESTS = (
    "python/omp-work/tests/test_flood_independence_gate.py",
    "python/omp-work/tests/test_flood_planner_prompt_cli.py",
    "python/omp-work/tests/test_flood_planner_rollout.py",
    "python/omp-work/tests/test_flood_rollout_provenance.py",
    "python/omp-work/tests/test_flood_start_metrics.py",
)

SUBSET_TARGETS = (
    "python/omp-work/tests/test_installed_runtime_isolation.py::test_candidate_edits_and_inherited_config_do_not_change_installed_processes",
    "python/omp-work/tests/test_installed_runtime_isolation.py::test_corrupted_disposable_release_is_refused_before_runtime_launch",
    "python/omp-work/tests/test_installed_execution_recovery.py::test_killed_persisted_turn_performs_one_real_criteria_seal",
    "python/omp-work/tests/test_installed_execution_recovery.py::test_repeated_child_kill_preserves_original_task_preparation",
    "python/omp-work/tests/test_installed_execution_recovery.py::test_committed_resume_post_response_loss_reconciles_same_operation",
)

FULL_TARGETS = (
    "python/omp-work/tests/test_installed_runtime_isolation.py",
    "python/omp-work/tests/test_installed_execution_recovery.py",
)

M1_DB_NODE = "python/omp-work/tests/test_research_m1_investigation.py"
M1_NOT_COVERED = (
    "Without OMP_WORK_POSTGRES_INTEGRATION=1 this command does not cover the M1 DB node. "
    f"It selects {M1_DB_NODE}, and that module's skipif skips every node with reason "
    "'set OMP_WORK_POSTGRES_INTEGRATION=1'."
)

EXECUTION_RULES = (
    "A unit-skipped node is covered only by an executed call with no skipped phase, in a complete receipt with the same run_id and the same fingerprint. The node must be selected and collected in that receipt.",
    "Collection-only evidence is incomplete and is rejected. It covers nothing.",
    "Subset-only evidence, where selected nodes are a proper subset of collected nodes, does not cover a node it did not select.",
    "An alternate that also skips the node does not cover it. There is no allowlist, no waiver, and no deletion option.",
    "A failed call stays a failed check. Listing the node as executed does not hide the failure.",
    "A malformed receipt, a stale fingerprint, a different run_id, or an incomplete receipt is rejected and covers nothing.",
    "Generated reports are not fingerprint inputs. skip_accounting.fingerprint ignores directories named reports or generated, files ending in .skip-receipt.json, changelogs, and the inventory. This command hashes the source before it writes --json and --markdown.",
    "--check exits 1 when any skipped node is uncovered or failing, or any evidence is rejected. Both output files are written first. Without --check the same files are written and the process exits 0.",
    "This command does not run pytest.",
)

_WAITING = {
    "doc": "docs/work-ledger-test-accounting.md",
    "applied": False,
    "waiver": False,
    "deletion": False,
    "flood_tests": list(FLOOD_TESTS),
    "needs": (
        "Chris chooses an already-patched Flood source, pins its bytes, and later activates the local gate "
        "with OMP584_FLOOD_SOURCE set to that pin. This report does not stage flood.py, does not waive skips, "
        "does not delete tests, and does not edit oh-my-pi-test.sh."
    ),
}


def repo_root(root_override: str | os.PathLike[str] | None = None) -> Path:
    """Checkout that holds package.json and the accounting plugin."""
    if root_override is not None:
        return Path(root_override).resolve()
    if os.environ.get("OMP_ACCOUNTING_ROOT"):
        return Path(os.environ["OMP_ACCOUNTING_ROOT"]).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "package.json").is_file() and (parent / "python/omp-work/src/omp_work/skip_accounting.py").is_file():
            return parent
    cwd = Path.cwd().resolve()
    for parent in (cwd, *cwd.parents):
        if (parent / "package.json").is_file() and (parent / "python/omp-work/src/omp_work/skip_accounting.py").is_file():
            return parent
    return cwd


def source_fingerprint(root: str | os.PathLike[str]) -> str:
    """Hash accounting inputs. Report files and receipts are not inputs."""
    return fingerprint(root)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _phase_rows(phases: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = []
    for name in sorted(phases):
        phase = phases[name]
        outcome = phase.outcome if hasattr(phase, "outcome") else ""
        reason = phase.reason if hasattr(phase, "reason") else str(phase)
        rows.append({"outcome": outcome, "phase": name, "reason": reason})
    return rows


def _reason_rows(check: str, phases: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"check": check, "phase": phase, "reason": reason} for phase, reason in sorted(phases.items())]


def _passed_count(receipt: Receipt) -> int:
    count = 0
    for node in receipt.selected:
        phases = receipt.nodes.get(node, {})
        call = phases.get("call")
        if call is None or call.outcome != "passed":
            continue
        if any(phase.outcome == "skipped" for phase in phases.values()):
            continue
        count += 1
    return count


def _integration_script(root: Path) -> tuple[str, list[str]]:
    path = root / "package.json"
    if not path.is_file():
        fallback = repo_root() / "package.json"
        if fallback.is_file():
            path = fallback
    try:
        script = json.loads(path.read_text(encoding="utf-8"))["scripts"]["test:py:work-ledger:integration"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return f"unread package.json script test:py:work-ledger:integration ({exc})", []
    if not isinstance(script, str):
        return "package.json script test:py:work-ledger:integration is not a string", []
    files = [part for part in script.split() if part.startswith("python/omp-work/tests/")]
    return script, files


def catalog(root: Path) -> list[dict[str, Any]]:
    """Concrete checks named by package.json, CI, the installed selector, and the external gate."""
    integration_script, integration_files = _integration_script(root)
    jail_command = (
        "uv run -q --isolated --python <uv python find --python-preference only-managed 3.13> "
        "--project python/omp-work --extra dev pytest -q -p no:cacheprovider " + " ".join(JAIL_FILES)
    )
    checks = [
        {
            "name": "work-ledger unit tests",
            "source": "package.json does not define this command. .github/workflows/ci.yml job work_ledger_postgres step 'Run Work Ledger unit tests', and oh-my-pi-test.sh label 'work-ledger unit tests', both run it.",
            "command": "uv run --project python/omp-work --extra dev pytest -n 4 python/omp-work/tests",
            "env": [],
            "files": ["python/omp-work/tests"],
            "prerequisites": [
                "OMP_WORK_POSTGRES_INTEGRATION is not set",
                "OMP_INSTALLED_RELEASE and OMP_INSTALLED_MANIFEST_SHA256 are not set",
                "OMP584_FLOOD_SOURCE is not set",
                "no -x, so a failing node does not leave the rest of the session unreported",
                "pytest-xdist, because the command passes -n 4",
            ],
            "covers": "selected nodes that do not skip under those unset variables",
            "does_not_cover": (
                "PostgreSQL-gated nodes, installed-release nodes, Flood-source nodes, and namespace nodes whose "
                "unshare probe fails. package.json test:py is a different command: it passes -x and also runs "
                "python/omp-rpc and python/robomp, so a test:py session is not this check."
            ),
            "activated": True,
        },
        {
            "name": "work-ledger integration",
            "source": "package.json script test:py:work-ledger:integration; .github/workflows/ci.yml step 'Run PostgreSQL integration'; oh-my-pi-test.sh label 'work-ledger integration'",
            "command": integration_script,
            "env": ["OMP_WORK_POSTGRES_INTEGRATION=1"],
            "files": integration_files,
            "prerequisites": [
                "PostgreSQL 18 client binaries initdb, postgres, and pg_isready on PATH",
                "CI installs postgresql-18 and postgresql-client-18 when pg_config's major version is not 18",
                "OMP_WORK_POSTGRES_INTEGRATION=1",
                "modules that also skip on unshare, including the M1 DB module, still require a successful namespace probe",
                "pytest-xdist, because the command passes -n 4",
            ],
            "covers": (
                "nodes in the allowlist whose skip conditions are satisfied. "
                f"{M1_DB_NODE} is on the allowlist and its DB nodes run when the flag is set and the unshare probe passes."
            ),
            "does_not_cover": "any node whose file is not an argument of this script. The allowlist is not the whole python/omp-work/tests tree.",
            "activated": True,
        },
        {
            "name": "jailed tests, managed Python",
            "source": "oh-my-pi-test.sh function jail_py_managed, label 'jailed tests, managed Python'",
            "command": jail_command,
            "env": ["OMP_WORK_POSTGRES_INTEGRATION is not set"],
            "files": list(JAIL_FILES),
            "prerequisites": [
                "uv-managed CPython 3.13 from: uv python find --python-preference only-managed 3.13",
                "a copy of HEAD python/omp-work extracted at <tmp>/work/oh-my-pi (git archive HEAD python/omp-work)",
                "each listed file runs only when it exists in that checkout",
                "unshare --user --map-root-user --net --mount --pid --fork true exits 0 for verifier_jail, research_runner, and a PG-enabled M1 run",
                "egress_sandbox_procfs also needs unshare, setpriv, ip, and gcc on PATH",
                "OMP_WORK_POSTGRES_INTEGRATION is not set",
            ],
            "covers": (
                "verifier_jail, research_runner, verifier_launch, and egress_sandbox_procfs when their own probes pass. "
                "The command selects those four files plus the M1 file."
            ),
            "does_not_cover": M1_NOT_COVERED,
            "activated": True,
        },
        {
            "name": "installed qualification",
            "source": "scripts/installed-qualification-mode.ts selects the targets; .github/workflows/ci.yml stages the runtime and runs pytest; oh-my-pi-test.sh label 'qualify installed isolation and controller recovery' uses the same selector",
            "command": (
                "OMP_INSTALLED_RELEASE=<staged> OMP_INSTALLED_MANIFEST_SHA256=<sha256 of manifest.json> "
                "uv run --project python/omp-work --extra dev pytest -n 3 <targets> "
                "--basetemp=<runner temp>/omp-installed-runtime-test-state"
            ),
            "env": ["OMP_INSTALLED_RELEASE", "OMP_INSTALLED_MANIFEST_SHA256"],
            "files": list(FULL_TARGETS),
            "subset_targets": list(SUBSET_TARGETS),
            "full_targets": list(FULL_TARGETS),
            "prerequisites": [
                "session-system/runtime/stage.ts with uv Python 3.13, bun, uv, and both linux-x64 native addons (baseline and modern)",
                "bubblewrap (bwrap) on PATH",
                "PostgreSQL 18 binaries, because the installed tests start a disposable server through pg_native.py",
                "pytest-xdist, because the command passes -n 3",
                "a staged manifest; the sha256 is of that manifest.json",
            ],
            "covers": (
                "In full mode, the nodes in FULL_TARGETS. In subset mode, only the five SUBSET_TARGETS node ids. "
                "CI full mode and the diff-failed fallback use FULL_TARGETS (two files). "
                "oh-my-pi-test.sh, when it does not narrow to subset, also includes "
                "python/omp-work/tests/test_installed_runtime_lifetime.py if that file exists. "
                "The selector's FULL_TARGETS does not list the lifetime file."
            ),
            "does_not_cover": (
                "nodes outside the selected targets. Missing OMP_INSTALLED_RELEASE or OMP_INSTALLED_MANIFEST_SHA256 "
                "skips with 'installed-process qualification requires OMP_INSTALLED_RELEASE and OMP_INSTALLED_MANIFEST_SHA256'. "
                "That skip is not coverage. Subset mode is subset-only evidence for every installed node it did not select."
            ),
            "mode_rules": [
                "release, schedule, or workflow_dispatch selects full",
                "a push whose before is all zeros selects full",
                "a changed path matching PROTECTED_PATHS selects full",
                "otherwise the mode is subset with reason 'no protected paths modified'",
                "an unknown event name throws and does not select a mode",
            ],
            "activated": True,
        },
        {
            "name": "flood source",
            "source": "python/omp-work/tests/test_flood_*.py; not wired into package.json, ci.yml, or oh-my-pi-test.sh by this task",
            "command": (
                "OMP584_FLOOD_SOURCE=<pinned directory> uv run --project python/omp-work --extra dev pytest "
                + " ".join(FLOOD_TESTS)
            ),
            "env": ["OMP584_FLOOD_SOURCE=<pinned already-patched directory>"],
            "files": list(FLOOD_TESTS),
            "prerequisites": [
                "OMP584_FLOOD_SOURCE names a directory whose top-level flood.py is already patched",
                "sibling .py modules and .md files that those tests copy sit beside flood.py",
                "git, so rollout tests can init a temporary repository and apply the OMP-584 patches onto a copy",
                "the raw stage infra/automation-user/omp584-flood-stage is not in this checkout, so independence_gate has nothing to run until the pin is set",
            ],
            "covers": "the five Flood test modules when that pin is set and the nodes execute",
            "does_not_cover": (
                "a missing source. There is no waiver and no deletion. "
                "test_flood_start_metrics.py falls back to ~/flood/flood.py when the variable is unset; "
                "a local-gate run must set OMP584_FLOOD_SOURCE so it does not use that mutable copy. "
                "The home fallback is not a pin."
            ),
            "activated": False,
        },
    ]
    return sorted(checks, key=lambda item: item["name"])


def _load(path: Path) -> tuple[Receipt | None, str | None, str | None]:
    try:
        return read_receipt(path), _digest(path), None
    except ValueError as exc:
        return None, None, str(exc)


def _historical() -> dict[str, Any]:
    return {
        "commit": HISTORICAL_COMMIT,
        "integration_passed": HISTORICAL_INTEGRATION_PASSED,
        "unit_passed": HISTORICAL_UNIT_PASSED,
        "unit_skipped": HISTORICAL_UNIT_SKIPPED,
    }


def _delta(observed: int | None, historical: int) -> int | None:
    if observed is None:
        return None
    return observed - historical


def _shell(root: Path, *, run_id: str | None, run_fingerprint: str | None, observed: dict[str, Any]) -> dict[str, Any]:
    source = source_fingerprint(root)
    return {
        "alternates": [],
        "checks": catalog(root),
        "counts": {
            "enabled": 0,
            "failing": 0,
            "rejected": 0,
            "uncovered": 0,
            "unit_collected": observed.get("unit_collected"),
            "unit_passed": observed.get("unit_passed"),
            "unit_selected": observed.get("unit_selected"),
            "unit_skipped": observed.get("unit_skipped"),
        },
        "deltas": {
            "integration_passed": _delta(observed.get("integration_passed"), HISTORICAL_INTEGRATION_PASSED),
            "unit_passed": _delta(observed.get("unit_passed"), HISTORICAL_UNIT_PASSED),
            "unit_skipped": _delta(observed.get("unit_skipped"), HISTORICAL_UNIT_SKIPPED),
        },
        "failures": [],
        "historical": _historical(),
        "nodes": [],
        "notes": [],
        "observed": {
            "integration_passed": observed.get("integration_passed"),
            "unit_passed": observed.get("unit_passed"),
            "unit_skipped": observed.get("unit_skipped"),
        },
        "ok": False,
        "problems": [],
        "rejected": [],
        "rules": list(EXECUTION_RULES),
        "run_fingerprint": run_fingerprint,
        "run_id": run_id,
        "source_fingerprint": source,
        "source_matches_run": run_fingerprint is not None and source == run_fingerprint,
        "version": 1,
        "waiting_for_chris": {**_WAITING, "flood_tests": list(FLOOD_TESTS)},
    }


def _skipped_ids(receipt: Receipt) -> list[str]:
    return sorted(node for node in receipt.selected if _skipped_phases(receipt.nodes.get(node, {})))


def _alternate_view(
    path: Path,
    receipt: Receipt | None,
    digest: str | None,
    error: str | None,
    unit: Receipt | None,
    source: str | None = None,
) -> dict[str, Any]:
    if receipt is None:
        return {
            "accepted": False,
            "argv": [],
            "check": "",
            "complete": False,
            "digest": None,
            "passed": None,
            "path": str(path),
            "prerequisites": [],
            "rejection": error,
            "subset_only": False,
        }
    reason = None if unit is None or not unit.complete else _rejection(receipt, unit)
    if reason is None and source is not None and receipt.fingerprint != source:
        reason = "stale fingerprint"
    if error is not None:
        reason = error
    selected = set(receipt.selected)
    collected = set(receipt.collected)
    return {
        "accepted": reason is None,
        "argv": list(receipt.argv),
        "check": receipt.check,
        "complete": receipt.complete,
        "digest": digest,
        "passed": _passed_count(receipt) if reason is None else None,
        "path": str(path),
        "prerequisites": list(receipt.prerequisites),
        "rejection": reason,
        "subset_only": receipt.complete and selected != collected,
    }


def _covering(node: str, usable: Sequence[tuple[Path, Receipt, str]]) -> tuple[Path, Receipt, str] | None:
    for path, receipt, digest in usable:
        if _executed_unskipped(receipt, node) is not None:
            return path, receipt, digest
    return None


def build_report(unit_path: Path, alternate_paths: Sequence[Path], root: Path) -> dict[str, Any]:
    """Build the JSON-ready report. Caller writes it after this returns."""
    root = Path(root)
    source = source_fingerprint(root)
    unit, unit_digest, unit_error = _load(Path(unit_path))
    loaded: list[tuple[Path, Receipt | None, str | None, str | None]] = []
    for path in alternate_paths:
        receipt, digest, error = _load(Path(path))
        loaded.append((Path(path), receipt, digest, error))

    if unit is None:
        report = _shell(root, run_id=None, run_fingerprint=None, observed={"integration_passed": None, "unit_passed": None, "unit_skipped": None})
        report["problems"] = [f"malformed: {unit_error}"]
        report["rejected"] = [{"check": "", "digest": None, "path": str(unit_path), "reason": unit_error}]
        views = []
        for path, receipt, digest, error in loaded:
            view = _alternate_view(path, receipt, digest, error, None, source=source)
            if view["rejection"] is None:
                view["accepted"] = False
                view["rejection"] = "unit receipt is malformed"
            views.append(view)
        report["alternates"] = views
        report["counts"]["rejected"] = len(report["rejected"])
        report["ok"] = False
        return _sort_report(report)

    observed_unit_passed = _passed_count(unit) if unit.complete else None
    observed_unit_skipped = len(_skipped_ids(unit)) if unit.nodes or unit.selected else 0
    if not unit.complete:
        observed_unit_passed = None
    base_observed: dict[str, Any] = {
        "integration_passed": None,
        "unit_collected": len(unit.collected),
        "unit_passed": observed_unit_passed,
        "unit_selected": len(unit.selected),
        "unit_skipped": observed_unit_skipped if unit.complete or unit.nodes else None,
    }
    report = _shell(root, run_id=unit.run_id, run_fingerprint=unit.fingerprint, observed=base_observed)
    unit_stale = unit.fingerprint != source

    rejected: list[dict[str, Any]] = []
    if not unit.complete:
        rejected.append(
            {
                "check": unit.check,
                "digest": unit_digest,
                "path": str(unit_path),
                "reason": f"incomplete (exitstatus {unit.exitstatus})",
            }
        )
    if unit_stale:
        rejected.append(
            {
                "check": unit.check,
                "digest": unit_digest,
                "path": str(unit_path),
                "reason": "stale fingerprint",
            }
        )

    usable: list[tuple[Path, Receipt, str]] = []
    views = []
    for path, receipt, digest, error in loaded:
        view = _alternate_view(path, receipt, digest, error, unit if unit.complete else None, source=source)
        if error is not None or receipt is None:
            rejected.append({"check": "", "digest": None, "path": str(path), "reason": error or "malformed"})
            views.append(view)
            continue
        if not unit.complete:
            views.append(view)
            continue
        reason = _rejection(receipt, unit)
        if reason is None and receipt.fingerprint != source:
            reason = "stale fingerprint"
        if reason is not None:
            rejected.append({"check": receipt.check, "digest": digest, "path": str(path), "reason": reason})
        elif not unit_stale:
            usable.append((path, receipt, digest or ""))
        views.append(view)
    report["alternates"] = views
    report["rejected"] = rejected

    failures: list[dict[str, str]] = []
    alternate_skips: dict[str, list[dict[str, str]]] = {}
    covered: set[str] = set()
    if unit.complete:
        result = reconcile(unit, [receipt for _, receipt, _ in usable])
        for item in result.failed:
            failures.extend({"id": item["id"], **row} for row in _reason_rows(item["check"], item["phases"]))
        for node, detail in result.pending.items():
            rows: list[dict[str, str]] = []
            for skip in detail["alternate_skips"]:
                rows.extend(_reason_rows(skip["check"], skip["phases"]))
            alternate_skips[node] = sorted(rows, key=lambda row: (row["check"], row["phase"], row["reason"]))
        covered = set(result.covered)
        for node in _skipped_ids(unit):
            for _path, receipt, _dig in usable:
                if _executed_unskipped(receipt, node) is not None:
                    continue
                failed = {name: phase.reason for name, phase in receipt.nodes.get(node, {}).items() if phase.outcome == "failed"}
                if failed:
                    failures.extend({"id": node, **row} for row in _reason_rows(receipt.check, failed))
    else:
        for node in unit.selected:
            phases = unit.nodes.get(node, {})
            failed = {name: phase.reason for name, phase in phases.items() if phase.outcome == "failed"}
            if failed:
                failures.extend({"id": node, **row} for row in _reason_rows(unit.check, failed))

    integration_ids: set[str] = set()
    saw_integration = False
    for _path, receipt, _dig in usable:
        if receipt.check not in INTEGRATION_CHECKS:
            continue
        saw_integration = True
        for node in receipt.selected:
            phases = receipt.nodes.get(node, {})
            call = phases.get("call")
            if call is None or call.outcome != "passed":
                continue
            if any(phase.outcome == "skipped" for phase in phases.values()):
                continue
            integration_ids.add(node)
    integration_passed = len(integration_ids) if saw_integration else None
    report["observed"]["integration_passed"] = integration_passed
    report["deltas"]["integration_passed"] = _delta(integration_passed, HISTORICAL_INTEGRATION_PASSED)

    nodes = []
    for node in _skipped_ids(unit):
        phases = unit.nodes.get(node, {})
        cover = _covering(node, usable) if unit.complete else None
        node_failures = [row for row in failures if row["id"] == node]
        is_covered = node in covered and cover is not None
        if node_failures:
            disposition = "failing"
        elif is_covered:
            disposition = "enabled"
        else:
            disposition = "uncovered"
        diagnostics: list[str] = []
        if not is_covered:
            diagnostics.append("no executed unskipped call covers this node")
        if unit.complete:
            for path, receipt, _dig in usable:
                if node in receipt.selected:
                    continue
                if node in receipt.collected and set(receipt.selected) != set(receipt.collected):
                    diagnostics.append(f"{receipt.check} did not select this node (subset-only evidence)")
                elif node not in receipt.collected:
                    diagnostics.append(f"{receipt.check} did not collect this node")
                else:
                    diagnostics.append(f"{receipt.check} did not select this node (subset-only evidence)")
        alternate = None
        if cover is not None:
            path, receipt, digest = cover
            proof = _executed_unskipped(receipt, node)
            assert proof is not None
            alternate = {
                "argv": list(receipt.argv),
                "call_outcome": proof["call"].outcome,
                "call_reason": proof["call"].reason,
                "check": receipt.check,
                "outcome": _phase_rows(proof),
                "prerequisites": list(receipt.prerequisites),
                "receipt_digest": digest,
            }
        nodes.append(
            {
                "alternate": alternate,
                "alternate_skips": alternate_skips.get(node, []),
                "covered": is_covered,
                "diagnostics": sorted(diagnostics),
                "disposition": disposition,
                "failures": [
                    {key: row[key] for key in ("check", "phase", "reason")}
                    for row in sorted(node_failures, key=lambda row: (row["check"], row["phase"], row["reason"]))
                ],
                "id": node,
                "skipped_phases": [
                    {"phase": name, "reason": reason} for name, reason in sorted(_skipped_phases(phases).items())
                ],
                "unit_phases": _phase_rows(phases),
            }
        )

    report["nodes"] = nodes
    report["failures"] = sorted(failures, key=lambda row: (row["id"], row["check"], row["phase"], row["reason"]))
    skipped_ids = {node["id"] for node in nodes}
    problems: list[str] = []
    if not report["source_matches_run"]:
        problems.append("stale: run fingerprint does not match source fingerprint")
    for node in nodes:
        if not node["covered"]:
            problems.append(f"uncovered: {node['id']}")
        if node["disposition"] == "failing":
            problems.append(f"failing: {node['id']}")
    for row in report["failures"]:
        if row["id"] not in skipped_ids:
            problems.append(f"failing: {row['id']}")
    for item in rejected:
        label = item["check"] or item["path"]
        problems.append(f"rejected: {label}: {item['reason']}")
    report["problems"] = sorted(set(problems))
    report["counts"]["enabled"] = sum(1 for node in nodes if node["disposition"] == "enabled")
    report["counts"]["failing"] = sum(1 for node in nodes if node["disposition"] == "failing")
    report["counts"]["uncovered"] = sum(1 for node in nodes if node["disposition"] == "uncovered")
    report["counts"]["rejected"] = len(rejected)
    report["ok"] = not report["problems"]
    return _sort_report(report)


def _sort_report(report: dict[str, Any]) -> dict[str, Any]:
    report["nodes"] = sorted(report["nodes"], key=lambda node: node["id"])
    report["rejected"] = sorted(report["rejected"], key=lambda item: (item["check"], item["path"], item["reason"]))
    report["alternates"] = sorted(report["alternates"], key=lambda item: (item["check"], item["path"]))
    report["problems"] = sorted(report["problems"])
    report["failures"] = sorted(report["failures"], key=lambda row: (row["id"], row["check"], row["phase"], row["reason"]))
    return report


def _json_line(prefix: str, value: Any) -> str:
    return f"{prefix}: {json.dumps(value, ensure_ascii=False, sort_keys=True)}"


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render the same nodes, in the same order, as the JSON report."""
    lines = ["# Work-ledger skip accounting report", "", "## Counts"]
    observed = report["observed"]
    historical = report["historical"]
    deltas = report["deltas"]
    counts = report["counts"]
    fields = [
        ("run-id", report["run_id"]),
        ("run-fingerprint", report["run_fingerprint"]),
        ("source-fingerprint", report["source_fingerprint"]),
        ("source-matches-run", report["source_matches_run"]),
        ("historical-commit", historical["commit"]),
        ("historical-unit-passed", historical["unit_passed"]),
        ("historical-unit-skipped", historical["unit_skipped"]),
        ("historical-integration-passed", historical["integration_passed"]),
        ("observed-unit-passed", observed["unit_passed"]),
        ("observed-unit-skipped", observed["unit_skipped"]),
        ("observed-integration-passed", observed["integration_passed"]),
        ("delta-unit-passed", deltas["unit_passed"]),
        ("delta-unit-skipped", deltas["unit_skipped"]),
        ("delta-integration-passed", deltas["integration_passed"]),
        ("count-unit-selected", counts["unit_selected"]),
        ("count-unit-collected", counts["unit_collected"]),
        ("count-unit-passed", counts["unit_passed"]),
        ("count-unit-skipped", counts["unit_skipped"]),
        ("count-enabled", counts["enabled"]),
        ("count-failing", counts["failing"]),
        ("count-uncovered", counts["uncovered"]),
        ("count-rejected", counts["rejected"]),
        ("ok", report["ok"]),
    ]
    lines.extend(_json_line(name, value) for name, value in fields)
    lines.extend(["", "## Notes"])
    lines.extend(_json_line("note", note) for note in report["notes"])
    lines.extend(["", "## Problems"])
    lines.extend(_json_line("problem", problem) for problem in report["problems"])
    lines.extend(["", "## Rejected"])
    for item in report["rejected"]:
        lines.append(_json_line("rejected", [item["check"], item["reason"], item["path"]]))
    lines.extend(["", "## Alternates"])
    for item in report["alternates"]:
        lines.append(
            _json_line(
                "alternate",
                {
                    "accepted": item["accepted"],
                    "argv": item["argv"],
                    "check": item["check"],
                    "digest": item["digest"],
                    "prerequisites": item["prerequisites"],
                    "rejection": item["rejection"],
                    "subset_only": item["subset_only"],
                },
            )
        )
    lines.extend(["", "## Nodes"])
    for node in report["nodes"]:
        lines.extend(["", "## Node"])
        lines.append(_json_line("id", node["id"]))
        lines.append(_json_line("disposition", node["disposition"]))
        lines.append(_json_line("covered", node["covered"]))
        for phase in node["unit_phases"]:
            lines.append(_json_line("unit-phase", [phase["phase"], phase["outcome"], phase["reason"]]))
        for phase in node["skipped_phases"]:
            lines.append(_json_line("skipped-phase", [phase["phase"], phase["reason"]]))
        alternate = node["alternate"]
        if alternate is None:
            lines.append(_json_line("alternate-check", None))
            lines.append(_json_line("alternate-argv", None))
            lines.append(_json_line("alternate-prerequisites", None))
            lines.append(_json_line("alternate-call", None))
            lines.append(_json_line("alternate-digest", None))
        else:
            lines.append(_json_line("alternate-check", alternate["check"]))
            lines.append(_json_line("alternate-argv", alternate["argv"]))
            lines.append(_json_line("alternate-prerequisites", alternate["prerequisites"]))
            lines.append(_json_line("alternate-call", [alternate["call_outcome"], alternate["call_reason"]]))
            lines.append(_json_line("alternate-digest", alternate["receipt_digest"]))
            for phase in alternate["outcome"]:
                lines.append(_json_line("alternate-outcome", [phase["phase"], phase["outcome"], phase["reason"]]))
        for skip in node["alternate_skips"]:
            lines.append(_json_line("alternate-skip", [skip["check"], skip["phase"], skip["reason"]]))
        for diagnostic in node["diagnostics"]:
            lines.append(_json_line("diagnostic", diagnostic))
        for failure in node["failures"]:
            lines.append(_json_line("failure", [failure["check"], failure["phase"], failure["reason"]]))
    lines.extend(["", "## Failures"])
    for failure in report["failures"]:
        lines.append(_json_line("failure", [failure["id"], failure["check"], failure["phase"], failure["reason"]]))
    lines.extend(["", "## Checks"])
    for check in report["checks"]:
        lines.append(_json_line("check", check))
    lines.extend(["", "## Rules"])
    lines.extend(_json_line("rule", rule) for rule in report["rules"])
    lines.extend(["", "## Waiting for Chris"])
    lines.append(_json_line("waiting-for-chris", report["waiting_for_chris"]))
    lines.append("")
    return "\n".join(lines)


def _write(path: Path, text: str) -> str | None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        return str(exc)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omp_work.skip_accounting_report")
    parser.add_argument("--unit", required=True)
    parser.add_argument("--alternate", action="append", default=[])
    parser.add_argument("--json", required=True)
    parser.add_argument("--markdown", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    json_path = Path(args.json)
    markdown_path = Path(args.markdown)
    root = repo_root(args.root)
    try:
        # Hash before either report file exists, so this run's outputs cannot enter the fingerprint.
        report = build_report(Path(args.unit), [Path(path) for path in args.alternate], root)
    except Exception as exc:
        report = _shell(
            root,
            run_id=None,
            run_fingerprint=None,
            observed={"integration_passed": None, "unit_passed": None, "unit_skipped": None},
        )
        report["problems"] = [f"malformed: {exc}"]
        report["ok"] = False
    text_json = json.dumps(report, indent=1, sort_keys=True) + "\n"
    text_markdown = render_markdown(report)
    errors = [error for error in (_write(json_path, text_json), _write(markdown_path, text_markdown)) if error]
    if errors:
        for error in errors:
            print(f"skip_accounting_report: {error}", file=sys.stderr)
        return 2
    if args.check and not report["ok"]:
        for problem in report["problems"]:
            print(f"skip_accounting_report: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
