AUDIT GATE (HOME-131) — the last auditor's report was REFUSED (missing: {{reasons}}). The audit ran, but its report was structurally unusable, and this /summary cannot settle until a usable report is on the record.

Spawn ONE fresh `auditor` task (fresh context, blocking). Its task text must inline the same five labeled sections with their actual content: Approved plan (the plan text), Acceptance criteria (with AC-IDs), Starting state (starting commit + pre-existing dirty files), Final diff (the diff itself), Verification (exact commands + results). Require the canonical plain headed-text report: first line `VERDICT: PASS | NEEDS_FIX | BLOCKED`, then FINDINGS, ACCEPTANCE COVERAGE, OUT OF SCOPE, CHECKS RUN, REMAINING QUESTIONS. Never pass outputSchema on the auditor task.

Then copy the report VERBATIM into the typed Linear review comment (linear tool, action:"comment", kind:"review"). A NEEDS_FIX verdict still gets forwarded — it ends this summary attempt.
