# NSI-6 frozen comparison and rollback evidence

**NOT QUALIFIED.** Actual current and typed arms each produce 13 false-ready
cases among 19 gold non-ready cases. No real Chris review, approval, state write,
route promotion, deployment, model call or paid trial occurred. Evidence can be
merged for inspection; it cannot authorize cutover. No failures were removed and
no corpus labels, evaluator semantics or research policy were changed.

## Stored evidence and bindings

- `shadow.json`: actual label-blind replay of both implementations for all 26
  frozen cases, raw inputs/results, per-case metrics, totals, NSI-2 PostgreSQL
  proof observations, paraphrase pairs, implementation/schema/corpus pins, and
  embedded exact rollback bytes. `report_sha256` seals canonical report content.
- `evidence-manifest.json`: shared frozen_corpus seal of exact shadow, rollback,
  service and host report bytes. Source origin is generating checkout HEAD
  before this evidence commit; each implementation/test digest remains pinned.
  Verification copies only those four JSON files to a temporary input directory
  and rejects even whitespace drift.
- `rollback.json`: combined service and host proof, preserving original two
  receipts as exact bytes and decoded records, richer captured observations,
  artifact hashes and test-source hashes. `service.json` and `host.json` preserve
  captured HTTP and workflow-tool outcomes respectively.
- `test_nsi6_evidence.py`: verifies stored hashes, recomputes every case metric
  and total, invokes existing rollout gate to re-execute both arms before its
  expected critical-omission rejection, and rejects resealed metric/result/pin
  tampering, missing cases, changed report/rollback bytes and corpus byte drift.
  This verifies evidence integrity, not qualification. Recorded provenance is
  consumed for deterministic recomputation; generating a new report exercises
  disposable PostgreSQL again. SHA-256 pins detect drift, not forged authorship.

Frozen origin: `ae7d23b0c8bad663b229ed987de90196ada58e80`.
Manifest digest: `0b4269e4a2fb9cd3dcf8325ff7aa9e208086733ba13ef51ae3326ad1ed114b4b`.
Tree digest: `d46431adc846f66b0ae3a634e6d10b578fc9388a3bf35861bbb1d7bbad5f85e1`.

Requested `python/omp-work/tests/fixtures/nsi6-manifest.json` is absent in this
checkout. Existing `nsi_eval.run` resolves that exact legacy name to
`evals/nsi6/v1/nsi6-manifest.json`; inputs are `evals/nsi6/v1/inputs`.
Requested `intake_route.py` and workflow `intake-route.ts` are also absent.
Actual route selection lives in `omp_work.nsi_route` and
`session-system/extensions/workflow/host.ts`; their bytes are pinned by existing
implementation binding. No compatibility files or substitute evaluators added.

## Metrics, denominators and units

All case counts use the same 26 frozen cases in original order. Failed cases
stay in expected denominators. Zero denominators yield `value: null`, retaining
numerator/denominator. Metrics describe fixture replay, not production quality.

