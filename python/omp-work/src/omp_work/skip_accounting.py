"""Opt-in pytest plugin and CLI that account for skipped tests.

Load with ``-p omp_work.skip_accounting`` and set ``OMP_ACCOUNTING_DIR``,
``OMP_ACCOUNTING_RUN``, ``OMP_ACCOUNTING_CHECK`` and ``OMP_ACCOUNTING_ROOT``.
The plugin writes one JSON v1 receipt per pytest session from reports alone
(never by scanning sources): per-node setup/call/teardown outcomes with skip
reasons, canonical repo-relative node ids, and whether the session finished.

``reconcile`` decides which skipped nodes of a unit receipt are covered by
alternate receipts. A node is covered only by an executed call with no phase
skipped, in a complete receipt of the same run and fingerprint.

CLI::

    python -m omp_work.skip_accounting pending --unit FILE --alternate FILE... --files PATH...
    python -m omp_work.skip_accounting merge --out FILE RECEIPT...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

RECEIPT_VERSION = 1
RECEIPT_SUFFIX = ".skip-receipt.json"
PHASES = ("setup", "call", "teardown")
EXECUTED = frozenset({"passed", "failed"})
# Worst outcome wins when one node/phase is reported more than once.
_SEVERITY = {"passed": 0, "xfailed": 1, "skipped": 2, "failed": 3}

ENV_DIR = "OMP_ACCOUNTING_DIR"
ENV_RUN = "OMP_ACCOUNTING_RUN"
ENV_CHECK = "OMP_ACCOUNTING_CHECK"
ENV_ROOT = "OMP_ACCOUNTING_ROOT"

_FINGERPRINT_INPUTS = (
    "python/omp-work/src",
    "python/omp-work/tests",
    "python/omp-work/pyproject.toml",
    "python/omp-work/uv.lock",
    "scripts",
    "tests",
    "packages",
    ".github/workflows",
    "package.json",
    "bun.lock",
)
_EXCLUDED_DIRS = frozenset(
    {".git", "node_modules", "__pycache__", ".pytest_cache", "reports", "generated", "target", "dist"}
)
_EXCLUDED_FILES = frozenset({"CHANGELOG.md", "upstream-fork-inventory.tsv", "models.json", "rules.json"})
_EXCLUDED_SUFFIXES = (".pyc", ".node", ".so", RECEIPT_SUFFIX)


@dataclass(frozen=True)
class Phase:
    outcome: str
    reason: str = ""


@dataclass
class Receipt:
    run_id: str
    fingerprint: str
    check: str
    argv: list[str]
    prerequisites: list[str]
    selected: list[str]
    collected: list[str]
    nodes: dict[str, dict[str, Phase]]
    exitstatus: int
    complete: bool
    version: int = RECEIPT_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "fingerprint": self.fingerprint,
            "check": self.check,
            "argv": self.argv,
            "prerequisites": self.prerequisites,
            "selected": self.selected,
            "collected": self.collected,
            "nodes": {
                node: {phase: {"outcome": p.outcome, "reason": p.reason} for phase, p in phases.items()}
                for node, phases in self.nodes.items()
            },
            "exitstatus": self.exitstatus,
            "complete": self.complete,
        }


def _canonical_file(path: str) -> str:
    return os.path.normpath(path).replace(os.sep, "/")


def fingerprint(root: str | os.PathLike[str]) -> str:
    """Hash test/source inputs, package.json files, workflows and accounting code under ``root``."""
    base = Path(root).resolve()
    digest = hashlib.sha256()
    files: list[Path] = []
    for entry in _FINGERPRINT_INPUTS:
        top = base / entry
        if top.is_file():
            files.append(top)
        elif top.is_dir():
            for dirpath, dirnames, filenames in os.walk(top):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in _EXCLUDED_DIRS and not d.endswith(".egg-info")
                )
                for name in sorted(filenames):
                    if name in _EXCLUDED_FILES or name.endswith(_EXCLUDED_SUFFIXES):
                        continue
                    files.append(Path(dirpath) / name)
    for path in sorted(set(files)):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _merge_phase(old: Phase | None, new: Phase) -> Phase:
    if old is None:
        return new
    worse, other = (new, old) if _SEVERITY[new.outcome] > _SEVERITY[old.outcome] else (old, new)
    if worse.outcome == other.outcome and other.reason and other.reason not in worse.reason:
        return Phase(worse.outcome, "; ".join(r for r in (worse.reason, other.reason) if r))
    return worse


def read_receipt(path: str | os.PathLike[str]) -> Receipt:
    """Load and validate a JSON v1 receipt; raise ``ValueError`` on any malformed content."""
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: unreadable receipt: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != RECEIPT_VERSION:
        raise ValueError(f"{path}: not a v{RECEIPT_VERSION} receipt")
    try:
        nodes = {
            str(node): {
                phase: Phase(str(body["outcome"]), str(body.get("reason", "")))
                for phase, body in phases.items()
            }
            for node, phases in raw["nodes"].items()
        }
        receipt = Receipt(
            run_id=str(raw["run_id"]),
            fingerprint=str(raw["fingerprint"]),
            check=str(raw["check"]),
            argv=[str(a) for a in raw["argv"]],
            prerequisites=[str(p) for p in raw["prerequisites"]],
            selected=[str(n) for n in raw["selected"]],
            collected=[str(n) for n in raw["collected"]],
            nodes=nodes,
            exitstatus=int(raw["exitstatus"]),
            complete=raw["complete"] is True,
        )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"{path}: malformed receipt: {exc!r}") from exc
    for phases in nodes.values():
        for phase, body in phases.items():
            if phase not in PHASES or body.outcome not in _SEVERITY:
                raise ValueError(f"{path}: unknown phase/outcome {phase}/{body.outcome}")
    return receipt


def _skipped_phases(phases: dict[str, Phase]) -> dict[str, str]:
    return {name: p.reason for name, p in phases.items() if p.outcome == "skipped"}


def _failed_phases(phases: dict[str, Phase]) -> dict[str, str]:
    return {name: p.reason for name, p in phases.items() if p.outcome == "failed"}


def _rejection(receipt: Receipt, unit: Receipt) -> str | None:
    if receipt.run_id != unit.run_id:
        return f"run_id {receipt.run_id!r} != {unit.run_id!r}"
    if receipt.fingerprint != unit.fingerprint:
        return "stale fingerprint"
    if not receipt.complete:
        return f"incomplete (exitstatus {receipt.exitstatus})"
    return None


def _executed_unskipped(receipt: Receipt, node: str) -> dict[str, Phase] | None:
    """Return the node's phases when the receipt proves an executed call with no skipped phase."""
    if node not in receipt.selected or node not in receipt.collected:
        return None
    phases = receipt.nodes.get(node)
    if phases is None or "call" not in phases or phases["call"].outcome not in EXECUTED:
        return None
    return None if _skipped_phases(phases) else phases


