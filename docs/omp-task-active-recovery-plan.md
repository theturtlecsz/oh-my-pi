# Actual task-active shared-process death reproduction

Read-only design, 2026-09-08. Baseline: qualified `09858a8b67`, installation `/home/thetu/.local/state/omp-stabilization/releases/persisted-09858a8b67`; manifest SHA256 independently read as `93e0dcd803c656da945843568a723ba959f638d0468167b2af66dcb177b71444`. No new runtime result is claimed here. Existing six installed cases stay unchanged.

## Exact supported task invocation

Use current disposable WorkService/local Git/installed RPC CLI setup. Change only this new case's disposable agent configuration, before normal setup computes its TCB:

```yaml
modelRoles:
  audit: qualification/local-recovery
  default: qualification/local-recovery
  smol: qualification/local-recovery
  task: qualification/local-task
advisor:
  enabled: false
async:
  enabled: false
task:
  batch: false
  isolation:
    mode: none
  prewalk: false
  maxRecursionDepth: 1
tools:
  xdev: false
```

Add `local-task` to the existing `qualification` provider in `models.yml`, with the same local base URL, `openai-completions` API, fixture-only key, `reasoning: false`, text input, zero costs, context window and token cap as `local-recovery`. Leave parent CLI explicitly on `--provider qualification --model local-recovery`. This makes `request.model == "local-task"` the child request discriminator; do not infer child identity from whether `work` is present in its tool list.

Parent provider response emits exactly one ordinary function call:

```json
{
  "id": "task-active-call",
  "type": "function",
  "function": {
    "name": "task",
    "arguments": "{\"name\":\"RecoveryTask\",\"agent\":\"task\",\"task\":\"<static fixture assignment text>\"}"
  }
}
```

Put assignment in `python/omp-work/tests/fixtures/task-active-assignment.md` and load it as provider input. Suggested content: inspect `result.txt` for the already authorized work, report its actual contents via normal task completion, and make no edits or workflow/audit calls. The first response is held before the child can do any of this. Neither task invocation nor assignment contains an audit verdict. No `model`, `mode`, `sync`, `async`, `blocking`, or `resume` field belongs in this task call. Omit `isolated` because isolation is disabled; omit `effort` because its wire field is disabled by default. No new role/extension/tool files are needed.

Source anchors:

- `task/types.ts:150–177, 194–300`: flat `name?`, `agent`, `task`, optional output schema/schema mode; batch has `context`, `tasks[]`. Runtime accepting old flat shapes under batch is not a reason to ignore advertised schema.
- `config/settings-schema.ts:4575`: `async.enabled` defaults true; `:4922`: `task.batch` defaults true. Both need explicit false for this controlled synchronous case. Isolation defaults `none` near `:4793`; recursion setting near `:4990` supports 1.
- `task/index.ts:722–755, 1234–1274`: async disabled selects actual synchronous fan-out and waits for the task result. `blocking` is agent-definition metadata, not a task argument. No need to invent a blocking agent.
- `task/agents.ts:60–77`: bundled `task` uses `@task` and no default prewalk. `task/structured-subagent.ts:282–297` resolves model role/settings before execution. Explicit `modelRoles.task` prevents accidental external model routing.
- `task/discovery.ts:67–126`: discovered roles can override bundled ones. Confirm live RPC snapshot says `agent: task`, `agentSource: bundled`; do not silently accept an unexpected role override.

## Minimum fixture delta

Extend `test_installed_execution_recovery.py` with one new scenario (`task-active`) and one provider branch, plus the static assignment file. Reuse `InstalledRelease`, `RpcProcess`, `_process`, `_health`, `native_postgres`, existing authority setup helper, and local repository/remote setup. Do not fork the whole fixture or refactor all six cases into a new harness.