| Metric | Current | Typed | Denominator / unit / interpretation |
| --- | --- | --- | --- |
| False-ready | 13/19 | 13/19 | Cases with gold readiness other than READY_FOR_RATIFICATION; numerator successful arm claims ready. Invalid cases remain in denominator. |
| Criterion coverage | 72/92 | 72/92 | Expected criterion-ID occurrences across cases; numerator set intersection with returned criterion IDs. Not semantic correctness. |
| Critical detection | 72/92 | 72/92 | Gold required-behavior occurrences; numerator criterion ID (ID or ac-ID) present or complete finding targets behavior. Invalid/refused/crashed arms claim none. |
| Undetected critical count | 20 | 20 | Count of missed required-behavior occurrences, out of 92; IDs retained per case, not deduplicated across cases. |
| Semantic edit count | 42 | 42 | Sum of leaf replacements/additions/deletions across 26 drafts; 42/26 edits per case if averaged. Missing record counts once; lists of ID records compare by ID, other list differences count once. Source and budget excluded; text changes count once without model judgment. Failed arms use raw draft. No repaired output invented. |
| Material questions by target | security: 1; others: 0 | Same | Raw capped-question count per gold material target across cases, not ratio. Eligible target sets vary by case; complete zero entries retained in JSON. Multiple questions count separately. |
| Questions by target | max_latency_ms: 1; none: 1; security: 1 | Same | Three returned questions across 26 cases, cap two per case; not all complete findings. Target is normalized dedup key. |
| Redundant repeats by target | {} | {} | Sum of max(count-1, 0) within each case/target; counts, no denominator. Empty map means no repeats among returned questions. |
| Arm statuses | 23 ok, 3 invalid | Same | Case observations out of 26 per arm; invalid is validation refusal, not ready. No arm skipped/crashed. |
| Paraphrase agreement | 2/2 | 2/2 | Two declared paired groups; both arms successful with equal readiness, criterion IDs, complete finding targets and capped question target multiplicities. Failed pairs would remain denominator. Not paraphrase robustness beyond these four cases. |
| Valid NSI-2 proofs | 1/4 | Shared | Four cases with proof scenarios; numerator current revision and baseline binding match. Refused/stale scenarios count as failures, not missing observations. |
| Provenance statuses | 2 ok, 2 refused, 22 skipped | Shared | 26 cases; 22 have no scenario, not safety evidence. One ok result is stale and invalid as proof. |
| Model tokens | 0 | 0 | Actual replay model calls: zero. Not estimated input tokens, price, model latency or inference savings. |
| Wall seconds | See stored totals | See stored totals | Monotonic measured seconds, summed over 26 arm observations / four provenance executions. Total report duration also includes fixture startup and shutdown; timing is nondeterministic and cannot be treated as matched performance evidence. |

## Every case, actual paths

Both arms agree on listed readiness/status. `FR` is each arm's false-ready
numerator for that case; all raw findings, capped questions, diagnostics and
metric numerators/denominators remain in `shadow.json`.

| Case | Current | Typed | FR | NSI-2 |
| --- | --- | --- | --- | --- |
| history-01-valid-ready | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| history-04-reordered-ready | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| history-03-conflicting-positives | CONFLICTED | CONFLICTED | 0 | No scenario |
| history-07-missing-oracle | NEEDS_SPEC | NEEDS_SPEC | 0 | No scenario |
| history-09-tampered-span | invalid | invalid | 0 | No scenario |
| ready-complete | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| omitted-rollback | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| silent-omitted-rollback | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| omitted-security | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| silent-omitted-security | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| omitted-failure | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| silent-omitted-failure | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| contradictory-security | CONFLICTED | CONFLICTED | 0 | No scenario |
| ambiguous-scope | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| stale-source | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | Refused: inconsistent source |
| stale-revision | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | ok, but stale revision; proof invalid |
| tampered-provenance | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | Refused: inconsistent hash |
| tampered-source-hash | invalid | invalid | 0 | No scenario |
| tampered-span-hash | invalid | invalid | 0 | No scenario |
| injected-ready-command | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| ready-paraphrase-a | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| ready-paraphrase-b | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | No scenario |
| omitted-security-paraphrase-a | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| omitted-security-paraphrase-b | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |
| ready-current-proof | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 0 | Valid current proof |
| injected-omitted-security | READY_FOR_RATIFICATION | READY_FOR_RATIFICATION | 1 | No scenario |

Omissions include rollback, security and failure criteria, including silent
omissions and security paraphrases. Missing critical IDs also include historical
conflicting positives (`ac-export`, `ac-table`) and invalid hash cases (four
behaviors each, plus historical `ac-hash`). Complete per-case IDs are retained;
none were rewritten as passes. Paraphrase groups are `complete-export` and
`missing-security`: consistent wrong readiness still agrees. Bounded readiness
alone does not detect stale NSI-2 sources/revisions or hash tampering; separate
provenance replay retains those refusals/staleness rather than blessing readiness.

## Rollback rehearsal scope

Service capture reuses existing service test without changing it. Temporary
qualified evidence uses a hypothetical label-blind typed wrapper changing ready
to NEEDS_SPEC, synthetic provenance and simulated Chris input. This enables
mechanical rehearsal only. Runtime HTTP handlers still execute actual current /
typed bounded evaluators against disposable PostgreSQL.