@dataclass
class Reconciliation:
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    covered: list[str] = field(default_factory=list)
    failed: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "pending": [{"id": node, **detail} for node, detail in self.pending.items()],
            "covered": self.covered,
            "failed": self.failed,
            "rejected": self.rejected,
            "absent": self.absent,
        }


def reconcile(unit: Receipt, alternates: Sequence[Receipt]) -> Reconciliation:
    """Decide which skipped nodes of ``unit`` are covered by ``alternates``.

    Raises ``ValueError`` when ``unit`` itself is not complete evidence. Alternates that are
    stale, from another run, or incomplete are listed in ``rejected`` and never cover anything.
    There is no allowlist: a node stays pending until an alternate executed its call unskipped.
    """
    if not unit.complete:
        raise ValueError(f"unit receipt {unit.check!r} is incomplete (exitstatus {unit.exitstatus})")
    result = Reconciliation()
    usable: list[Receipt] = []
    for alt in alternates:
        reason = _rejection(alt, unit)
        if reason is None:
            usable.append(alt)
        else:
            result.rejected.append({"check": alt.check, "reason": reason})
    for node in unit.selected:
        phases = unit.nodes.get(node, {})
        failed = _failed_phases(phases)
        if failed:
            result.failed.append({"id": node, "check": unit.check, "phases": failed})
        skipped = _skipped_phases(phases)
        if not skipped:
            continue
        alternate_skips: list[dict[str, Any]] = []
        covered = False
        for alt in usable:
            proof = _executed_unskipped(alt, node)
            if proof is not None:
                covered = True
                alt_failed = _failed_phases(proof)
                if alt_failed:
                    result.failed.append({"id": node, "check": alt.check, "phases": alt_failed})
                break
            alt_skipped = _skipped_phases(alt.nodes.get(node, {}))
            if alt_skipped:
                alternate_skips.append({"check": alt.check, "phases": alt_skipped})
        if covered:
            result.covered.append(node)
        else:
            result.pending[node] = {"phases": skipped, "alternate_skips": alternate_skips}
    return result


