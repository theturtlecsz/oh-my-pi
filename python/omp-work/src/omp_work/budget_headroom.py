"""Compute soft/hard headroom from BUDGET-CAPS + post-program ledger spends."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class Headroom:
    soft_warn_fraction: float
    hard_refuse_fraction: float
    milestone_soft_tokens: int | None
    milestone_hard_tokens: int | None
    spent_tokens: int
    soft_remaining: int | None
    hard_remaining: int | None
    soft_fraction_used: float | None
    hard_fraction_used: float | None
    soft_warn: bool
    hard_refuse: bool
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "soft_warn_fraction": self.soft_warn_fraction,
            "hard_refuse_fraction": self.hard_refuse_fraction,
            "milestone_soft_tokens": self.milestone_soft_tokens,
            "milestone_hard_tokens": self.milestone_hard_tokens,
            "spent_tokens": self.spent_tokens,
            "soft_remaining": self.soft_remaining,
            "hard_remaining": self.hard_remaining,
            "soft_fraction_used": self.soft_fraction_used,
            "hard_fraction_used": self.hard_fraction_used,
            "soft_warn": self.soft_warn,
            "hard_refuse": self.hard_refuse,
            "notes": list(self.notes),
        }


def _sum_ledger_tokens(ledger_path: Path) -> tuple[int, list[str]]:
    notes: list[str] = []
    if not ledger_path.is_file():
        return 0, ["ledger missing"]
    data = json.loads(ledger_path.read_text())
    events: list[Any]
    if isinstance(data, list):
        events = data
    else:
        events = []
        for key in ("events", "entries", "records", "usage", "steps"):
            if isinstance(data.get(key), list):
                events = data[key]
                break
        else:
            notes.append("ledger shape unrecognized; spent=0")
            return 0, notes
    total = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        total += int(ev.get("input_tokens") or 0) + int(ev.get("output_tokens") or 0)
        total += int(ev.get("tokens") or 0)
    return total, notes


def compute_headroom(
    *,
    caps_path: Path | str,
    ledger_path: Path | str | None = None,
) -> Headroom:
    caps = json.loads(Path(caps_path).read_text())
    soft_f = float(caps.get("soft_warn_fraction", 0.7))
    hard_f = float(caps.get("hard_refuse_fraction", 1.0))
    ms = caps.get("post_program_milestone_soft_tokens")
    mh = caps.get("post_program_milestone_hard_tokens")
    ms_i = int(ms) if ms is not None else None
    mh_i = int(mh) if mh is not None else None
    notes: list[str] = []
    spent = 0
    if ledger_path is None:
        ledger_path = caps.get("post_program_ledger")
    if ledger_path:
        spent, extra = _sum_ledger_tokens(Path(ledger_path))
        notes.extend(extra)
    else:
        notes.append("no ledger path")

    soft_rem = None
    hard_rem = None
    soft_used = None
    hard_used = None
    soft_warn = False
    hard_refuse = False
    if ms_i is not None and ms_i > 0:
        soft_used = spent / ms_i
        soft_rem = max(0, ms_i - spent)
        soft_warn = soft_used >= soft_f
    else:
        notes.append("milestone soft UNKNOWN")
    if mh_i is not None and mh_i > 0:
        hard_used = spent / mh_i
        hard_rem = max(0, mh_i - spent)
        hard_refuse = hard_used >= hard_f
    else:
        notes.append("milestone hard UNKNOWN")
    return Headroom(
        soft_warn_fraction=soft_f,
        hard_refuse_fraction=hard_f,
        milestone_soft_tokens=ms_i,
        milestone_hard_tokens=mh_i,
        spent_tokens=spent,
        soft_remaining=soft_rem,
        hard_remaining=hard_rem,
        soft_fraction_used=soft_used,
        hard_fraction_used=hard_used,
        soft_warn=soft_warn,
        hard_refuse=hard_refuse,
        notes=notes,
    )
