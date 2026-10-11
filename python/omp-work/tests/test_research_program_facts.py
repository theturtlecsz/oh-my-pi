from copy import deepcopy

import pytest

from omp_work.program_facts import append_program_fact, read_program_facts
from omp_work.v1.canonical import canonical_json


@pytest.fixture
def campaign():
    return {
        "schema": "cpk5/v1",
        "sourceIdentity": "research-program",
        "sourceVersion": "research-program.v1",
        "sequence": 1,
        "factType": "research.campaign",
        "timestamp": "2026-10-02T00:00:00Z",
        "payload": {
            "campaignId": "camp-1",
            "status": "running",
            "objective": "holdout",
            "domain": "machine_learning",
            "revisionId": "rev-1",
            "activeTrials": 1,
            "completedTrials": 0,
        },
    }


def test_example_round_trips_as_canonical_jsonl(tmp_path, campaign):
    path = tmp_path / "facts.jsonl"
    append_program_fact(path, campaign)
    assert read_program_facts(path) == [campaign]
    assert path.read_text() == canonical_json(campaign) + "\n"


def test_repeated_sequence_rejected_without_appending(tmp_path, campaign):
    path = tmp_path / "facts.jsonl"
    append_program_fact(path, campaign)
    changed = deepcopy(campaign)
    changed["payload"]["status"] = "done"
    for fact in (campaign, changed):
        with pytest.raises(ValueError, match="^duplicate_sequence$"):
            append_program_fact(path, fact)
    assert read_program_facts(path) == [campaign]


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("schema", "cpk4/v1", "invalid_schema"),
        ("sourceIdentity", "other", "invalid_schema"),
        ("sourceVersion", "research-program.v2", "invalid_schema"),
        ("sequence", 0, "invalid_schema"),
        ("sequence", True, "invalid_schema"),
        ("timestamp", "2026-02-30T00:00:00Z", "invalid_schema"),
        ("payload", [], "invalid_schema"),
        ("factType", "research.unknown", "unknown_fact"),
    ],
)
def test_invalid_envelope_does_not_create_log(tmp_path, campaign, field, value, code):
    path = tmp_path / "facts.jsonl"
    campaign[field] = value
    with pytest.raises(ValueError, match=f"^{code}$"):
        append_program_fact(path, campaign)
    assert not path.exists()


@pytest.mark.parametrize(
    "fact_type,payload,bad_field,bad_value",
    [
        (
            "research.campaign",
            {
                "campaignId": "c",
                "status": "running",
                "objective": "q",
                "domain": "ml",
                "revisionId": "r",
                "activeTrials": 1,
                "completedTrials": 0,
            },
            "activeTrials",
            True,
        ),
        (
            "research.graph",
            {
                "nodes": [{"id": "n", "parentId": None, "metric": None, "cost": 2}],
                "edges": [{"from": "n", "to": "m"}],
            },
            "nodes",
            [{"id": "n", "parentId": None, "metric": "bad", "cost": 2}],
        ),
        (
            "research.evidence",
            {
                "claimId": "c",
                "status": "supported",
                "sourceId": "s",
                "passage": "finding",
            },
            "status",
            "invalid",
        ),
        (
            "research.usage",
            {
                "computeSeconds": 2.5,
                "modelTokens": 10,
                "costUsd": "0.03",
                "quotaExceeded": False,
            },
            "costUsd",
            0.03,
        ),
        (
            "research.worker",
            {
                "workerId": "w",
                "capabilities": ["search"],
                "activeLeases": 1,
                "status": "busy",
                "queueDepth": 0,
            },
            "capabilities",
            [1],
        ),
        (
            "research.operation",
            {"operationId": "o", "name": "create_research_campaign", "campaignId": "c"},
            "name",
            "delete_campaign",
        ),
    ],
)
def test_payload_types_round_trip_and_reject_bad_fields(
    tmp_path, campaign, fact_type, payload, bad_field, bad_value
):
    path = tmp_path / "facts.jsonl"
    campaign.update(factType=fact_type, payload=payload)
    append_program_fact(path, campaign)
    invalid = deepcopy(campaign)
    invalid["sequence"] = 2
    invalid["payload"][bad_field] = bad_value
    with pytest.raises(ValueError, match="^invalid_schema$"):
        append_program_fact(path, invalid)
    assert read_program_facts(path) == [campaign]
