# ECC Database Reviewer trial

## Method

Use the real roster entry `ECC Database Reviewer`. Check out each case commit in its own worktree. Run this command in that worktree:

```sh
omp -p --advisor
```

The case prompt is `runs/<id>/prompt.md`. The advisor transcript is `runs/<id>/advisor-transcript.jsonl`. Accepted notes are `runs/<id>/notes.json`. `runs/<id>/SHA256SUMS` is the SHA-256 checksum of those three files.

A finding is an accepted advise call. The note number is `n`.

## Labels

Label every finding in `labels/<id>.json`.

`useful` means the finding is a real problem, or a needed check, and the finding is in scope at that commit. `false_alarm` means every other finding.

`known_defects_caught` is the number of `known_bad` cases that have a useful label with `matches_known_defect` set. Leave `matches_known_defect` unset on an ordinary case.

## Rule

Keep the advisor when `useful >= false_alarm` and `known_defects_caught >= 1`. Drop the advisor for every other tally.

`useful` and `false_alarm` are label counts across the whole trial. `summary.json` stores the rule text `keep iff useful >= false_alarm and known_defects_caught >= 1, else drop`. `recommendation` is `keep` or `drop`.

Run `bun scripts/ecc-advisor-trial/tally.ts --write` to write `<root>/summary.json`. The default root is this directory. `--check` validates each run directory. `--check` also validates a labels file when that file is present.
