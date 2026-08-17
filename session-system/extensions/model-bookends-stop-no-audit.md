AUDIT GATE (HOME-131) — the audit has not run yet, and this /summary cannot settle until an independent audit has run and its report is on the record. Spawn the single auditor task now.

Spawn EXACTLY ONE `auditor` task (fresh context, blocking). Its task text must inline five labeled sections, each carrying the actual content — never a pointer like "attached below": Approved plan (the plan text), Acceptance criteria (with AC-IDs), Starting state (starting commit + pre-existing dirty files), Final diff (the diff itself), Verification (exact commands + results). Do NOT include your own self-assessment, conclusions, or confidence claims — the auditor inspects the work cold.

Then wait for its verdict-structured report and forward it VERBATIM as the body of a `work` tool call, action:"append_evidence", kind:"audit". A NEEDS_FIX verdict still gets forwarded — it ends this summary attempt; fixes happen afterward and the next owner-entered /summary spawns a fresh auditor.
