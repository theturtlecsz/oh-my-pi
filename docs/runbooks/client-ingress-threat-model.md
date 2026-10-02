# Client ingress threat model (WFM-1)

## Purpose

This document specifies the threat model and write-surface audit for external client ingress into the Work Ledger. External client agents (such as Grok Bot automation on Cursor) access the WorkService via a dedicated loopback edge gate (`omp-work ingress serve` on `127.0.0.1:54323`) routed through Cloudflare Tunnel (`omp-client.czimmerman.io`).

Contract decisions 0019 (`0019-client-contract.md`) and 0020 (`0020-relayed-stop-release-and-client-oversight.md`) define the client interface and relayed intent boundaries. The code remains client-neutral (OMP-430).

## 1. Operation table

The client contract (`client.omp.dev/v1`) defines 17 operations under `/v1/workspaces/{workspace_id}/client`. The loopback edge gate enforces an allowlist that permits 8 operations and terminates all other 9 operations at the edge with HTTP 404 (code `not_found`) and header `X-OMP-Ingress: refused`.

| Name | Method | Path | Scope | Principal | Edge |
| --- | --- | --- | --- | --- | --- |
| project.list | GET | /v1/workspaces/{workspace_id}/client/projects | work.read, work.client | client | allow |
| project.context | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/context | work.read, work.client | client | allow |
| project.status | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/status | work.read, work.client | client | allow |
| project.decisions | GET | /v1/workspaces/{workspace_id}/client/projects/{project_id}/decisions | work.read, work.client | client | allow |
| mission.status | GET | /v1/workspaces/{workspace_id}/client/missions/{mission_id} | work.read, work.client | client | allow |
| evidence.inspect | GET | /v1/workspaces/{workspace_id}/client/evidence/{receipt_id} | work.read, work.client | client | refuse |
| stop.status | GET | /v1/workspaces/{workspace_id}/client/stop | work.read, work.client | client | allow |
| mission.submit | POST | /v1/workspaces/{workspace_id}/client/missions | work.client, work.mutate | client | refuse |
| mission.intake | POST | /v1/workspaces/{workspace_id}/client/mission-intake | work.client, work.mutate | client | refuse |
| stop.engage | POST | /v1/workspaces/{workspace_id}/client/stop | work.stop | client | allow |
| mission.pause | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/pause | work.client | client | allow |
| mission.resume | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/resume | work.client | client | refuse |
| mission.cancel | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/cancel | work.client | client | refuse |
| mission.reprioritise | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/priority | work.client | client | refuse |
| mission.scope.confirm | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/confirm | work.client | client | refuse |
| mission.scope.edit | POST | /v1/workspaces/{workspace_id}/client/missions/{mission_id}/scope/edit | work.client | client | refuse |
| decision.answer | POST | /v1/workspaces/{workspace_id}/client/decisions/{decision_id}/answer | work.client | client | refuse |

The starting edge allowlist permits read operations (`project.list`, `project.context`, `project.status`, `project.decisions`, `mission.status`, `stop.status`), emergency stop (`stop.engage`), and relayed pause (`mission.pause`). All other relays, evidence inspection, and mission submissions are refused at the edge.

## 2. Principals and token scope

Access control enforces strict boundary separation across three tiers of principals:

1. **Owner-controller client actor**:
   - Holds actor kind `client` in capability file `~/.config/omp/work-ledger/capabilities/owner-controller.json` (mode 0600).
   - Holds scopes `work.read`, `work.client`, and `work.stop`. It does not hold `work.mutate`, preventing arbitrary mission creation or unapproved mutations.
   - Designated via `<config_dir>/owner-controller.json` containing `{workspace_id, controller_actor_id, owner_signature}`, signed with the owner Ed25519 signing key (`designate_owner_controller`).
   - Authority permits relaying routine unsigned intent (`mission.pause`).
   - Bound actions: Cannot relay `release_stop` (Decision 0020 strictly requires an owner cryptographic signature). Cannot relay `answer_decision` for decisions with an `action_class` or decisions that broaden scope (Decision 0019 requires owner cryptographic signatures).

2. **Other clients**:
   - Client principals with actor kind `client` hold subsets of `client_scopes` (`work.read`, `work.client`, `work.stop`).
   - Lack designation in `owner-controller.json`. Any attempt to submit an unsigned relay is rejected with HTTP 403 `not_designated_controller`.
   - Bound to granted workspace and cannot access routes outside allowed scopes.

3. **Owner and admin routes**:
   - Direct owner mutations (`/v1/commands`, direct `release_stop`, `set_execution_state`, `admit_mission_budget`) require `actor_kind: "owner"` and owner host scopes (`work.mutate`, etc.).
   - Admin routes (`/v1/health/ready`, `/v1/health/live`, `/v1/work-items/**`, `ops credentials`, `ops controller`) remain strictly LAN-only (loopback `127.0.0.1:54322`).
   - The edge gate (`omp-work ingress serve` on port 54323) does not expose or forward any admin, health, or direct owner endpoints. Any attempt to reach `/v1/commands` or other LAN paths returns HTTP 404 with `X-OMP-Ingress: refused`.

## 3. Replay and forgery risks for relayed intent