def merge_receipts(receipts: Sequence[Receipt]) -> Receipt:
    """Consolidate same-check/run/fingerprint receipts; the worst outcome per node phase wins."""
    if not receipts:
        raise ValueError("no receipts to merge")
    first = receipts[0]
    for other in receipts[1:]:
        if (other.check, other.run_id, other.fingerprint) != (first.check, first.run_id, first.fingerprint):
            raise ValueError(
                f"cannot merge {other.check!r}/{other.run_id!r} into {first.check!r}/{first.run_id!r}: "
                "check, run_id and fingerprint must match"
            )
    nodes: dict[str, dict[str, Phase]] = {}
    for receipt in receipts:
        for node, phases in receipt.nodes.items():
            merged = nodes.setdefault(node, {})
            for name, phase in phases.items():
                merged[name] = _merge_phase(merged.get(name), phase)
    failing = [r.exitstatus for r in receipts if r.exitstatus != 0]
    return Receipt(
        run_id=first.run_id,
        fingerprint=first.fingerprint,
        check=first.check,
        argv=["merge", *(shlex.join(r.argv) for r in receipts)],
        prerequisites=sorted({p for r in receipts for p in r.prerequisites}),
        selected=sorted({n for r in receipts for n in r.selected}),
        collected=sorted({n for r in receipts for n in r.collected}),
        nodes=nodes,
        exitstatus=max(failing) if failing else 0,
        complete=all(r.complete for r in receipts),
    )


# --------------------------------------------------------------------------- pytest plugin


