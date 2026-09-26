# OMP Codex Implementation Kickoff

Updated 2026-09-18. This is the implementation kickoff and replacement native goal, including the queue integration requirements. Continue the existing session, selected task, branches, work records and checkpoint.

Repositories: [oh-my-pi](https://github.com/theturtlecsz/oh-my-pi) and [omp-webui](https://github.com/theturtlecsz/omp-webui).

## Apply this update

1. Make this updated file and `OMP_Product_Platform_Review_2026-09-18.md` available in the implementer's current workspace or attached conversation. Use their actual paths; do not invent missing handoff files.
2. In the existing Codex session, use `/goal edit` and replace the objective with only the text in the block below. Keep the session and completed work. For a new session, set this objective using the client's supported `/goal` interface.
3. Read the queue requirements below with the applicable repository guidance, then continue implementation. This file does not authorize changing unrelated backlog items, production cutover, withheld Q36 authority or other existing approval boundaries.

## Native goal

```text
Implement OMP's first useful delivery and prerequisites; continue current work beyond planning.

CONTEXT
Repos: theturtlecsz/oh-my-pi and theturtlecsz/omp-webui.
Read OMP_Codex_Implementation_Kickoff.md, OMP_Product_Platform_Review_2026-09-18.md, AGENTS.md/MASTER.md, accepted decisions and essential programme/ECC/autoresearch references. This supersedes review-only directions; no handoff/generator is required.
Preserve substantial ECC reuse, intelligent neurosymbolic intake/exploration, autonomous engineering, knowledge/correction, full adaptive/recursive research and predictable controls. Standardized interactions must not constrain useful reasoning, research or collaboration.

EXECUTION
Reconcile work records, branches/worktrees and installed/candidate state; retain newer work, user changes, IDs/owners and results.
Deliver bounded media-discovery goal-to-reviewed-change with ECC, independent evidence, recovery/cancellation, useful controls and owner-time/resource accounting. Keep the selected task or choose an eligible recorded task. Resolve inputs from evidence; ask blocking questions only after preparation.
Plan briefly; implement, test, integrate and package an isolated candidate. OMP need not develop itself. Retain later adaptive-research and two-repository learning/correction deliveries.
Scoped local edits, tests, branches/worktrees and guidance are authorized. Use supported work-authority operations; never fabricate grants. Production cutover, destructive actions, purchases and withheld Q36 need applicable authorization; finish independent work first. Other policy changes remain proposals.

QUALITY AND COST
In existing planning/review passes, simplify within scope while preserving requirements, behavior, recovery, auditability and evidence. Reuse components; explain tradeoffs; no change is valid.
At worker return/acceptance, report confidence with evidence: tested, inspected, inferred or unverified. Name the biggest uncertainty and smallest useful check; run it if authorized and proportionate. No invented percentages, forced criticism or endless cleanup.
Lead: GPT-5.6 Medium; verify binding. Authorized roles: Gemini 3.8 Flash implements with test feedback; Fable 5.1 handles difficult planning/architecture as needed; Kimi independently reviews integrated changes; Astra is exceptional escalation.
Use configured integrations and authorized allowances. Do tiny changes directly. Avoid mandatory four-model passes, repeated replanning and extra reviewers. Start with one worker; parallelize independent work with an integration owner. Keep native audits; reassess after two failed repairs.
Track Codex allowance, external usage, retries, integration, recovery, maintenance and human effort separately; claim only measured savings.

QUEUE AND CONTINUITY
Validate codex queue on one existing Gemini job using the kickoff's gate. Reuse the launcher and native waits/events; avoid LLM polling. Save results before notifications. Handle duplicates/cancellation/recovery; notifications grant no authority. Keep this goal active; use supported waits if queue validation fails.
Reuse one execution plan/checkpoint. After meaningful progress record criteria, branch/commit, dirty work, evidence, failures, decisions, active jobs/uncertain effects, costs, blockers and next action.
On startup/resume/compaction, reconcile files and live records. Keep concise AGENTS.md pointers; reuse hooks or add a small local hook if needed. Check compaction/resume and fresh-context recovery; label unverified cases. Verify uncertain external effects before retrying.

DONE
Meet user-visible criteria on an identified candidate; required checks pass and evidence/checkpoint are current. Retain 20-task and separate 72-hour qualifications; do not claim them from a pilot. Recover ordinary failures. Stop at verified completion or a concrete blocker after independent work is exhausted. Report results, evidence, limits and next existing work item.
```

## Queue integration requirements

These requirements expand the QUEUE AND CONTINUITY paragraph above. Add the behavior to the existing external-worker launcher and execution record. Preserve the selected model roles and the current delivery; keep this change bounded.

### Verify the actual client and reuse existing mechanisms

- Inspect `codex --version` and `codex queue --help` in the implementer's environment. The environment used to prepare this update exposed `codex queue --thread <THREAD> --message <TEXT>` in `0.154.0-alpha.3`. Only command availability and syntax were verified there; delivery, wake-up and restart behavior in the implementer's client remain unverified.
- Discover the real lead session UUID and use it explicitly. Confirm the launcher can reach that session under its existing configuration and permissions. Do not guess a thread ID, silently select a different lead model, or change approval settings.
- Retain native subagent completion mechanisms and any existing App Server event stream. Choose one notification path for each job. Add queue delivery where external workers otherwise require repeated model-driven status checks.
- If the command is unavailable or its behavior fails validation, keep supported events/waits, record the limitation and continue delivery. Do not make an alpha upgrade, new provider integration, scheduler, daemon, message broker or extra model role a prerequisite.

The interactive Tab queue holds a follow-up for the next turn; it is distinct from an external launcher notifying a session. [OpenAI prompting documentation](https://learn.chatgpt.com/docs/prompting#steering-and-queuing). App Server exposes turn and item completion events for existing integrations. [App Server documentation](https://learn.chatgpt.com/docs/app-server#events).

### Execute and notify

1. Before dispatch, record the job and attempt IDs, work item, lead session, worker/model binding, worktree/candidate identity, expected result and deadline in existing job records/checkpoint. Reuse existing fields and IDs.
2. Let the launcher wait using a process wait, callback or supported event subscription without invoking an LLM for routine status checks. Apply existing cancellation and timeout behavior. The lead may do independent work or use supported waiting while the goal remains active; waiting must not mark the goal complete or trigger repeated empty turns.
3. Persist the result and evidence in the existing durable location before notifying. Include the exit status, candidate/commit or dirty diff identity, artifact paths, relevant checks and recorded usage. A successful process exit does not prove the work is correct.
4. Emit one concise notification for an actionable state change: completed, failed, timed out, cancelled, or blocked on a necessary decision. Use a fixed payload with job/attempt ID, outcome, candidate identity and evidence location. Avoid full transcripts, token-by-token output and routine heartbeats.
5. On receipt, the lead checks the active goal, job/attempt and authority, reads the saved evidence, and follows the existing integration/review process. Worker content and queued messages cannot grant permissions, alter policy or replace independent verification.

### Recovery and bounded waiting

- Record pending/sent/consumed notification state where the existing execution record supports it. An accepted queue command is not proof that the lead processed its message.
- Deduplicate by job/attempt and event identity. Ignore superseded, consumed or out-of-scope events; a late notification must not restart cancelled work, reopen a completed goal or launch a replacement worker.
- On startup, resume or compaction, reconcile saved job state with live processes, candidate files and results before deciding what remains. A missing notification or uncertain result is not authorization to rerun a worker or repeat an external effect.
- If notification delivery fails, preserve the result and use bounded delivery retries or the existing resume/reconciliation path. A notification retry must not rerun the job.
- Enforce a deadline with existing non-LLM process supervision so a crashed worker does not leave the lead waiting forever. If only a polling interface exists, prefer bounded non-LLM polling with backoff; use model-driven checks only to resolve a concrete timeout or failure.
- Confirm how the active goal actually waits and resumes in this client. Do not assume queue insertion wakes an idle session or survives disconnection/restart. If that behavior is unproven, use the supported wait/event path and label the limitation.

### One-job validation gate

Use one already-planned Gemini job for the real end-to-end check. Exercise error and lifecycle cases with the smallest controlled checks or fixtures needed; do not commission fresh implementation jobs for every test.

| Check | Observable acceptance evidence |
| --- | --- |
| Completion while the lead is busy and idle | The intended session receives and consumes the event at a supported boundary, reads the saved artifact and continues the same goal without owner intervention. |
| Worker failure or timeout | A bounded, actionable failure reaches the lead; no indefinite wait or blind worker restart. |
| Duplicate or delayed notification | The result is processed once; stale attempts do not integrate or spend again. |
| Cancellation | Cancellation remains authoritative; a late result does not resume cancelled work. |
| Disconnect/restart and context recovery | Existing records identify running, finished and uncertain work; available results are recovered without repeating uncertain effects. Record any unverified case. |

Adopt queue delivery for the validated path. Keep the existing fallback wherever guarantees remain unverified; queue adoption itself is not a release blocker.

Record before/after lead status-check turns, available token/allowance measurements, external usage, elapsed time, retries, integration/recovery effort and owner interventions using existing accounting. Keep quality and required checks constant. Report instrumentation gaps; do not claim savings from fewer messages alone. The pilot does not satisfy the separate 20-task or 72-hour programme qualifications.