Keep this scenario's observations/assertions separate from existing `persisted` no-tool suffix assertions: here the parent **must** have a persisted assistant task call and unresolved tool start. It is a different boundary, not another row of the no-assistant case. Branch to task-specific capture/restart logic before existing controller-specific assertions. Existing provider behavior/config remains byte-for-byte equivalent for the six existing cases.

In provider dispatch, classify `local-task` **before** existing `if no work tool` fallback and before generic `restarting` handling. Save request, set a dedicated child-held event, and wait without sending HTTP headers/body. Capture a request ordinal under a lock: parent/child/title requests can overlap, so `append` then `len(calls)` without synchronization can mislabel evidence files. Release the lock before waiting. Record per-request model, arrival timestamp, phase and whether any response bytes were sent.

Parent `local-recovery` with normal parent tools emits this one task call. Auxiliary title/label requests keep current harmless text response and separate counters. Never emit a second task call to compensate for a failed launch. Initial live `get_state` can run while the task is blocked. Optional `set_subagent_subscription(level="events")` adds raw frames, but do not depend on winning its race: `get_subagents` caches lifecycle state even when subscription defaults `off`.

## Pre-kill barrier: all evidence required

1. Child HTTP request reaches provider with `model == local-task`; zero child response bytes sent. Save raw request; ensure assignment text appears and its actual tools include task completion support (`yield`). A parent tool-call request alone is not a running child.
2. RPC `get_state` reports original main `sessionId`, actual relocated `sessionFile`, and `isStreaming: true`. Preserve returned `dumpTools.task.parameters` and confirm the flat supported shape.
3. RPC `get_subagents` yields exactly one matching running snapshot: actual `id`, `agent`, `agentSource`, `sessionFile`, `parentToolCallId`, `index`, task/assignment/progress where available. Match `parentToolCallId` to the actual persisted parent assistant toolCall id, not merely the fixture's expected transport spelling. In fresh state requested name should allocate `RecoveryTask`; use returned id as truth.
4. Poll actual parent JSONL until it contains original `work-execute` identity, matching assistant `task` call and `custom` entry with `customType: tool_execution_start` for that call. Confirm no matching `toolResult` and no successful terminal parent answer after it. Important shape: tool start is a **custom entry**, not `entry.type == tool_execution_start`.
5. Read actual child file from RPC snapshot. Require header `type: session` with distinct child session id, cwd equal to real execution workspace, actual `session_init` (`agent`, `modelRole`, `resolvedModel`, `tools`, task, spawn contract), and initial task message. Require no assistant answer, tool start, tool result or yield yet. Poll complete JSONL lines; do not mistake a partial append read for corrupted recovery state.
6. Read grant/workflow through existing service API. Require same active grant/item/revision/reservation as bound execution message, unchanged phase/version since launching task, no pending checkpoint delivery that could independently wake parent, no changed candidate, and no unresolved service mutation from setup. Preserve full state responses.
7. Save real workspace HEAD, `result.txt`, dirty paths, and local bare remote refs. No file effect is expected at this barrier. Save parent and child journal snapshots before process termination.

Useful existing sources: `task/structured-subagent.ts:348–356` puts child artifacts beside actual parent JSONL (remove `.jsonl` extension); `task/executor.ts:2758` chooses `<artifactsDir>/<id>.jsonl`, `:3267–3279` emits lifecycle identity, `:3290–3307` persists `session_init`. `task/output-manager.ts` seeds used IDs from disk on resume; a later `RecoveryTask-2` would mean a fresh allocation, not same-task revival.

## Process topology and fault

`_process` uses `start_new_session=True`. Its `Popen.pid` becomes the Bun launcher PID/group leader after `bin/omp` shell `exec`. Launcher `session-system/runtime/run.ts:212` uses `Bun.spawn` to start installed `packages/coding-agent/src/cli.ts`; this is another PID in the same process group. `task/executor.ts:2707, 3209` executes the child AgentSession **inside that CLI PID**.

