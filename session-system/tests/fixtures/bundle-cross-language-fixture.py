#!/usr/bin/env python3
"""Cross-language bundle fixture generator.

Generates bundle content bytes and SHA-256 hash using qualified candidate Python
(omp_work.v1.canonical.canonical_json and sha256).

Vector specifications:
- Unicode values: Accented Latin (é, ú, ñ), emojis (🚀, 👾), CJK (漢字), em-dash (—)
- Numeric values: positive integer (42), negative integer (-17), zero (0), float (3.14159, -0.001)
- Key-order values: deliberately unsorted keys in mandatory, optional, and nested objects to verify lexicographical sorting
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Add candidate python/omp-work/src to sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
CANDIDATE_SRC = REPO_ROOT / "python" / "omp-work" / "src"
if str(CANDIDATE_SRC) not in sys.path:
    sys.path.insert(0, str(CANDIDATE_SRC))

from omp_work.knowledge_contracts import CONTEXT_BUNDLE_IDENTITY_ENCODING
from omp_work.v1.canonical import canonical_json, sha256


def budget_actual(spec: dict[str, object], content: dict[str, object]) -> dict[str, object]:
    """Exactly the fields Python BudgetActual emits (no tokenizer_id)."""
    return {
        "method": spec["method"],
        "limit": spec["limit"],
        "used": len(canonical_json(content).encode("utf-8")),
        "mandatory_used": len(canonical_json(content["mandatory"]).encode("utf-8")),
        "dropped_optional": [],
    }


def build_fixture_data() -> dict[str, object]:
    # Deliberately unsorted dictionary keys covering Unicode, numerics, and nested objects
    mandatory = {
        "z_unicode": "éléphant 🚀 漢字 — ñandú 👾",
        "m_numeric": 42,
        "a_float": 3.14159,
        "d_negative": -17,
        "k_zero": 0,
        "b_nested": {
            "zebra": "last-entry",
            "alpha": 1,
            "nested_num": -99.5,
        },
    }

    optional = {
        "z_notes": "✨ cross-language canonical json verification",
        "a_count": 100,
        "p_priority": -1,
        "b_ratio": 0.125,
    }

    # Python canonical JSON serialization over { mandatory, optional }
    content_payload = {
        "mandatory": mandatory,
        "optional": optional,
    }

    content_bytes = canonical_json(content_payload)
    legacy_content_sha = sha256(content_payload)

    budget_spec = {
        "method": "utf8_bytes",
        "limit": 32768,
        "tokenizer_id": None,
    }

    identity_encoding = CONTEXT_BUNDLE_IDENTITY_ENCODING

    # Shared canonical sha256 of full identity payload (v2: encoding tag is a hashed key)
    identity_payload = {
        "identity_encoding": identity_encoding,
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "repository_id": "00000000-0000-0000-0000-000000000002",
        "work_id": "00000000-0000-0000-0000-000000000003",
        "revision_id": "00000000-0000-0000-0000-000000000004",
        "candidate_id": None,
        "stage": "execute",
        "snapshot_id": None,
        "budget_spec": budget_spec,
        "content": content_payload,
    }

    identity_canonical_json = canonical_json(identity_payload)
    bundle_sha = hashlib.sha256(identity_canonical_json.encode("utf-8")).hexdigest()

    bundle_payload = {
        "bundle_id": "00000000-0000-0000-0000-000000000099",
        "bundle_sha256": bundle_sha,
        "identity_encoding": identity_encoding,
        "identity_canonical_json": identity_canonical_json,
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "repository_id": "00000000-0000-0000-0000-000000000002",
        "work_id": "00000000-0000-0000-0000-000000000003",
        "revision_id": "00000000-0000-0000-0000-000000000004",
        "candidate_id": None,
        "stage": "execute",
        "snapshot_id": None,
        "budget": budget_actual(budget_spec, content_payload),
        "mandatory": mandatory,
        "optional": optional,
        "enrichment_status": "applied",
        "proposal_lineage": ["00000000-0000-0000-0000-000000000051"],
        "receipt_lineage": ["00000000-0000-0000-0000-000000000061"],
        "content_canonical_json": content_bytes,
    }

    # Divergent vectors: values whose Python canonical_json bytes differ from any TS
    # re-serialization (float with integral value, exponent formatting, huge float,
    # code-point vs UTF-16 key ordering around non-BMP keys, non-BMP values).
    divergent_mandatory = {
        "ratio": 1.0,
        "tiny": 1e-07,
        "small": 2.5e-05,
        "huge": 1e16,
        "\U0001F600": "non-bmp key sorts after U+FFFD by code point",
        "�": "replacement char key",
        "glyph": "\U0001D518 \U0001F680 plain",
    }
    divergent_optional = {"weight": 10.0, "scale": 1e-05}
    divergent_content = {"mandatory": divergent_mandatory, "optional": divergent_optional}
    divergent_content_bytes = canonical_json(divergent_content)
    divergent_identity = {**identity_payload, "content": divergent_content}
    divergent_identity_json = canonical_json(divergent_identity)
    divergent_sha = hashlib.sha256(divergent_identity_json.encode("utf-8")).hexdigest()
    divergent_bundle_payload = {
        **bundle_payload,
        "bundle_id": "00000000-0000-0000-0000-000000000098",
        "bundle_sha256": divergent_sha,
        "identity_canonical_json": divergent_identity_json,
        "budget": budget_actual(budget_spec, divergent_content),
        "mandatory": divergent_mandatory,
        "optional": divergent_optional,
        "content_canonical_json": divergent_content_bytes,
    }

    # Nested decoy: mandatory carries its own `content` / `identity_encoding` / `repository_id`
    # keys (mimicking the top-level identity layout), a string with escaped quotes and braces,
    # and Unicode/float vectors. Only the positional top-level `content` range is acceptable.
    nested_inner_content = {"mandatory": {"ratio": 1.0, "glyph": "\U0001F680"}, "optional": {}}
    nested_mandatory = {
        "candidate_id": None,
        "content": nested_inner_content,
        "identity_encoding": identity_encoding,
        "repository_id": "00000000-0000-0000-0000-000000000002",
        "tricky": '}{"content":{"mandatory":{},"optional":{}},"identity_encoding":"x" \\ "',
        "ratio": 1.0,
        "tiny": 1e-07,
        "z_unicode": "éléphant 🚀 漢字",
    }
    nested_optional = {"scale": 10.0, "list": [{"content": {"mandatory": {}}}, "]", "}"]}
    nested_content = {"mandatory": nested_mandatory, "optional": nested_optional}
    nested_content_bytes = canonical_json(nested_content)
    nested_identity_json = canonical_json({**identity_payload, "content": nested_content})
    nested_bundle_payload = {
        **bundle_payload,
        "bundle_id": "00000000-0000-0000-0000-000000000097",
        "bundle_sha256": hashlib.sha256(nested_identity_json.encode("utf-8")).hexdigest(),
        "identity_canonical_json": nested_identity_json,
        "budget": budget_actual(budget_spec, nested_content),
        "mandatory": nested_mandatory,
        "optional": nested_optional,
        "content_canonical_json": nested_content_bytes,
    }

    return {
        "nested_content_bytes": nested_content_bytes,
        "nested_inner_content_bytes": canonical_json(nested_inner_content),
        "nested_bundle_payload": nested_bundle_payload,
        "content_bytes": content_bytes,
        "bundle_sha256": bundle_sha,
        "legacy_content_sha256": legacy_content_sha,
        "identity_encoding": identity_encoding,
        "identity_canonical_json": identity_canonical_json,
        "bundle_payload": bundle_payload,
        "mandatory": mandatory,
        "optional": optional,
        "budget_spec": budget_spec,
        "divergent_content_bytes": divergent_content_bytes,
        "divergent_bundle_payload": divergent_bundle_payload,
    }


def main() -> None:
    data = build_fixture_data()
    if len(sys.argv) > 2 and sys.argv[1] == "--output":
        target = Path(sys.argv[2])
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        sys.stdout.write(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