Relayed intent carries owner instructions through external intermediaries. The architecture defends against four primary risks:

1. **Bearer theft**:
   - *Risk*: A bearer token is stolen from an external automation secret store.
   - *Gate control*: The edge gate limits method and path exposure to the allowlisted operations only. High-impact operations (`mission.submit`, `mission.resume`, `mission.cancel`, `decision.answer`, and `/v1/commands`) are refused at the edge. The gate rejects all query strings (`?detail=true`) and rejects bodies over 64 KiB with HTTP 413. Gate logs record only `caller=sha256(bearer)[:12]`, preventing token leakage in system journals.
   - *Service control*: The owner-controller credential only has `work.read`, `work.client`, and `work.stop`. It cannot perform direct store mutations or release a stop. Permitted mutations are fail-safe: engaging emergency stop (`stop.engage`) or pausing an active mission (`mission.pause`).

2. **Unsigned controller relays**:
   - *Risk*: An unauthorized caller attempts to relay intent without an owner cryptographic signature.
   - *Gate control*: Forwards requests only to configured routes, checking path UUID parameters.
   - *Service control*: `WorkService.execute_client` validates the controller designation file per request. If caller `actor_id` does not match the designated controller actor, unsigned relays return HTTP 403 `not_designated_controller`. Invalid signatures on signed payloads return `approval_required` / `owner_signature_invalid`. High-risk operations (scope broadening or classified decisions) require cryptographic owner signatures even from the designated controller.

3. **Replay of one relay body**:
   - *Risk*: An attacker captures and replays a valid signed or controller-authorized relay payload.
   - *Gate control*: Method and path validation prevents replaying across endpoints.
   - *Service control*: Every mutation envelope requires a unique `request_id` (UUIDv4). The database idempotency store identifies repeated requests; an identical `request_id` returns `outcome: "replayed"` without re-executing state transitions. Replaying the payload under a new `request_id` is guarded by state-machine transitions: a mission already paused returns `mission_transition_refused` (HTTP 409). Instructions record `received_at` timestamps and `source_message_ref` for non-repudiation.

4. **`detail` leakage**:
   - *Risk*: Internal execution diagnostics, worker IDs, worktree paths, or token budgets leak to external clients.
   - *Gate control*: The gate strictly refuses any request with a query string (`?detail=true`), returning HTTP 404 with `X-OMP-Ingress: refused` and never calling upstream.
   - *Service control*: `client_api.py` strips all diagnostic keys (containing `token`, `worker`, `worktree`, or named `operation_id`, `request_id`, `correlation_id`) across all depths unless `detail=True`. Upstream error bodies cap diagnostics to 8 entries.

## 4. Write-surface audit

The write-surface audit verifies edge restrictions before and after opening the public route:

1. **Local probes (OMP-490-s05)**:
   - Run `omp-work ingress probe` against loopback `http://127.0.0.1:54323`.
   - Probe without token: verifies HTTP 401 for allowed routes and HTTP 404 with `X-OMP-Ingress: refused` for unallowed routes.
   - Probe with owner-controller token: verifies HTTP 200 for allowed reads (`project.list`, `stop.status`), HTTP 400 for mutation probes (empty `{}` POST validates routing without mutation), and HTTP 404 with `X-OMP-Ingress: refused` for unallowed routes.
   - Socket audit: verify `ss -ltnH 'sport = :54323'` listens on `127.0.0.1:54323` only.

2. **Drain check and route opening (OMP-490-s06)**:
   - Safety check: run `omp-work drain check` to ensure no missions are in flight. If a mission is active, abort with no configuration changes.
   - Install cleanup trap: backup `/etc/cloudflared/config.yml` to `/etc/cloudflared/config.yml.bak490`. On failure, the trap restores the backup and restarts cloudflared.
   - Add ingress rule for `omp-client.czimmerman.io` pointing to `http://localhost:54323`.
   - Validate ingress configuration, route DNS entry, and restart `cloudflared.service`.

3. **Public probes (OMP-490-s06)**:
   - Poll public endpoint with bounded wait until HTTP 401 is received.
   - Execute `omp-work ingress probe` against `https://omp-client.czimmerman.io` with and without credentials.
   - Confirm public route matches local gate behavior: only allowlisted operations respond; unallowed paths return HTTP 404 with `X-OMP-Ingress: refused`.

4. **Live stop.engage and owner release (OMP-490-s06)**:
   - Execute live mutation test through public route: POST `stop.engage` with a unique `request_id` and test reason.
   - Verify state transition: `stop.status` reports `"state": "stopped"`.
   - Clear stop state: owner issues direct `omp-work stop release` over LAN/direct config.
   - Verify state clearance: `stop.status` reports `"stopped": false`.

5. **Narrowing from attributed traffic (OMP-490-s08)**:
   - Read `omp-work-client-ingress` journal logs filtered by caller fingerprint `F=sha256(bearer)[:12]`.
   - Identify read operations actually invoked by the controller client.
   - Narrow `client-ingress.json` config: remove uncalled read operations; retain only exercised read operations plus the mandatory control operations (`mission.pause` and `stop.engage`).
   - Restart `omp-work-client-ingress` service with narrowed allowlist.
   - Verify all evidence files are clean of token leakage (`grep -F`).
