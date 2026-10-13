# Shared frozen corpus API

`omp_work.frozen_corpus` supplies byte-bound inputs for OMP-252 Harbor
comparisons and OMP-317 R13 development comparisons and fresh confirmation
cohorts. It qualifies existing inputs; it does not compile text into graphs,
extract new semantics, select research policy, or authorize model trials.

```python
from omp_work.frozen_corpus import freeze, verify, tree_sha256

sealed = freeze(
    "cases",
    [("p12-01", "p12-01.json"), ("p12-02", "nested/p12-02.json")],
    origin_commit="0123456789abcdef0123456789abcdef01234567",
    manifest="artifacts/corpus.json",
)
verified = verify("cases", "artifacts/corpus.json")
```

`freeze(directory, files, origin_commit, manifest)` accepts a nonempty list of
`(id, relative JSON path)` pairs. IDs and paths must each be unique. Paths use
canonical POSIX spelling; absolute paths, traversal, aliases and symlinks are
rejected. Every file under the input directory must appear exactly once; extra
files (including non-JSON files), missing files and invalid JSON are rejected.
The manifest must resolve outside the input directory. The origin commit is a
full lowercase 40- or 64-character Git object ID supplied by the caller, not a
claim that the tool retrieved or adjudicated history.

The returned `FrozenCorpus` records version `1`, origin commit, path-sorted
`files` with IDs and exact-byte SHA-256 hashes, `tree_sha256`, and
`manifest_sha256`. The manifest digest uses shared canonical JSON hashing over
all other fields. The immutable-artifact installer writes canonical JSON bytes;
repeating an identical freeze reuses the artifact, while different bytes at the
same destination raise `RuntimeError("artifact_unavailable")` without replacing it.

`verify(directory, manifest)` returns the validated record. Invalid records,
duplicate IDs/paths, digest drift, membership drift, JSON errors and byte drift
raise `ValueError`; missing/unreadable files surface filesystem errors. Consumers
must verify before replay and keep inputs stable during freeze and replay.
Digests detect drift relative to a trusted manifest; they are not signatures or
owner approval. Keep the reviewed manifest digest independently pinned.

`tree_sha256(directory)` preserves Harbor's exact algorithm: files discovered
with `os.walk(..., followlinks=False)`, sorted by relative POSIX path, each
contributing UTF-8 path, NUL, unsigned 8-byte big-endian byte length, then original
content bytes. Enumeration order does not matter; paths, whitespace and content
do. This low-level compatibility function retains Harbor's existing file-link
behavior; the JSON freeze/verify boundary rejects symlinks. Harbor's public
`task_sha256` delegates to it. Existing Harbor manifest fields, serialization,
corpus hashes and fixtures remain unchanged.

## Replay metrics and review boundaries

For local P12 qualification, historical inputs mean tracked, adjudicated P12
fixtures, not externally retrieved history. Compare existing bounded readiness
with NSI-1 typed compatibility on matched frozen cases; exercise NSI-2 provenance
separately. Report case IDs, corpus digest, evaluator/version and cohort identity
with results so development evidence cannot masquerade as fresh confirmation.

| Metric | Meaning |
| --- | --- |
| Readiness / typed compatibility | Existing readiness outcome versus NSI-1 compatibility outcome on the same input; report disagreements and refusal/error outcomes, not only aggregate successes. |
| Provenance | Separate NSI-2 provenance outcome; compatibility success does not establish provenance validity. |
| Tokens | Replay accounting with zero model calls; distinguish any input token estimate from actual billed/generated model tokens (zero). No inference-cost or model-quality claim follows. |
| Correction burden | Count semantic edits required to reach independently specified corrected drafts. Exclude formatting-only changes; state edit-count rubric and denominator. |
| Historical evidence | Replay outcomes on the pinned, adjudicated tracked fixtures; not a new owner adjudication or proof on unseen tasks. |

For OMP-252, freezing a corpus does not approve spending or choose a production
model. For OMP-317 R13, reproducible development replay does not replace fresh
confirmation, independent evaluator evidence, or owner review. Chris applies
local route selection after reviewing evidence; installed deployment remains a
separate operation. Paid/model trials, new extraction semantics, exploration,
deployments and formal solvers remain deferred. If a solver need appears,
record the failing case and requirement; do not introduce Z3 automatically.
