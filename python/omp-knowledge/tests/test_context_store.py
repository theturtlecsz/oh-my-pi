"""Contract tests for token counters and persisted bundle storage (OMP-311 / FK-5).

Defends observable contracts:
- Reopening the store returns byte-identical text, identity, and exclusion rows.
- Bundle reproduction is byte-identical with no live counter.
- Tampering with stored request_json raises ReproductionError.
- Persisting identical bundle is a no-op, conflicting content raises BundleConflictError.
- OmpTokenCounter successfully communicates with an external CLI counter.
- OmpTokenCounter raises TokenCounterUnavailable on non-zero exit, wrong encoding profile,
  missing IDs, duplicate IDs, extra IDs, or timeout.
- RecordedCounter replays text counts and raises ReproductionError for unrecorded text.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest
from omp_knowledge.context.compiler import compile_bundle
from omp_knowledge.context.models import (
    CompileRequest,
    ContextItem,
    StageIdentity,
)
from omp_knowledge.context.store import (
    BundleConflictError,
    ContextBundleStore,
    ReproductionError,
    compute_bundle_id,
)
from omp_knowledge.context.tokens import (
    OmpTokenCounter,
    RecordedCounter,
    TokenCounterUnavailable,
)
from omp_work.v1.canonical import canonical_json, sha256


class WordCounter:
    """Deterministic in-memory counter for testing store and reproduction."""

    profile = "test-word-counter-v1"

    def count(self, texts: list[str]) -> list[int]:
        return [len(text.split()) for text in texts]


def _make_identity() -> StageIdentity:
    return StageIdentity(
        work_id="11111111-1111-1111-1111-111111111111",
        work_key="OMP-311",
        revision_id="22222222-2222-2222-2222-222222222222",
        stage="implement",
        attempt_id="33333333-3333-3333-3333-333333333333",
        candidate_id="44444444-4444-4444-4444-444444444444",
    )


def _make_request(*, budget: int = 100_000) -> CompileRequest:
    items = (
        ContextItem(
            section="exact",
            source="work",
            ref="spec",
            text="mandatory specification text",
            mandatory=True,
            status="current",
        ),
        ContextItem(
            section="structural",
            source="codebase",
            ref="ast",
            text="structural symbol details",
            status="current",
            score=0.8,
        ),
        ContextItem(
            section="semantic",
            source="search",
            ref="stale-result",
            text="old search result that is stale",
            status="stale",
            detail="superseded by newer index",
        ),
    )
    return CompileRequest(
        identity=_make_identity(),
        token_budget=budget,
        encoding="ClaudeV5",
        items=items,
    )


def test_reopen_load_gives_identical_text_identity_and_exclusion_rows(
    tmp_path: Path,
) -> None:
    counter = WordCounter()
    request = _make_request()
    compiled = compile_bundle(request, counter)

    store = ContextBundleStore(tmp_path)
    bundle_id = store.persist(request, compiled)

    # Reopen a fresh store instance from the same directory
    reopened = ContextBundleStore(tmp_path)
    record = reopened.load(bundle_id)

    assert record is not None
    assert record.bundle_id == bundle_id
    assert record.text == compiled.text
    assert record.bundle_text == compiled.text
    assert record.sha256 == compiled.sha256
    assert record.identity == request.identity
    assert record.exclusions == compiled.exclusions
    assert len(record.exclusions) == 1
    assert record.exclusions[0].ref == "stale-result"
    assert record.exclusions[0].reason == "stale"


def test_reproduce_is_byte_identical_with_no_live_counter(tmp_path: Path) -> None:
    counter = WordCounter()
    request = _make_request()
    compiled = compile_bundle(request, counter)

    store = ContextBundleStore(tmp_path)
    bundle_id = store.persist(request, compiled)

    reproduced_text = store.reproduce(bundle_id)
    assert reproduced_text == compiled.text
    assert sha256(reproduced_text) == compiled.sha256


def test_tampered_request_json_raises_reproduction_error(tmp_path: Path) -> None:
    counter = WordCounter()
    request = _make_request()
    compiled = compile_bundle(request, counter)

    store = ContextBundleStore(tmp_path)
    bundle_id = store.persist(request, compiled)

    # Tamper with request_json in SQLite database: alter item text to unrecorded content
    tampered_dict = request.model_dump(mode="json")
    tampered_dict["items"][0]["text"] = "tampered text that was never recorded"
    tampered_json = canonical_json(tampered_dict)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE context_bundles SET request_json = ? WHERE bundle_id = ?",
            (tampered_json, bundle_id),
        )

    with pytest.raises(ReproductionError):
        store.reproduce(bundle_id)


def test_tampered_malformed_request_json_raises_reproduction_error(
    tmp_path: Path,
) -> None:
    counter = WordCounter()
    request = _make_request()
    compiled = compile_bundle(request, counter)

    store = ContextBundleStore(tmp_path)
    bundle_id = store.persist(request, compiled)

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE context_bundles SET request_json = ? WHERE bundle_id = ?",
            ("not valid json", bundle_id),
        )

    with pytest.raises(ReproductionError):
        store.reproduce(bundle_id)


def test_persist_idempotent_and_conflict_rejection(tmp_path: Path) -> None:
    counter = WordCounter()
    request = _make_request()
    compiled = compile_bundle(request, counter)

    store = ContextBundleStore(tmp_path)
    # First persist creates the record
    bundle_id = store.persist(request, compiled)

    # Second persist with identical request and compiled is a no-op
    again_id = store.persist(request, compiled)
    assert again_id == bundle_id

    # Persist with differing content under the same bundle_id raises BundleConflictError
    conflicting_compiled = compiled.model_copy(update={"text": compiled.text + "\nconflicting extra line"})
    with pytest.raises(BundleConflictError):
        store.persist(request, conflicting_compiled)


def test_compute_bundle_id_is_canonical() -> None:
    identity = _make_identity()
    bundle_sha = "abc123def456"
    expected = sha256({"identity": identity.model_dump(mode="json"), "bundle_sha256": bundle_sha})
    assert compute_bundle_id(identity, bundle_sha) == expected


def test_recorded_counter_replays_and_unrecorded_text_raises() -> None:
    counts = {sha256("alpha"): 1, sha256("beta"): 2}
    counter = RecordedCounter(profile="test-rec", counts=counts)

    assert counter.count(["alpha", "beta"]) == [1, 2]

    with pytest.raises(ReproductionError):
        counter.count(["gamma"])


def test_omp_token_counter_against_fake_counter_returns_counts(tmp_path: Path) -> None:
    script_path = tmp_path / "fake_counter.py"
    script_path.write_text(
        """import sys, json

