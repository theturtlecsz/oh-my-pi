# 0014 — Agent stop control

## Problem

1. Automated sessions and background workers operate concurrently across workspace tasks. Runaway, erroneous, or compromised agent activity requires an immediate, reliable kill switch to prevent state divergence or wasted budget without terminating processes unsafely or rebooting the host.
2. Emergency halt must be accessible to both monitoring callers (e.g. grokbot) and the human owner, but resuming operations after an emergency must be strictly restricted to the human owner.
3. Units must halt within a predictable, bounded window (30 seconds), unit timers must not restart halted units, and commands issued while stopped must be cleanly rejected by the ledger while reads and terminal transitions remain permitted.

## Decision

1. **Stop operations and status read.** WorkService adds two commands, `engage_stop` and `release_stop`, each carrying a required payload `StopReasonPayload {reason: str}` (1–500 characters). A read endpoint `GET /v1/workspaces/{workspace_id}/stop` returns `StopStatusView {workspace_id, stopped, reason, changed_at, changed_by_actor_kind}`; the nullable fields are `None` before any stop event.
2. **Caller authority and capability separation.** `engage_stop` requires the `work.stop` capability scope, held by the owner and by dedicated monitoring callers (`grokbot`); `release_stop` requires `work.approve` and is strictly forbidden (HTTP 403) unless `actor_kind == "owner"` (owner-only release). The stop status read route requires either `work.read` or `work.stop`. `SecurityPolicy` declares `stop_client_scopes: ("work.stop",)` and the owner host scopes gain `work.stop`.
3. **30 s halt time.** Sessions poll the stop status every 5 seconds. Host units (flood, flood-operator, continuous-admit, media-watch-agent, and robomp) are managed by a host watcher that polls every 5 seconds with `TimeoutStopSec=20`, ensuring all agents and units halt within 30 seconds of stop engagement. Unit timers are guarded with an `ExecCondition` invoking `omp-work stop check`: exit 1 when stopped prevents unit start, and exit 255 when the service is unreachable defers start to let `Restart=` retry.
4. **Refusal while stopped.** While stop is engaged, the work ledger refuses every new mutation command with error `agent_stop_engaged`, excepting only `engage_stop`, `release_stop`, and setting execution state to `paused`, `stopped`, or `canceled`. Stale-source checks (OMP-89) pass `engage_stop` through like halt commands. Read operations remain fully functional.
5. **State durability without migrations.** Stop state is derived from the latest applied `engage_stop` or `release_stop` domain event on the workspace aggregate in `omp_audit.domain_events`, so the state survives work service restarts without introducing database migrations.

## Consequences

- The contract bundle declares the `GET /v1/workspaces/{workspace_id}/stop` read, the `engage_stop` and `release_stop` commands, the `work.stop` capability scope, and the `agent_stop_engaged` error code.
- `SecurityPolicy.stop_client_scopes` is `("work.stop",)` and `owner_host_scopes` includes `work.stop`.
- WorkService enforces the scope access and the owner-only release; the store derives the status from the event log.
