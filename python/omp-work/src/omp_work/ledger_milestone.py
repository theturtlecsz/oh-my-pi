"""Maintain milestone_total_tokens on a post-program usage ledger JSON."""
from __future__ import annotations
from pathlib import Path
from typing import Any
import json


def sum_event_tokens(events: list[Any]) -> int:
    total = 0
    for ev in events:
        if not isinstance(ev, dict):
            continue
        total += int(ev.get("input_tokens") or 0)
        total += int(ev.get("output_tokens") or 0)
        total += int(ev.get("tokens") or 0)
    return total


def sync_milestone_total(ledger_path: Path | str) -> dict[str, Any]:
    path = Path(ledger_path)
    if not path.is_file():
        data: dict[str, Any] = {"events": [], "milestone_total_tokens": 0}
    else:
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            data = {"events": raw, "milestone_total_tokens": 0}
        elif isinstance(raw, dict):
            data = raw
        else:
            data = {"events": [], "milestone_total_tokens": 0}
    events = data.get("events")
    if not isinstance(events, list):
        for key in ("entries", "records", "usage", "steps"):
            if isinstance(data.get(key), list):
                events = data[key]
                data["events"] = events
                break
        else:
            events = []
            data["events"] = events
    total = sum_event_tokens(events)
    # also count top-level if present and events empty
    if total == 0 and "milestone_total_tokens" in data:
        total = int(data.get("milestone_total_tokens") or 0)
    data["milestone_total_tokens"] = total
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return {"path": str(path), "milestone_total_tokens": total, "event_count": len(events)}