lines = [line.strip() for line in sys.stdin if line.strip()]
print(json.dumps({"api": "countTokens", "encoding": "ClaudeV5"}))
for line in lines:
    data = json.loads(line)
    print(json.dumps({"id": data["id"], "tokens": len(data["text"].split())}))
"""
    )

    counter = OmpTokenCounter([sys.executable, str(script_path)], encoding="ClaudeV5")
    counts = counter.count(["hello world", "one two three four"])
    assert counts == [2, 4]


def test_omp_token_counter_exit_1_raises_token_counter_unavailable(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "exit_1_counter.py"
    script_path.write_text("import sys\nsys.exit(1)\n")

    counter = OmpTokenCounter([sys.executable, str(script_path)], encoding="ClaudeV5")
    with pytest.raises(TokenCounterUnavailable):
        counter.count(["hello"])


def test_omp_token_counter_wrong_encoding_profile_raises_token_counter_unavailable(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "wrong_encoding_counter.py"
    script_path.write_text(
        """import sys, json
print(json.dumps({"api": "countTokens", "encoding": "OtherEncoding"}))
for line in sys.stdin:
    if not line.strip(): continue
    data = json.loads(line)
    print(json.dumps({"id": data["id"], "tokens": 1}))
"""
    )

    counter = OmpTokenCounter([sys.executable, str(script_path)], encoding="ClaudeV5")
    with pytest.raises(TokenCounterUnavailable):
        counter.count(["hello"])


def test_omp_token_counter_missing_id_raises_token_counter_unavailable(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "missing_id_counter.py"
    script_path.write_text(
        """import sys, json
# Skips the second item
lines = [line.strip() for line in sys.stdin if line.strip()]
print(json.dumps({"api": "countTokens", "encoding": "ClaudeV5"}))
if lines:
    data = json.loads(lines[0])
    print(json.dumps({"id": data["id"], "tokens": 1}))
"""
    )

    counter = OmpTokenCounter([sys.executable, str(script_path)], encoding="ClaudeV5")
    with pytest.raises(TokenCounterUnavailable):
        counter.count(["first item", "second item"])


def test_omp_token_counter_duplicate_and_extra_id_raises_token_counter_unavailable(
    tmp_path: Path,
) -> None:
    dup_script = tmp_path / "dup_id_counter.py"
    dup_script.write_text(
        """import sys, json
print(json.dumps({"api": "countTokens", "encoding": "ClaudeV5"}))
print(json.dumps({"id": "0", "tokens": 1}))
print(json.dumps({"id": "0", "tokens": 1}))
"""
    )

    counter = OmpTokenCounter([sys.executable, str(dup_script)], encoding="ClaudeV5")
    with pytest.raises(TokenCounterUnavailable):
        counter.count(["first item"])

    extra_script = tmp_path / "extra_id_counter.py"
    extra_script.write_text(
        """import sys, json
print(json.dumps({"api": "countTokens", "encoding": "ClaudeV5"}))
print(json.dumps({"id": "0", "tokens": 1}))
print(json.dumps({"id": "extra-id-999", "tokens": 5}))
"""
    )
    counter_extra = OmpTokenCounter(
        [sys.executable, str(extra_script)], encoding="ClaudeV5"
    )
    with pytest.raises(TokenCounterUnavailable):
        counter_extra.count(["first item"])


def test_omp_token_counter_timeout_raises_token_counter_unavailable(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "sleep_counter.py"
    script_path.write_text("import time\ntime.sleep(2)\n")

    counter = OmpTokenCounter(
        [sys.executable, str(script_path)], encoding="ClaudeV5", timeout_s=0.1
    )
    with pytest.raises(TokenCounterUnavailable):
        counter.count(["hello"])
