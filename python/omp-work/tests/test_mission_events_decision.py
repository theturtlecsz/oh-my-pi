"""OMP-415: decision 0018 records the mission-event rules and the id formula."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from omp_work.mission_events import derive_mission_events

ROOT = Path(__file__).parents[1]
DECISION = ROOT / "src/omp_work/contracts/v1/decisions/0018-mission-events.md"
MANIFEST = ROOT / "src/omp_work/contracts/v1/manifest.json"

# Every quoted or backticked string in the decision's [D] bullets, plus the
# trigger names and the default budget fraction.
_SNIPPETS = (
    "omp_audit.domain_events",
    "stage_change",
    "operation_completed",
    "<config_dir>/mission-events.json",
    'uuid5(uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events"), f"{source_event_id}:{type}")',
    '{"kind": "evidence", "ref": <string>}',
    "work.execute",
    "work.read",
    "work.events.admin",
    "omp-work events push",
    "event-push",
    "ops.alarm",
    "ops.digest",
    "digest:{ws}:{day}",
    "omp-work alarms init|run|digest",
    "OMP_GROKBOT_ALERT_*",
    "<config_dir>/push-destinations.json",
    "allowed_hosts",
    "egress_policy.blocked_address",
    'X-OMP-Signature: v1=<hex HMAC-SHA256(key, idempotency_key+"\\n"+body)>',
    "<config_dir>/push-signing.key",
    '"omp-push-subscription\\0"',
    '"{event_id}:{kind}"',
    "OWNER_SCOPES",
    "0.8",
    "mission.started",
    "mission.blocked",
    "mission.completed",
    "mission.failed",
    "mission.progressed",
    "decision.required",
    "budget.threshold_reached",
    "important_finding",
    "abandoned",
    "actor_kind automation",
)


def test_0018_is_in_the_manifest_and_records_the_decision_strings() -> None:
    manifest = json.loads(MANIFEST.read_text())
    paths = manifest["paths"]
    assert paths == sorted(set(paths))
    assert "decisions/0018-mission-events.md" in paths
    text = DECISION.read_text()
    missing = [snippet for snippet in _SNIPPETS if snippet not in text]
    assert missing == []


def test_id_formula_equals_a_derived_mission_event_id() -> None:
    text = DECISION.read_text()
    formula = (
        'uuid5(uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events"), '
        'f"{source_event_id}:{type}")'
    )
    assert formula in text
    source_event_id = str(uuid4())
    mission_id = uuid4()
    event = {
        "event_id": source_event_id,
        "sequence": 4,
        "outcome": "applied",
        "event_type": "set_mission_status",
        "occurred_at": "2026-09-30T00:00:00+00:00",
        "payload": {
            "mission": {
                "mission_id": str(mission_id),
                "transitions": [{"from_status": "approved", "to_status": "running"}],
            }
        },
    }
    rows = derive_mission_events([event], mission_for_work=lambda _work, _sequence: None)
    assert len(rows) == 1
    row = rows[0]
    expected = str(
        uuid5(
            uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events"),
            f"{row['source_event_id']}:{row['type']}",
        )
    )
    assert row["mission_event_id"] == expected
    assert row["source_event_id"] == source_event_id
    assert row["type"] == "mission.started"
