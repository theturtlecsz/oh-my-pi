# S2.2: resume a persisted, unfinished controller turn

Continue OMP-246 after the queued-intent/frozen-candidate controller slice merged
at `3831313c2e8c7f3ff00d86205ba01eb994f8aa30`. Its four hosted installed cases passed
against manifest `4fbc05d75fe7f950c95b242a8daff2f5851c3c9c9a26e8653912f451a3c2dfc0`;
that is not evidence for this next boundary. WorkService owns progress and
acceptance; this file specifies one bounded implementation slice.

## Failure to reproduce first

Hold the first actual provider HTTP request before any response bytes. Wait for
the exact execution custom message to appear in system-written session JSONL.
Require no later assistant, user, tool result, tool-start marker or other execution
intent. Kill the real installed controller with SIGKILL and restart the same
session without a repair prompt. Capture valid recovery preflight, the persisted
entry/identity/branch, service grant/version, local files/HEAD/remote refs and raw
RPC. The baseline must demonstrate a persisted intent whose model turn does not
resume; do not relabel a preflight refusal as this failure.

## Smallest accepted implementation

1. Preserve the actual active-branch custom-message entry ID along with its stable
   outbox identity. Missing injection continues through the existing replay path.
   Matching persisted injection suppresses another custom message, but must not
   automatically suppress a safe unfinished model turn.
2. Add one narrow extension-to-AgentSession scheduling action. Request binds
   session, entry, active leaf and prompt generation, with a host callback that
   revalidates authority immediately before dispatch. Results describe scheduling
   or refusal, never durable consumption or workflow completion.
3. Reuse AgentSession's existing prompt preparation and tracked scheduler. Do not
   expose raw Agent access or copy the prompt pipeline. Resume existing context
   without inserting the execution custom message again; retain system-prompt,
   before_agent_start, pending context notices, usage and maintenance behavior.
4. Dispatch only after the actual session_start lifecycle finishes. Use an
   explicit lifecycle completion gate, not a timer guess. Recheck disposal,
   streaming/in-flight work, retry/abort/compaction/handoff, queued owner input,
   branch/entry and generation before and after awaited authority/preparation.
5. SessionManager owns persistence/error handling. Flush and fail on persistence
   errors before treating a live entry as durable. Coalesce the same request and
   refuse conflicting requests; clear only process-local scheduling ownership.
6. Initially accept only an unfinished no-tool suffix. A successful terminal
   assistant means that conversation turn settled, not that workflow progressed.
   Unresolved tools, conflicting later turns, missing/compacted anchors and stale
   authority refuse precisely. Never delete tool history, fabricate an aborted
   result, reinject a replacement assignment or revive terminal grants.
7. Wire the action through actual extension runtime/loader/runner and supported
   mode action sets. Restricted/embedded hosts may explicitly report unavailable;
   they must not claim recovery. No new RPC command, WorkService schema, contract,
   migration, controller framework or durable sidecar queue is needed.

## Observable checks

- Fresh candidate issues one resumed provider request from existing context and
  preserves one original execution custom message and the original reservation.
- After that response settles durably, another restart does not repeat the same
  turn. Preserve grant, local content/HEAD and remote references.
- A separate progress variant performs a real supported service operation and
  checks its transition/receipt. Later next-stage work may legitimately run; prove
  no duplicate original operation rather than forbidding all future requests.
- Component coverage uses real AgentSession/SessionManager and protects request
  coalescing, stale identity after awaits, terminal authority, pending tools,
  persistence error, held startup lifecycle, restored preparation notices and
  queued owner input. No source-text assertions or fake session engine.
- Run existing controller/isolation cases against each fresh matching artifact,
  focused affected tests, bun check and independent review. Required CI retains
  actual manifest, JUnit and fault/effect evidence. Skips are not qualification.

The implementation also preserves inherited extension-context fields during
workspace relocation and retains the existing unreadable-operation-journal guard
at persisted dispatch. Real-runner smoke must exercise these integration paths.
Print/text and JSON modes wait for tracked recovery before final output/disposal;
explicit owner input and clients owning turn dispatch take precedence. Existing
print-mode test fakes must implement the required idle-wait API, with their output
assertions retained. Public extension documentation describes scheduling results
and refusals without claiming a durable consumption receipt.
The service smoke models persisted-turn wake separately from prompt injection and
new reservations. It retains distinct fresh-reservation, duplicate, malformed
journal and budget-exhaustion cases; simulated settlement is never represented
as installed AgentSession execution.

## Later task boundary stays separate

Task agents currently run in-process despite the `runSubprocess` name. The later
test must start a real child AgentSession/tool and kill the shared controller/task
process. An independently killable worker PID does not exist in this path. Cold
revival alone does not recreate a lost parent task promise or prove its effect.
First reproduce actual child/parent loss; any subsequent task-call/result binding
must reuse existing lifecycle, reviver and finalization code, with real persisted
correlation and original parent tool-call identity. That implementation is excluded
from this no-tool controller slice until its own reproduction and plan are ready.

Only software-crash durability is in scope. Model-turn settlement is not native
acceptance, arbitrary exactly-once effects, provider readiness, or deployment.
Keep full S2.2 and OMP-246 open until their remaining boundaries are proved.