Three fresh applications observe current→typed→current. Saved positive
assessments replay unchanged without evaluator calls. Rejected drafts clarify,
publication returns `intake_not_ready`, changed semantics/ratification return
`stale_intake`, missing admission returns `intake_admission_blocked`. All checked
rejected row deltas are zero. Each valid publication adds one item, relation,
candidate and publication; final counts are 2/1/2/1 respectively. Re-created
applications replay all three publications without new evaluator calls.
`service.json` stores 33 HTTP responses and nine before/rejected/after row counts.
Counts cover these four domain tables, not every audit/receipt write.

Host capture creates a fresh real workflow host per transition, reading copies
of sealed temporary service states. Typed prose is refused; structured preview
and both current prose previews require confirmation with zero backend writes.
Each confirmation invokes one createIssue and one setNowRemote (3/3 phases).
Backend assessment/results/writes are mocked; this proves host dispatch and
confirmation mechanics, not PostgreSQL persistence or model assessment.
Host reader checks kind/version/mode only; it does not validate service evidence
bindings. Temporary service evidence paths in copied states are intentionally
not durable state. Existing host test's minimal rollback receipt alone does not
prove bound service promotion; its original bytes are retained with richer proof.

No installed process restart, live ledger history, unseen task, extraction,
solver, actual owner adjudication, concurrent cutover or deployment tested.
Bounded current findings use existing evaluator on independent issue units to
recover findings beyond public two-question cap; typed raw results preserve all
gaps. This is deterministic labeled replay, not a new model comparison.

## Reproduction

From root, with frozen Bun dependencies, exact-version native addon and native
PostgreSQL binaries available. First service command overwrites rollback output;
host command appends a second JSON record. `seal.py` retains both exact original
receipts and packages richer captures into one JSON object. Existing evaluator
requires a single JSON object and creates reports exclusively, so use a fresh
output destination or remove only old generated `shadow.json` and
`evidence-manifest.json` first. Frozen manifests cannot overwrite different
evidence bytes. Keep old evidence for comparison if needed.

```sh
NSI6_ROLLBACK_OUT=docs/nsi6/rollback.json OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_nsi6_service_route.py
NSI6_ROLLBACK_OUT=docs/nsi6/rollback.json bun test session-system/tests/nsi6-intake-host.test.ts
NSI6_SERVICE_EVIDENCE_OUT=docs/nsi6/service.json OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_nsi6_evidence.py -k capture
NSI6_HOST_EVIDENCE_OUT=docs/nsi6/host.json bun test session-system/tests/nsi6-evidence-host.test.ts
uv run --project python/omp-work --extra dev python docs/nsi6/seal.py
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev python -m omp_work.nsi_eval run --manifest python/omp-work/tests/fixtures/nsi6-manifest.json --rollback-proof docs/nsi6/rollback.json --out docs/nsi6/shadow.json
uv run --project python/omp-work --extra dev python docs/nsi6/seal.py --freeze-evidence --origin "$(git rev-parse HEAD)"
OMP_WORK_POSTGRES_INTEGRATION=1 uv run --project python/omp-work --extra dev pytest python/omp-work/tests/test_nsi6_evidence.py
```

JSON UUIDs, temporary paths and measured durations vary on regeneration. Frozen
inputs, labels, deterministic arm observations and metrics must not change.
Stored test hashes mean edits to tests require regenerating proof, not weakening
verification. Recipe scripts below use temporary directories under checkout and
remove them on exit; they do not launch paid work or alter any policy.

## OMP-252 Harbor API recipe

Runnable offline snapshot of existing batch A, preserving its task IDs,
categories, fixture paths and exact fixture tree bytes. It does not run Harbor,
change its manifest format, select model arms or approve budget/egress. Native
Harbor trees contain TOML/patch/shell files: use `tree_sha256` for those, and
`freeze`/`verify` for JSON descriptors outside those trees. Re-verify live tree
hashes before any separately authorized comparison, retain batch/category
allocation and exclusions, match approved model/version/effort/tool/caps, record
actual costs and failures, and keep fresh confirmation separate from development.

