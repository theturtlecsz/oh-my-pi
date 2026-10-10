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

## Results

Source: `summary.json`, written by `bun scripts/ecc-advisor-trial/tally.ts --write`.

| id | commit | kind | notes | useful | false_alarm | known defect caught |
| --- | --- | --- | --- | --- | --- | --- |
| k1 | 20900d3be9c5245be9aa0267bf6a3a122fd1ab7f | known_bad | 0 | 0 | 0 | no |
| k2 | 44a2f9446f00786188e17567f5eaee79aa020658 | known_bad | 7 | 0 | 7 | no |
| o1 | a2d815bbe659c3d783e43d37c2589387dc0b120b | ordinary | 1 | 0 | 1 | n/a |
| o2 | b2a5ca700d2d48216134cdc074d1b611bdd0914a | ordinary | 11 | 0 | 11 | n/a |
| o3 | 3f38b2984d612e4d233f63419505cc8fedaead58 | ordinary | 2 | 1 | 1 | n/a |
| o4 | b9793a797d403da7be32c84b90c22189635a97cb | ordinary | 4 | 1 | 3 | n/a |

Totals: 6 cases, 2 known_bad. useful = 2, false_alarm = 23.

Known defects:

- Caught: none.
- Missed: k1 (E0187, concurrent same-id `run_operation` calls both run; the advisor made no notes), k2 (E0189, racy `MAX(step_index)+1` insert).

Advisor model (every `notes.json`): `google-antigravity/gemini-3.7-flash`.

Recommendation: **drop**. Rule: keep iff useful >= false_alarm and known_defects_caught >= 1, else drop. Applied: useful 2 >= false_alarm 23 is false, and known_defects_caught 0 >= 1 is false, so drop.

Acting on this recommendation is a later Work Ledger item.