class _Accounting:
    def __init__(self, config: pytest.Config, out_dir: Path, run_id: str, check: str, root: Path) -> None:
        self.config = config
        self.root = root
        self.run_id = run_id
        self.check = check
        self.fingerprint = fingerprint(root)
        self.prefix = os.path.relpath(config.rootpath, root).replace(os.sep, "/")
        if self.prefix == ".." or self.prefix.startswith("../"):
            raise pytest.UsageError(f"pytest rootdir {config.rootpath} is outside {ENV_ROOT} {root}")
        self.path = out_dir / f"{check}-{run_id}-{os.getpid()}-{uuid.uuid4().hex[:8]}{RECEIPT_SUFFIX}"
        self.collected: set[str] = set()
        self.selected: set[str] = set()
        self.nodes: dict[str, dict[str, Phase]] = {}

    def canonical(self, nodeid: str) -> str:
        file_part, sep, rest = nodeid.partition("::")
        if self.prefix != ".":
            file_part = f"{self.prefix}/{file_part}"
        return _canonical_file(file_part) + sep + rest

    def write(self, exitstatus: int, complete: bool) -> None:
        argv = [str(a) for a in self.config.invocation_params.args]
        try:
            import xdist  # noqa: PLC0415 - optional prerequisite probe

            xdist_version = [f"pytest-xdist={xdist.__version__}"]
        except ImportError:
            xdist_version = []
        receipt = Receipt(
            run_id=self.run_id,
            fingerprint=self.fingerprint,
            check=self.check,
            argv=argv,
            prerequisites=sorted([f"python={platform.python_version()}", f"pytest={pytest.__version__}", *xdist_version]),
            selected=sorted(self.selected),
            collected=sorted(self.collected | self.selected),
            nodes=self.nodes,
            exitstatus=exitstatus,
            complete=complete,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(receipt.to_json(), indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    # Hooks -----------------------------------------------------------------
    @pytest.hookimpl(tryfirst=True)
    def pytest_collection_modifyitems(self, items: list[pytest.Item]) -> None:
        self.collected.update(self.canonical(i.nodeid) for i in items)

    @pytest.hookimpl(trylast=True)
    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.selected.update(self.canonical(i.nodeid) for i in session.items)
        self.write(exitstatus=-1, complete=False)

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: object, ids: list[str]) -> None:
        # Controller never collects under xdist; workers report their (already deselected) ids.
        self.selected.update(self.canonical(i) for i in ids)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in PHASES:
            return
        if report.skipped:
            wasxfail = getattr(report, "wasxfail", None)
            if wasxfail is not None:
                phase = Phase("xfailed", str(wasxfail))
            else:
                longrepr = report.longrepr
                reason = str(longrepr[2] if isinstance(longrepr, tuple) else longrepr)
                phase = Phase("skipped", reason.removeprefix("Skipped: "))
        elif report.failed:
            crash = getattr(report.longrepr, "reprcrash", None)
            phase = Phase("failed", str(getattr(crash, "message", "")))
        else:
            phase = Phase("passed")
        phases = self.nodes.setdefault(self.canonical(report.nodeid), {})
        phases[report.when] = _merge_phase(phases.get(report.when), phase)

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        status = int(exitstatus)
        done = (
            not self.config.option.collectonly
            and status in (0, 1)
            and all("teardown" in self.nodes.get(n, {}) for n in self.selected)
        )
        self.write(exitstatus=status, complete=done)


def pytest_configure(config: pytest.Config) -> None:
    out_dir = os.environ.get(ENV_DIR)
    if not out_dir or hasattr(config, "workerinput"):
        return  # opt-in; xdist workers stay silent, the controller writes
    missing = [k for k in (ENV_RUN, ENV_CHECK, ENV_ROOT) if not os.environ.get(k)]
    if missing:
        raise pytest.UsageError(f"{ENV_DIR} is set but {', '.join(missing)} missing")
    plugin = _Accounting(
        config,
        Path(out_dir),
        os.environ[ENV_RUN],
        os.environ[ENV_CHECK],
        Path(os.environ[ENV_ROOT]).resolve(),
    )
    config.pluginmanager.register(plugin, "omp-skip-accounting")


# --------------------------------------------------------------------------- CLI


def _file_matches(node: str, paths: Iterable[str]) -> bool:
    file_part = node.partition("::")[0]
    return any(file_part == p or file_part.startswith(p.rstrip("/") + "/") for p in paths)


def _cmd_pending(args: argparse.Namespace) -> int:
    unit = read_receipt(args.unit)
    result = reconcile(unit, [read_receipt(p) for p in args.alternate])
    paths = [_canonical_file(p) for p in args.files]
    if paths:
        result.absent = [p for p in paths if not any(_file_matches(n, [p]) for n in unit.selected)]
        result.pending = {n: d for n, d in result.pending.items() if _file_matches(n, paths)}
        result.covered = [n for n in result.covered if _file_matches(n, paths)]
        result.failed = [f for f in result.failed if _file_matches(f["id"], paths)]
    json.dump(result.to_json(), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    merged = merge_receipts([read_receipt(p) for p in args.receipts])
    Path(args.out).write_text(json.dumps(merged.to_json(), indent=1, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omp_work.skip_accounting")
    sub = parser.add_subparsers(dest="command", required=True)
    pending = sub.add_parser("pending", help="emit skipped node ids no alternate covered, as JSON")
    pending.add_argument("--unit", required=True)
    pending.add_argument("--alternate", action="append", default=[])
    pending.add_argument("--files", nargs="+", default=[])
    pending.set_defaults(func=_cmd_pending)
    merge = sub.add_parser("merge", help="consolidate same-check/run receipts")
    merge.add_argument("--out", required=True)
    merge.add_argument("receipts", nargs="+")
    merge.set_defaults(func=_cmd_merge)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"skip_accounting: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
