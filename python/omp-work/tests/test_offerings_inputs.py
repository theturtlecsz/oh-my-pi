"""OMP-615-s09-s01: the frozen offerings captures refuse malformed evidence.

Failure mode defended: the daily read-only model-offerings check compares an
owner capture against the governing policy. If a wrong-provider row, a duplicate
provider mapping or model id, a malformed shape/type/timestamp, or an absent
freshness proof slips through parsing, the check reports a false comparison
(including a fabricated retrieval time) instead of an evidence gap. The tests
below mutate valid captures and prove each refusal names its field, and that a
proof gap, a ``checked`` empty coverage and ``not_published`` stay distinct.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from omp_work.routing.offerings_inputs import (
    OfferingsInputError,
    SourceGap,
    parse_listing,
    parse_notices,
    parse_sources,
)

SOURCES = {
    "version": "offerings-sources.v1",
    "notices": "python/omp-work/tests/fixtures/offerings-notices.json",
    "lanes": {
        "gemini_flash": {"kind": "omp-cli", "catalog_provider": "gemini_flash"},
        "kimi": {
            "kind": "listing-file",
            "catalog_provider": "kimi",
            "path": "python/omp-work/tests/fixtures/kimi-listing.json",
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
            "fetchedAt": 1760400000000,
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
        "retrieved_at": "2026-10-14T00:00:00+00:00",
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
                "retrieved_at": "2026-10-14T00:00:00+00:00",
                "items": [
                    {
                        "model": "gemini-3.7-flash",
                        "type": "deprecation",
                        "url": "https://ai.google.dev/gemini-api/docs/deprecations",
                        "published": "2026-09-01",
                        "effective": "2026-12-01",
                    }
                ],
            },
            "status": {
                "coverage": "checked",
                "source": "https://status.cloud.google.com/",
                "retrieved_at": "2026-10-14T00:00:00+00:00",
                "value": "operational",
            },
        },
        "kimi": {
            "notices": {
                "coverage": "not_published",
                "source": "https://platform.kimi.ai/",
                "retrieved_at": "2026-10-14T00:00:00+00:00",
            }
        },
    },
}


def lane(name: str):
    return parse_sources(json.dumps(SOURCES)).lanes[name]


def refused(payload: dict, field_substring: str) -> None:
    with pytest.raises(OfferingsInputError) as info:
        parse_sources(json.dumps(payload))
    assert field_substring in str(info.value)
    assert info.value.field


# --- sources: duplicates, wrong kinds, unknown shapes -----------------------


def test_valid_sources_parses_lanes_with_kind_specific_path():
    sources = parse_sources(json.dumps(SOURCES))
    assert set(sources.lanes) == {"gemini_flash", "kimi"}
    assert sources.lanes["gemini_flash"].path is None
    assert sources.lanes["kimi"].path.endswith("kimi-listing.json")


def test_duplicate_provider_mapping_refused_naming_field():
    payload = json.loads(json.dumps(SOURCES))
    payload["lanes"]["kimi_alias"] = {"kind": "omp-cli", "catalog_provider": "gemini_flash"}
    refused(payload, "lanes.kimi_alias.catalog_provider")


def test_duplicate_json_key_refused():
    raw = '{"version": "offerings-sources.v1", "version": "offerings-sources.v1", "notices": "x", "lanes": {}}'
    with pytest.raises(OfferingsInputError) as info:
        parse_sources(raw)
    assert "duplicate JSON key" in str(info.value)


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        (lambda p: p.__setitem__("version", "offerings-sources.v2"), "version"),
        (lambda p: p.__setitem__("notices", ""), "notices"),
        (lambda p: p.pop("lanes"), "lanes"),
        (lambda p: p.__setitem__("lanes", []), "lanes"),
        (lambda p: p.__setitem__("lanes", {}), "lanes"),
        (lambda p: p["lanes"].__setitem__("bad", {"kind": "scrape", "catalog_provider": "x"}), "lanes.bad.kind"),
        (lambda p: p["lanes"]["kimi"].pop("path"), "lanes.kimi.path"),
        (lambda p: p["lanes"]["kimi"].__setitem__("path", 5), "lanes.kimi.path"),
        (lambda p: p["lanes"]["gemini_flash"].__setitem__("path", "a.json"), "lanes.gemini_flash.path"),
        (lambda p: p["lanes"]["kimi"].__setitem__("catalog_provider", ""), "lanes.kimi.catalog_provider"),
        (lambda p: p["lanes"].__setitem__("no_provider", {"kind": "omp-cli"}), "lanes.no_provider.catalog_provider"),
    ],
)
def test_malformed_sources_refused(payload, field):
    data = json.loads(json.dumps(SOURCES))
    payload(data)
    refused(data, field)


# --- listing: wrong provider, duplicates, types, proofs ---------------------


def test_cli_listing_preserves_attributes_and_provenance():
    listing = parse_listing(json.dumps(CLI_LISTING), lane("gemini_flash"))
    assert listing.provenance is not None
    assert listing.provenance["fetchedAt"] == 1760400000000
    assert listing.gaps == ()
    assert listing.models[0].thinking == ("low", "high")
    assert listing.sha256 == hashlib.sha256(json.dumps(CLI_LISTING).encode()).hexdigest()


def test_listing_wrong_provider_refused_per_row():
    payload = json.loads(json.dumps(CLI_LISTING))
    payload["models"][0]["provider"] = "openai_codex"
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("gemini_flash"))
    assert "models[0].provider" in str(info.value)


def test_duplicate_model_id_refused():
    payload = json.loads(json.dumps(CLI_LISTING))
    payload["models"].append(json.loads(json.dumps(payload["models"][0])))
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("gemini_flash"))
    assert "duplicate model id" in str(info.value)


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda p: p["models"][0].pop("name"), "models[0].name"),
        (lambda p: p["models"][0].__setitem__("contextWindow", "big"), "models[0].contextWindow"),
        (lambda p: p["models"][0].__setitem__("reasoning", "yes"), "models[0].reasoning"),
        (lambda p: p["models"][0].__setitem__("thinking", "high"), "models[0].thinking"),
        (lambda p: p["models"][0].__setitem__("input", ["audio"]), "models[0].input"),
        (lambda p: p["models"][0].__setitem__("cost", []), "models[0].cost"),
        (lambda p: p.pop("models"), "models"),
        (lambda p: p.__setitem__("models", {}), "models"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("status", "error"), "status"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("stale", True), "stale"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("source", "bundled"), "source"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("fetchedAt", "2026-10-14"), "fetchedAt"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("fetchedAt", -1), "fetchedAt"),
        (lambda p: p["discovery"]["gemini_flash"].__setitem__("provider", "kimi"), "provider"),
    ],
)
def test_malformed_cli_listing_refused(mutate, field):
    payload = json.loads(json.dumps(CLI_LISTING))
    mutate(payload)
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("gemini_flash"))
    assert field in str(info.value)


def test_missing_cli_proof_is_a_gap_not_a_timestamp():
    payload = json.loads(json.dumps(CLI_LISTING))
    payload.pop("discovery")
    listing = parse_listing(json.dumps(payload), lane("gemini_flash"))
    assert listing.provenance is None
    assert listing.gaps == (SourceGap("gemini_flash", "gemini_flash", "no cli discovery proof for provider"),)
    assert not hasattr(listing.gaps[0], "retrieved_at")


def test_missing_file_proof_is_a_gap():
    payload = json.loads(json.dumps(FILE_LISTING))
    payload.pop("retrieval")
    listing = parse_listing(json.dumps(payload), lane("kimi"))
    assert listing.provenance is None
    assert listing.gaps[0].reason == "no file retrieval proof"


def test_file_listing_requires_utc_retrieval_and_success():
    payload = json.loads(json.dumps(FILE_LISTING))
    payload["retrieval"]["retrieved_at"] = "2026-10-14T00:00:00"
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("kimi"))
    assert "retrieved_at" in str(info.value)

    payload = json.loads(json.dumps(FILE_LISTING))
    payload["retrieval"]["success"] = False
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("kimi"))
    assert "retrieval.success" in str(info.value)


def test_file_listing_wrong_provider_refused():
    payload = json.loads(json.dumps(FILE_LISTING))
    payload["retrieval"]["provider"] = "gemini_flash"
    with pytest.raises(OfferingsInputError) as info:
        parse_listing(json.dumps(payload), lane("kimi"))
    assert "retrieval.provider" in str(info.value)


# --- notices: coverage states stay distinct ---------------------------------


def test_checked_vs_not_published_stay_distinct():
    notices = parse_notices(json.dumps(NOTICES))
    gemini = notices.providers["gemini_flash"]
    kimi = notices.providers["kimi"]
    assert gemini.notices is not None and gemini.notices.coverage == "checked"
    assert gemini.notices.items is not None and len(gemini.notices.items) == 1
    assert gemini.notices.items[0].effective.isoformat() == "2026-12-01"
    assert gemini.status is not None and gemini.status.value == "operational"
    assert kimi.notices is not None and kimi.notices.coverage == "not_published"
    assert kimi.notices.items is None
    assert kimi.status is None


def test_checked_empty_items_is_not_not_published():
    payload = json.loads(json.dumps(NOTICES))
    payload["providers"]["gemini_flash"]["notices"]["items"] = []
    notices = parse_notices(json.dumps(payload))
    dim = notices.providers["gemini_flash"].notices
    assert dim is not None and dim.coverage == "checked" and dim.items == ()


@pytest.mark.parametrize(
    ("mutate", "field"),
    [
        (lambda p: p.__setitem__("version", "offerings-notices.v2"), "version"),
        (lambda p: p.pop("providers"), "providers"),
        (lambda p: p["providers"]["gemini_flash"]["notices"].__setitem__("coverage", "sometimes"),
         "notices.coverage"),
        (lambda p: p["providers"]["gemini_flash"]["notices"].pop("source"), "notices.source"),
        (lambda p: p["providers"]["gemini_flash"]["notices"].__setitem__("retrieved_at", "yesterday"),
         "notices.retrieved_at"),
        (lambda p: p["providers"]["kimi"]["notices"].__setitem__("items", []), "notices.items"),
        (lambda p: p["providers"]["gemini_flash"]["notices"].pop("items"), "notices.items"),
        (lambda p: p["providers"]["gemini_flash"]["notices"]["items"][0].__setitem__("type", "rumour"),
         "items[0].type"),
        (lambda p: p["providers"]["gemini_flash"]["notices"]["items"][0].__setitem__("published", "soon"),
         "items[0].published"),
        (lambda p: p["providers"]["gemini_flash"]["notices"]["items"][0].pop("effective"),
         "items[0].effective"),
        (lambda p: p["providers"]["gemini_flash"]["status"].__setitem__("value", "green"), "status.value"),
        (lambda p: p["providers"]["gemini_flash"]["status"].pop("value"), "status.value"),
        (lambda p: p["providers"]["gemini_flash"]["status"].__setitem__("coverage", "not_published"),
         "status.value"),
    ],
)
def test_malformed_notices_refused(mutate, field):
    payload = json.loads(json.dumps(NOTICES))
    mutate(payload)
    with pytest.raises(OfferingsInputError) as info:
        parse_notices(json.dumps(payload))
    assert field in str(info.value)


def test_absent_lane_and_dimension_stay_absent():
    payload = json.loads(json.dumps(NOTICES))
    payload["providers"].pop("kimi")
    payload["providers"]["gemini_flash"].pop("status")
    notices = parse_notices(json.dumps(payload))
    assert "kimi" not in notices.providers
    assert notices.providers["gemini_flash"].status is None
