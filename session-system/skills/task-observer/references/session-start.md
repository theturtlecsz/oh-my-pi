# Task Observer — Session Start Digest

Read at the start of task-oriented sessions. Load the full `skill://task-observer` for log appends, reviews, or skill edits.

## 1. Setup & Checks
1. State paths: log is `[workspace]/skill-observations/log.md`, review date is `last-review-date.txt`, principles in `cross-cutting-principles.md`. If missing, create (date defaults to literal `never`). Re-anchor if in an ephemeral worktree.
2. Scan: read and scan OPEN observations in `log.md` and active principles in `cross-cutting-principles.md`. Hold them in awareness; do not surface unprompted.
3. Review check: read `last-review-date.txt`. If `never` or >7 days old with OPEN observations, offer review in one line and continue unless user opts in. If ≥5 OPEN cluster on one skill, offer mini-review.
4. Target system: if analyzing a named target system, resolve to concrete state root first.

## 2. Prescriptive Command Rules (Mandatory)
Any OPEN observation or plan clause prescribing command construction ("never pipe", "run separately", flags, redirection) MUST be restated in working notes BEFORE the first tool call it governs. Scope by command binary/shape, not intent.

## 3. What to Watch During Work
- New skill: recurring multi-step workflow, user-explained methodology.
- Improve skill: rule violations (needs structural enforcement), edge cases, better patterns.
- Simplify skill: unused sections, unvalidated rules, bypassed complexity.
- Do not log: one-off user preferences, unrelated tool bugs.

## 4. Full Skill Episodes
Load `skill://task-observer` before:
- Logging observations (mandatory checkpoint every 3rd todo, deliverable flush, 3-step numbering, pre-write assertion, log-write safety, archival on write).
- Running weekly reviews (`references/weekly-review.md`).
- Creating/editing skills (`references/skill-authoring.md`, staging under `skill-updates/[date]/[skill]/`, 2-tier verification).
