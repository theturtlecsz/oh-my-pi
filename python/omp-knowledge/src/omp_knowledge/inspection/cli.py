from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .report import InspectionReport, build_report

VALID_SECTIONS = ("sources", "evidence", "procedures", "acceptance")


def format_cli_output(report: InspectionReport, section: str | None = None) -> str:
    """Format human-readable CLI output for a report or section."""
    sections_to_show = [section] if section else list(VALID_SECTIONS)
    lines: list[str] = []

    for sec in sections_to_show:
        lines.append(f"=== {sec.capitalize()} ===")
        val = getattr(report, sec, None)
        if val == "missing":
            lines.append("  missing")
            lines.append("")
            continue

        if sec == "sources":
            snaps = val.get("structural_snapshots") if isinstance(val, dict) else None
            units = val.get("capture_units") if isinstance(val, dict) else None
            lines.append("  Structural Snapshots:")
            if snaps == "missing":
                lines.append("    missing")
            elif isinstance(snaps, list) and snaps:
                for s in snaps:
                    lines.append(
                        f"    - repository={s.get('repository')} snapshot={s.get('snapshot')} state={s.get('state')}"
                    )
            else:
                lines.append("    (none)")

            lines.append("  Capture Units:")
            if units == "missing":
                lines.append("    missing")
            elif isinstance(units, list) and units:
                for u in units:
                    lines.append(
                        f"    - unit_id={u.get('unit_id')} state={u.get('state')} error_code={u.get('error_code')}"
                    )
            else:
                lines.append("    (none)")

        elif sec == "procedures":
            if isinstance(val, list) and val:
                for p in val:
                    lines.append(
                        f"  - [{p.get('status')}] {p.get('id')}: {p.get('title')} (v{p.get('current_version')})"
                    )
                    v_history = p.get("version_history", [])
                    if v_history:
                        lines.append(f"    Versions: {', '.join(str(v.get('version')) for v in v_history)}")
                    if p.get("supporting_receipt_ids"):
                        lines.append(f"    Supporting receipts: {p.get('supporting_receipt_ids')}")
                    if p.get("outcome_receipt_ids"):
                        lines.append(f"    Outcome receipts: {p.get('outcome_receipt_ids')}")
            else:
                lines.append("  (none)")

        elif sec == "evidence":
            if isinstance(val, list) and val:
                for ev in val:
                    lines.append(f"  Procedure {ev.get('procedure_id')}:")
                    lines.append(f"    Support receipts: {ev.get('supporting_receipt_ids', [])}")
                    lines.append(f"    Uses receipts: {ev.get('use_receipt_ids', [])}")
                    lines.append(f"    Outcome receipts: {ev.get('outcome_receipt_ids', [])}")
                    lines.append(f"    Correction receipts: {ev.get('correction_receipt_ids', [])}")
            else:
                lines.append("  (none)")

        elif sec == "acceptance":
            if isinstance(val, dict):
                props = val.get("proposals", [])
                lines.append("  Proposals:")
                if props:
                    for pr in props:
                        lines.append(
                            f"    - [{pr.get('status')}] id={pr.get('proposal_id')} reason={pr.get('reason')}"
                        )
                else:
                    lines.append("    (none)")

                cleanup = val.get("cleanup_queue", {})
                pending = cleanup.get("pending", [])
                done = cleanup.get("done", [])
                lines.append(f"  Cleanup Queue: {len(pending)} pending, {len(done)} done")
            else:
                lines.append("  (none)")

        lines.append("")

    return "\n".join(lines).strip()


def run_show(args: argparse.Namespace) -> int:
    report = build_report(args.state_root)
    data = report.to_dict()

    if args.json:
        if args.section:
            payload = data.get(args.section)
        else:
            payload = data
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(format_cli_output(report, section=args.section))

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omp_knowledge.inspection",
        description="Inspect Fleet Knowledge stores over a state root",
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Inspection command")

    show_parser = subparsers.add_parser("show", help="Show inspection report")
    show_parser.add_argument(
        "--state-root",
        required=True,
        help="Path to state root holding learning.sqlite and structural-publications.sqlite",
    )
    show_parser.add_argument(
        "--section",
        choices=VALID_SECTIONS,
        default=None,
        help="Section to inspect (sources, evidence, procedures, acceptance)",
    )
    show_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )

    # Top-level fallback arguments for direct invocation without 'show'
    parser.add_argument(
        "--state-root",
        default=None,
        help="Path to state root",
    )
    parser.add_argument(
        "--section",
        choices=VALID_SECTIONS,
        default=None,
        help="Section to inspect",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand == "show" or (args.subcommand is None and args.state_root is not None):
        return run_show(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
