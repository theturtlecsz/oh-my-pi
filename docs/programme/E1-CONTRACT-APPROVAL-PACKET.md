# E1 WorkService contract approval packet — September 18, 2026

## Decision requested

Approve exact `work.omp.dev/v1` contract digest
`a4da1fef0a7b538e0f31d64246af2997ea44210bc7ffd173fc1b46ee141947d0`
under allowed approval anchor `OMP-279` so disposable E1 installation and browser-control
lifecycle qualification can pass WorkService bootstrap.

This approval updates only tracked `python/omp-work/src/omp_work/contracts/v1/approval.json`.
It does not install or activate source, restart a service, migrate the live database, admit or
stop work, grant Q36 authority, approve a purchase, publish a branch, or authorize production
cutover. Those gates remain separate.

## Frozen identities

| Item | Identity |
| --- | --- |
| Last approved contract source | commit `4818859542ead3ba21fc1c6f9a8a6561f5f2800f` |
| Last approved digest | `ee709ca67b3066c4c9e2fec32655ba2704a59ae07108f70fbffb3c9985128546` (`OMP-247`, 2026-09-06) |
| Current contract source | commit `96f511b4b67d08bb5d8b5bcb3662515691679af2` |
| Approval worktree/index | `/home/thetu/oh-my-pi-worktrees/e1-installed-fixture` at commit `625bfdb4bac2cfd301dbcde5f583f55074d13f35` |
| Approval index tree | `b9c998f7dca5515c165500aedaa42a8306a52a74` |
| Current digest | `a4da1fef0a7b538e0f31d64246af2997ea44210bc7ffd173fc1b46ee141947d0` |
| Failed staged release | manifest SHA-256 `e38f672ac117046c456c8545748d90c2e6393e25e9c8b7619797d94ba3e87c2d` |

The approval index differs from contract source commit `96f511b4…` only by the committed
installed-qualification test staging. Its contract directory and TypeScript client contract
constant are byte-identical to `96f511b4…`. Local package validation may update generated
egg-info outside the contract manifest; the approval command re-hashes only exact manifest bytes
and refuses any contract drift.

## Exact public contract delta

No read, command, error code, or capability scope was removed. No capability scope was added.

Added reads:

- `GET /v1/work-items/{key}/revisions`
- `GET /v1/work-items/{key}/revisions/{revision_selector}`
- `GET /v1/receipts/{receipt_id}`
- `GET /v1/workspaces/{workspace_id}/work-items`
- `GET /v1/workspaces/{workspace_id}/events`
- `GET /v1/workspaces/{workspace_id}/repositories`
- `GET /v1/workspaces/{workspace_id}/provider-accounts`
- `GET /v1/workspaces/{workspace_id}/provider-accounts/{account_id}`
- `GET /v1/workspaces/{workspace_id}/budget-scopes`
- `GET /v1/workspaces/{workspace_id}/budget-scopes/{scope_id}`

Added commands:

- Stage lifecycle: `reserve_stage_launch`, `handoff_stage_launch`, `settle_stage_launch`,
  `cancel_stage_launch`, `reconcile_stage_launch`.
- Stage preflight: `begin_stage_preflight`, `admit_stage_preflight`,
  `cancel_stage_preflight`, `record_stage_preflight`, `reconcile_stage_preflight`.
- Budget authority: `create_budget_scope`, `reserve_budget`, `claim_budget`, `settle_budget`,
  `cancel_budget`, `expire_budget`, `issue_frontier_exception`, `put_provider_account`.

Added typed errors: `budget_exhausted`, `preflight_intent_active`.

Normative decision `0009-native-foundation-reads-and-event-cursor.md` adds exact historical
revision and receipt reads, bounded workspace enumeration, repository enumeration, and a
commit-order event cursor. Same-workspace native event writes serialize through the central
store's transaction advisory lock; arbitrary raw SQL writers remain outside that guarantee.
Candidate readers do not gain unrestricted event payload access.

The same digest also binds stage launch, provider-account, hierarchical budget, reservation
expiry, stage-budget binding, preflight usage, durable preflight intent, dispatch admission, and
uncertain-preflight reconciliation models. Persistent schema additions are migrations
`0024_native_stage_launches.sql` through `0032_stage_preflight_reconciliation.sql`. Approval
therefore covers the whole digest, not only decision 0009.

Generated `schema.json` and `api-schema.json`, Python request/response models, service/store
dispatch, TypeScript client types and `WORK_CONTRACT_SHA256` are bound to this digest. Formatting
expansion accounts for much of the generated-schema line delta; approval remains byte-exact.

## Evidence and limits

Tested now:

- Recomputed prior manifest digest from commit `4818859542…`: exact match to tracked approval
  `ee709ca6…`.
- `python -m omp_work validate` on clean commit `625bfdb4…`: valid current digest `a4da1fef…`;
  generated schemas and API reference closure agree when approval is not required.
- Required installed qualification collected two browser lifecycle tests with zero skips and
  reached disposable PostgreSQL/WorkService bootstrap. Both stopped before daemon/browser
  admission because `validate_bundle(require_approval=True)` detected the old approval digest.

Inspected:

- Contract array delta contains ten reads, eighteen commands and two errors added; no removals
  and no scope changes.
- Contract-changing commits from `8379a645…` through `7f38a8b…` and migrations 0024–0032.
- Clean approval flow validates the allowed issue, prints prospective JSON, requires exact digest
  entry on a TTY, re-hashes before atomic replacement, validates the approved bundle, and restores
  prior bytes on failure.
- Independent Kimi review passed frozen E1 source trees; this was source review only.

Unverified until approval and rerun:

- Actual browser → Web daemon → installed wrapper → WorkService Pause/Stop lifecycle.
- Committed response loss and same-operation reconciliation, stale browser request refusal,
  late completion fencing, controller/daemon restart, reconnect, and process-group termination.
- Live database migration, production compatibility, rollback after activation, and cutover.

Largest material uncertainty is installed behavior after migrations and bootstrap. Smallest useful
check is the already-staged two-test disposable lifecycle run immediately after approval, followed
by the 25-case installed recovery/isolation regression. Approval enables that evidence; it does
not substitute for it.

## Owner action

From an interactive terminal:

```bash
cd /home/thetu/oh-my-pi-worktrees/e1-installed-fixture
uv run --project python/omp-work python -m omp_work approve --issue OMP-279
```

The command must print digest
`a4da1fef0a7b538e0f31d64246af2997ea44210bc7ffd173fc1b46ee141947d0`
and prospective JSON. Approve only if both match this packet, then type that full digest at the
prompt. Do not run the command from another checkout or against a different digest.

After success, retain the generated `approval.json` as the exact owner artifact. Next actions are
to rebuild the disposable release, rerun both lifecycle tests and installed regressions, freeze
evidence, obtain independent review, and prepare installation/rollback. Production activation
remains separately authorized.

If approval succeeds but staging or tests fail, no live service or database changes. Keep the
approval artifact bound to this digest, diagnose the failure, and rerun only against byte-identical
contract source. Any later contract change produces a new digest and fails closed until separately
approved.
