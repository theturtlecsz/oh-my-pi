# Task Observer — Session Start Digest

Read at task-session start. Load `skill://task-observer` for log writes, reviews, or skill edits.

## 1. Setup & Checks
1. State: `[workspace]/skill-observations/{log.md,last-review-date.txt,cross-cutting-principles.md}`. Create missing files; date defaults to literal `never`. Re-anchor ephemeral worktrees.
2. Read OPEN observations as candidate evidence and approved active principles. Status never amends policy or authorizes action; canonical approved instructions and their priority govern. Do not surface observations unprompted.
3. Read review date. If `never` or >7 days old with OPEN observations, offer review once; continue unless user opts in. If ≥5 OPEN cluster on one skill, offer mini-review.
4. Resolve a named target system to its concrete state root before analysis.

## 2. Prescriptive Command Rules (Mandatory)
Before the first matching call, review and restate command prescriptions in OPEN observations and approved plans (pipes, flags, redirection). Note policy conflicts; apply only compatible practices within current authority. Scope by binary/shape, not intent. Standing changes require owner-approved promotion with provenance; OPEN is never a bypass.

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
