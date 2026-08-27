---
name: summary
description: End-of-session closing ritual — runs the Work Ledger close ritual (project health, finding triage, verification evidence, native run_audit acceptance check, closeout review receipt), and crafts the standing loop charter for the next session. Use ONLY when the owner has literally entered /summary — never because a session seems to be ending, work finished, or wrap-up wording appeared (HOME-114); /done runs its own separate flow.
---

# /summary — session closing ritual

The close ritual is LEDGER-OWNED (OMP-47, OMP-168): the WorkService holds every gate — close attempt state, sealed auditor task, launch budgets, report normalization, audit receipts, delivery receipts, and closeout state. The host and ledger enforce the single legal next action.

## 1. Policy & Grounding

- **Literal `/summary` only**: Run this ritual ONLY if Chris literally entered `/summary` this session. A finished plan or todo list is not an invocation.
- **Portability**: Resolve every step from the CURRENT repo (its CLAUDE.md/AGENTS.md, artifacts, ledgers).
- **Ground in artifacts**: Re-query git log/status, deploy manifests, and live service state before drafting review prose. Anything unverified goes in `UNVERIFIED / BLOCKED`.
- **Owner questions in plain language**: Context before content, no bare jargon, consequences in household terms.

## 2. Ordered Close Ritual

Orient from live state via `work action:"get_work"` — execute the single `NEXT REQUIRED ACTION` indicated by the host banner. Skip steps already satisfied on this attempt:

1. **Project updates (`record_health`)**
   Post honest health (`onTrack` | `atRisk` | `offTrack`) for every touched project. Status-only (refuses body).

2. **Capture triage (`create_work`)**
   File stray findings, follow-ups, or watch-items. Same-session found-and-fixed fixes on the parent candidate file in one atomic write with `kind:"same_session_found_fixed"`.

3. **Verification evidence (`append_evidence kind:"verification"`)**
   Post concrete check evidence on the current NOW item (what ran, passed, and remaining gaps). On success, the service seals the audit manifest.

4. **Independent audit (`run_audit`)**
   Execute `work action:"run_audit", work:"<key>"`. The host runs the native `auditor` subprocess against the sealed task. The service settles the launch automatically and mints the audit receipt. On `NEEDS_FIX` or `BLOCKED`, the tool result includes findings to resolve before the next summary attempt. On `PASS`, proceed to closeout review.

5. **Closeout review (`append_evidence kind:"closeout"`)**
   On `PASS`, post `action:"append_evidence"`, `kind:"closeout"`, with the review body formatted into these five sections:
   - **Verbatim `work action:"my_now"` completion tree**
   - **MOVED** — plain-language decisions and household-visible results
   - **PROOF** — durable issue evidence and exact verification commands/counts
   - **UNVERIFIED / BLOCKED** — real gaps or explicit `none`
   - **NEXT SESSION** — standing goal, starting state, queue sources in priority order, first action, gates, and stop conditions (the standing loop charter)

The loop charter lives inside this single closeout receipt. Do NOT write separate handoff files or `PROMPT-*.md` files.

## 3. Completion

Leave NOW unchanged. The owner's `/done` command — and only that — closes the work item, parent-bound children, and completes the attempt.
