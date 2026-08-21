# Rule map — where every removed /summary instruction now lives (OMP-54)

Every instruction removed from the pre-OMP-47 monolithic SKILL.md maps to a
deterministic owner: WorkService, the workflow host, a test, or retained
policy. Nothing was dropped silently.

| Removed instruction | Now owned by |
| --- | --- |
| Build the auditor task from the PLAN PACKET (plan body, criteria, receipt SHA, Final commit) | WorkService `seal_audit_manifest` composes the complete five-section task from stored ledger bytes; `work get_work` renders it verbatim (`store.py::_compose_audit_task`, host `renderAuditTask`) |
| Cite `Plan receipt SHA-256:` / `Final commit:` lines in the task | Sealed into the manifest body by the service; the task hash pins every byte |
| Compare packet Final commit against the bound candidate before spawning (obs #135) | `reserve_auditor_launch` + `settle_auditor_launch` recheck full identity under lock; drift refuses/supersedes with a typed event |
| Exactly ONE auditor; one bounded replacement; two unusable reports exhaust the attempt | `close_attempts.launch_count`/`accepted_report_count` budgets (3/2) enforced transactionally; `budget_exhausted` is a terminal state |
| Forward the report VERBATIM as `kind:"audit"` evidence | Deleted: audit receipts are minted ONLY by the settle transaction; external audit appends are refused (`store.py::_append_evidence`) |
| Bridge receipt claims/commits/releases (process-global audit bridge) | Deleted (`workflow/audit-bridge.ts` removed); the service is the only authority; bookends is reserve/settle transport |
| Report shape validation (VERDICT first line, five sections, JSON shapes) | WorkService `normalize_auditor_report` (`semantics.py`) with stable refusal codes; tested in `test_workflow_service.py` |
| "A gate refusal outranks reminder text" (obs #142) | Retained policy (`ledger-close.md`) — refusals are now typed events with legal next actions |
| Host adds the `Session review` prefix and plan hash; require success:true | Unchanged host behavior; review append additionally requires the audited live attempt and mints a delivered checkpoint (`store.py` closeout branch) |
| Closeout request after review | `request_closeout` requires `attempt_id`, an audited attempt, and zero pending deliveries; still routine self-confirmed after literal /summary (OMP-23) |
| /done closes; /summary never does | Unchanged (HOME-114 host lock); `complete_work` additionally requires a fresh single-use /done authorization reference |
| Stop-hook budget prose ("one replacement per attempt…") | Deleted from prompts; the service refuses over-budget spawns with the remaining counts in the event |
| Command hygiene, obs #141/#131/#99 grounding rules | Retained policy (`session-review.md`) |
| Phase 1/2 skill invocation, law-28 section format | Retained policy (`session-review.md`) |
| Loop charter format, obs #54/#70/#84/#87/#90/#91/#101 | Retained policy (`loop-charter.md`) |
| HOME-114 literal gate, plain-language questions, portability (obs #63) | Retained policy (`policy.md`) |
| Same-session found-and-fixed filing | New: `ledger-close.md` triage step + `same_session_found_fixed` receipts validated by WorkService at append AND at `/done` completion |
