# 0018 — Mission events

## Problem

1. Mission progress lives only in applied domain events. A client cannot read one ordered feed of what changed on a mission, or resume that feed from a cursor.
2. Nothing registers a client's subscription, push destination, or durable cursor, and nothing says who may read or change another client's subscription.
3. OMP-406 alerts and the daily digest need the same delivery path, while the alarm commands and their environment stay as they are.

## Decision

1. **Derived on read.** Mission events are derived on read from omp_audit.domain_events (no migration): at most one per source event, carrying its sequence; a cursor is a domain sequence.
2. **Triggers.** set_mission_status approved→running = mission.started; →blocked/completed/failed = that type (mission.blocked, mission.completed, mission.failed); other non-abandoned transitions = mission.progressed trigger "stage_change"; complete_work of a mission-linked item = mission.progressed "operation_completed"; create_decision whose mission_id parses as a UUID = decision.required; link_mission_work crossing a fraction (default 0.8) of mission budget usd = budget.threshold_reached; record_finding at or above a severity (default high) = important_finding. Thresholds in `<config_dir>/mission-events.json`. abandoned emits nothing.
3. **Identity.** mission_event_id = `uuid5(uuid5(NAMESPACE_URL, "work.omp.dev/v1/mission-events"), f"{source_event_id}:{type}")`.
4. **Evidence and scopes.** record_finding evidence_refs are opaque strings like decision evidence_refs; in events each becomes {"kind": "evidence", "ref": <string>}. record_finding needs work.execute; the 3 subscription commands need work.read.
5. **Subscriptions.** Subscriptions and cursors are domain events; a new cursor starts at the watermark. Put on an existing id alters it; client_id fixed at creation; delete is final.
6. **Clients.** A client principal is any principal with work.read. Acting on another client's subscription needs scope work.events.admin. GET event-subscriptions without client_id: admin gets every client's, anyone else only their own; an admin may put, delete or advance any subscription.
7. **Push runner.** The `omp-work events push` runner authenticates with a new `event-push` capability (actor_kind automation, scopes work.read + work.events.admin). OWNER_SCOPES stays unchanged.
8. **Ops streams.** OMP-406 alerts and the daily digest are subscription streams "ops.alarm" and "ops.digest", each its own subscription and durable cursor, delivered by the same runner (registered, destination-checked, signed). ops.alarm: alarm rules replayed from sequence 0 each run; only alerts past the cursor are sent; cursor advances past each delivered alert. ops.digest: one digest per completed UTC day, key `digest:{ws}:{day}`; cursor advances to that day's last sequence only after delivery, so a failed digest is resent next run with the same key.
9. **Alarms stay.** `omp-work alarms init|run|digest`, their state file and OMP_GROKBOT_ALERT_* stay unchanged; new delivery never uses them.
10. **Outbound policy.** Outbound policy, at registration and before each delivery: https, no userinfo, host in `<config_dir>/push-destinations.json` allowed_hosts (missing = refuse all), every resolved address passes egress_policy.blocked_address.
11. **Signature.** `X-OMP-Signature: v1=<hex HMAC-SHA256(key, idempotency_key+"\n"+body)>`, key = HMAC-SHA256(`<config_dir>/push-signing.key`, "omp-push-subscription\0"+subscription_id); a push to a push_url that `<config_dir>/push-bearer.json` maps to a token file also carries `Authorization: Bearer <token>` (OMP-514); other pushes carry no bearer; the token never appears in logs, results or events. Idempotency-Key = mission_event_id; alerts keep "{event_id}:{kind}". 3 attempts; cursor never passes an undelivered item.

## Consequences

- Reading mission events projects omp_audit.domain_events. There is no migration and no stored mission-event table.
- A cursor is a domain sequence. It starts at the watermark, and it never moves past an item that was not delivered.
- The push runner is `omp-work events push` with the `event-push` capability. It does not call `omp-work alarms init|run|digest` and it does not read OMP_GROKBOT_ALERT_*.
- `omp-work events push` reads `<config_dir>/push-bearer.json` (`{"token_files": {"<push_url>": "<absolute token file path>"}}`) and sends that URL's token with X-OMP-Signature and Idempotency-Key; the Cursor automation is not changed.
- OWNER_SCOPES stays unchanged. work.events.admin is not added to the owner capability.