```sh
OMP_ORIGIN=$(git rev-parse HEAD) uv run --project python/omp-work --extra dev python - <<'PY'
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from omp_work.frozen_corpus import freeze, verify, tree_sha256

batch_path = Path("evals/harbor/corpus/batches/a.json")
batch = json.loads(batch_path.read_bytes())
with TemporaryDirectory(prefix=".harbor-freeze-", dir=".") as temp:
    root = Path(temp)
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "batch.json").write_bytes(batch_path.read_bytes())
    fixtures = []
    for task in batch["tasks"]:
        candidates = [Path(task["fixture"]), Path("evals/harbor") / task["fixture"], batch_path.parent / task["fixture"]]
        directory = next(p for p in candidates if p.is_dir())
        fixtures.append({"task_id": task["task_id"], "category": task["category"],
                         "path": str(directory), "tree_sha256": tree_sha256(directory)})
    (inputs / "fixtures.json").write_text(json.dumps(fixtures, sort_keys=True))
    manifest = root / "manifest.json"
    sealed = freeze(inputs, [("batch-a", "batch.json"), ("fixture-trees", "fixtures.json")],
                    os.environ["OMP_ORIGIN"], manifest)
    assert verify(inputs, manifest) == sealed
    for item in fixtures:
        assert tree_sha256(item["path"]) == item["tree_sha256"]
    print(sealed.model_dump_json())
PY
```

This prints a demonstration manifest then removes temporary snapshot. A real
approved trial must retain its immutable descriptor/manifest, bind the Harbor
native manifest and each task digest, independently pin reviewed digest, and
verify immediately before replay. Corpus hashing alone grants no execution.

## OMP-317 R13 API recipe

Runnable **development fixture demonstration**, not fresh confirmation. Imports
existing policy and tree fixture; freezes complete task budget/width plus exact
policy source identities, verifies before comparison, and reports every task
result/cost. No candidate source or research policy is changed. Zero supplied
research/comparison overhead means local fixture accounting only, not zero paid
trial cost. Node trial costs retain fixture units. Wins denominator is number of
frozen development tasks; node metrics retain fixture units and direction.

```sh
OMP_ORIGIN=$(git rev-parse HEAD) uv run --project python/omp-work --extra dev python - <<'PY'
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from omp_work.frozen_corpus import freeze, verify
from omp_work.research import search
from omp_work.research.mechanism_eval import development_comparison

sys.path.insert(0, str(Path("python/omp-work/tests").resolve()))
from test_research_mechanism_eval import _restart, _task

policies = {name: hashlib.sha256(inspect.getsource(fn).encode()).hexdigest()
            for name, fn in (("candidate", _restart), ("baseline", search.greedy))}
with TemporaryDirectory(prefix=".r13-freeze-", dir=".") as temp:
    root = Path(temp)
    inputs = root / "inputs"
    inputs.mkdir()
    document = {"cohort": "development-fixture", "policies": policies,
                "tasks": {"r13-development-fixture": _task()}}
    (inputs / "development.json").write_text(json.dumps(document, sort_keys=True))
    manifest = root / "manifest.json"
    freeze(inputs, [("development-fixture", "development.json")], os.environ["OMP_ORIGIN"], manifest)
    verified = verify(inputs, manifest)
    loaded = json.loads((inputs / "development.json").read_bytes())
    assert loaded["policies"] == policies
    result = development_comparison(_restart, search.greedy, loaded["tasks"],
                                    research_cost=0.0, comparison_cost=0.0)
    print(json.dumps({"corpus": verified.model_dump(), "comparison": result}, indent=2))
PY
```

For real R13 work, substitute already admitted complete development inputs and
pinned candidate/baseline policy callables, not a rewritten tree, budget, metric,
policy or grant. Preserve candidate inspect→contract_tested→development_compared
lifecycle; freeze confirmation in a separate directory/manifest only after
candidate source/policy and win threshold are pinned. `ConfirmationCohort`
requires task IDs disjoint from development and a valid required_wins threshold;
`confirm` checks frozen source against candidate source hash. Broader recursive
controller also requires content/cohort digests disjoint from prior history;
renaming/reusing development content is not fresh. Use its existing admission,
consumption history and policy/source/grant checks; frozen_corpus never replaces
them. Do not use this fixture as confirmation or mint a contract-pass/owner
approval from a successful freeze. Real independent confirmation, runner
provenance, finite budget/egress authority and owner review remain required.

Validation: original service tests 6 passed; original host tests 14 passed;
new host capture 1 passed; new stored-evidence/service tests 12 passed. Both
offline API recipes executed successfully.
Initial setup failures and rehearsal limitations are recorded in
`NOTES-EXTERNAL.md`. Stored-evidence tests run separately with PostgreSQL enabled;
full suite/gate deferred to flood.