Before SIGKILL, capture process ids, parent ids, group/session ids, process-start identity, command line and executable/cwd paths for launcher group (Linux `/proc` through Python file APIs is sufficient). Identify actual CLI PID by installed CLI argument and ancestry. Label separately:

- `launcherPid`, `processGroupId`;
- `cliPid` hosting main AgentSession **and** task AgentSession;
- child **session/registry** ids, with no invented task PID;
- service/provider/PostgreSQL processes outside killed group.

Use existing `os.killpg(cli.pid, signal.SIGKILL)` where `cli` is `_process` handle. Require `wait()` returns `-SIGKILL`; verify original CLI process-start identity no longer runs (brief zombie observation is not a live worker). Retain process-tree before/after and raw stdout/stderr. A killed launcher alone, graceful abort, killed bash subprocess or wrapper pid is not this fault.

## Restart and predicted current behavior

Restart same admitted launcher/state and **actual relocated parent session file**, same local config/service/repository, with no owner prompt, `/retry`, `/execute resume`, `hub` send, or manual child revival. Keep original blocked HTTP handler separate from post-restart requests; releasing its disconnected response later is cleanup, not recovered activity.

Observe bounded window (e.g. 10 seconds after successful RPC `get_state`), retaining raw startup notifications, parent/child request counters, journals, service state, local effects and group topology. A second restart can establish the same preserved refusal/no-duplicate state.

Expected from current code, **not yet an executed result**:

- `host.ts:2037–2078` selects matching persisted execution intent and requests continuation.
- `agent-session.ts:3503–3511` calls `describePendingToolCalls` before other suffix decisions. Parent unresolved `task` should produce `pending-tools` refusal, displayed by host as `Execution recovery skipped: Previous session ended while 1 tool call remained pending: task <actual-call-id>...`.
- Parent stays idle; no child provider request or reissued parent task; original child journal stays preserved. Grant and all effects stay unchanged. This is explicit safe refusal and **absence of automatic task recovery**, not a successful resumed task or a regression in the qualified no-tool seam.

`get_subagents` after restart may be empty: `RpcSubagentRegistry` is an event-bus view populated in the current process (`rpc-subagents.ts:103–164`), not a disk-recovered global roster. It is not proof of child deletion or terminal state. `get_subagent_messages` rejects unknown ids/files in this fresh RPC view (`:249–265`); read preserved child file directly for this observation instead of treating that RPC rejection as corruption. Do not trigger `hub`/cold revival just to populate a nicer report.

## Existing correlation and limits

Durable today: parent assistant call id and task arguments/name; parent tool-start diagnostic; child filename/header/`session_init`/initial assignment. Live today: RPC lifecycle/progress/snapshot join actual child id and file to `parentToolCallId`. Save that real join as **test observation**, not as a synthetic runtime journal. Child `session_init` has no `parentToolCallId` or workflow continuation binding (`session/session-entries.ts:210–238`); its header's optional `parentSession` must not be assumed populated. Current observer evidence can establish the specific child that died; it does not establish general production exactly-once parent-result reconstruction.

Failure attribution must distinguish missing/disabled task tool, wrong role/model, provider fallback, non-flat schema, unexpected async result, absent child input, startup preflight refusal (TCB/HEAD/revision/authority), true pending-tool refusal, unreported silence, and unexpected fresh task allocation. If any pre-kill predicate fails, mark barrier not reached and preserve evidence; never edit phase, grants, journals or child state to force it. If current restart differs from prediction, record actual outcome before designing a production change.

This reproduction qualifies one synchronous, non-isolated task active before first answer/effect. It does not qualify async completion delivery, child-yield/parent-result crash gap, effectful child retry, separate-worker PID recovery, native audit acceptance, or paid-provider readiness. Preserve real negative before adding a recovery fix/framework. Add required CI only with the intended observable contract and honest scope; an expected refusal can pass a refusal-safety test without closing automatic task recovery.
