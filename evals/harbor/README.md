# omp-harbor-eval

Host-side evidence, fixtures, and grader for Harbor evaluation runs.

The score is `grade` over a sealed evidence directory. Harbor's in-container
verifier is not the score. `seal` writes `manifest.json` (every other file's
sha256) and returns the sha256 of those manifest bytes. That digest is passed
out of band; `load_evidence` trusts it, then checks each file hash. A missing,
extra, or altered file, or a run id, nonce, or fixture digest that does not
match the graded fixture, is `invalid_evidence`. An outcome of `harness_error`
is `harness_defect`.

```bash
python -m omp_harbor_eval.grader grade --help
python -m omp_harbor_eval.grader validate --help
```

Both subcommands print one JSON object. This package does not call a model.
