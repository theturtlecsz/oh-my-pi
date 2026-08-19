AUDIT GATE (HOME-131) — the last auditor's report was REFUSED (missing: {{reasons}}). The audit ran, but its report was structurally unusable, and this /summary cannot settle until a usable report is on the record.

{{retry}}

The replacement task text must inline the same five labeled sections with their actual content: Approved plan (the plan text), Acceptance criteria (with AC-IDs), Starting state (starting commit + pre-existing dirty files), Final diff (the inline git diff — or, for large/binary committed work, a complete git manifest with exactly `Mode: git-range-sha256`, `Repository:` absolute path, `Start commit:` 40-hex, `Final commit:` 40-hex, `SHA-256:` 64-hex digest of `git -C REPOSITORY diff --binary --full-index START..FINAL --`), Verification (exact commands + results).

Then forward the report VERBATIM as the body of a `work` tool call, action:"append_evidence", kind:"audit". A NEEDS_FIX verdict still gets forwarded — it ends this summary attempt.
