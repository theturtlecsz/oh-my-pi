# Pinned source installations

For the session-by-session checklist, start with [OMP stabilization](omp-stabilization-plan.md).

This is the bounded installation-isolation slice of OMP-249, developed outside
OMP's execution loop. It retains repository layout and existing WorkService
authority. It introduces no Fleet controller or general plugin framework.

## Reconciled work

Read-only WorkService reconciliation on 2026-09-07, against main
`d30f80909b9a240404d2a79ed3511cebc4d1f6ba`:

| Work | Ledger revision | Disposition |
| --- | --- | --- |
| OMP-249 | 2, `0dff0658-8b8a-51dd-ab4a-76f20fdd786b` | Existing deployment/manifest/rollback scope; this slice prepares an artifact and procedure. Owner schedules cutover. |
| OMP-246 | 1, `b960fa99-67d5-4153-9e91-75c81442187a` | Remaining complete-process continuation and interrupted-effect recovery qualification. Reuse existing protocol tests. |
| OMP-233 | 2, `35966504-9045-5ac8-8399-725624127411` | Remaining execution lifecycle and admission hardening; no redesign here. |
| OMP-245 / OMP-247 / OMP-251 | DONE, revisions 3 / 2 / 3 | Delivered fixes retained, with existing tests rerun. Do not reopen from historical reports. |

These annotations are an inspection snapshot, not a new backlog or authority.
OMP-249 and OMP-246 had requirements in their descriptions but empty structured
scope/criteria fields. Reconcile their current revisions and admit the bounded
scope through supported WorkService operations before claiming acceptance.

## Build and verify

Run from a clean candidate checkout. Destination's parent must exist; destination
must be new, outside the checkout, and remain at this final path. Python venv
entrypoints are not relocatable. The first supported qualification target is
Linux x64 with native PostgreSQL 18.

```sh
bun session-system/runtime/stage.ts \
  --source /absolute/clean-candidate \
  --destination /absolute/releases/candidate-id \
  --bun /absolute/bun --bun-version 1.3.14 \
  --uv /absolute/uv --python /usr/bin/python3 \
  --native /absolute/pi_natives.linux-x64-baseline.node \
  --native /absolute/pi_natives.linux-x64-modern.node
sha256sum /absolute/releases/candidate-id/manifest.json
```

The Bun version is an explicit operator pin, not an assumption that the
repository's packageManager declaration matches the installed interpreter.
Both values are recorded. Native versions must match the archived package.

The stage contains an exact Git archive, release-local JavaScript dependencies
installed with copyfile semantics, a copied Bun executable and native addons,
generated tool-view and embedded stats assets, and a noneditable WorkService installation from its
frozen dependency lock. The manifest inventories all files/links and records
interpreters, dependencies, platform, and source identity. Shared inodes and
unexpected external links are rejected. Python's system interpreter and stdlib
remain explicit host dependencies; this is not a hermetic or relocatable build.
The Python build backend is not separately lock-pinned, so exact staged bytes
are attested; byte-reproducible rebuilding is not claimed.

`STAGED` means bytes were assembled, not that the release passed qualification.
An interrupted build has no consumable manifest. No current pointer, live
extension link, service unit, database, or execution grant is changed by staging.

## Launch against separate state

Use the copied shell entrypoint, which starts pinned Bun from the release root
with a clean child environment. Starting Bun directly from the candidate can
execute its bunfig preload before application validation.

```sh
/absolute/releases/candidate-id/bin/omp \
  /absolute/runtime-state /absolute/workspace MANIFEST_SHA256 --mode rpc
```

The first launch creates a private HOME/XDG tree and links the existing managed
assets from the verified release. Subsequent launches require the same manifest
and unchanged managed links. A different release requires a distinct state root;
old sessions, evidence, and candidate worktrees are retained. Reopen an existing
session with OMP's supported session option when recovering the same release.

`OMP_DISCOVERY_CWD` separates control configuration from tool cwd. The managed
launcher selects an empty discovery directory, exact trusted extension modules,
and release-owned workflow assets. It does not inherit ambient project/global
plugin configuration or dotenv overrides. It rejects CLI flags that replace the
admitted configuration and extension sources. Normal OMP launch behavior remains
unchanged when this optional configuration root is absent.

Provider authentication, reviewed model/Advisor configuration, and WorkService
client capabilities must be provisioned explicitly in the private state through
supported operator paths. They are not copied from the developer's global home.
Preserve the intended Advisor/Observer configuration when qualifying a real
deployment; a successful no-model installation test does not prove that coverage.
Record the actual configuration identities with the qualification evidence.

Installed mode disables candidate-driven WorkService refresh. Candidate Python
or migration changes do not restart the service or silently rebind an active
grant. The existing judge/contract/evidence checks still apply. A trusted runtime
change requires deliberate activation and applicable fresh authorization.

The service route invokes installed Python directly with `-I -B`; it never runs
`uv run` or resynchronizes an editable package:

```sh
/absolute/releases/candidate-id/bin/omp \
  /absolute/runtime-state /absolute/workspace MANIFEST_SHA256 \
  --service --postgres-port 55421 serve --port 55422 \
  --capabilities-dir /absolute/runtime-state/config/omp/work-ledger/capabilities
```

Use disposable state and unused ports for qualification. Omitting the explicit
PostgreSQL port retains WorkService's existing default; do not point qualification
at the production database. This launcher does not schedule work or restart
failed workers automatically.

## Qualification layers

```sh
bun check
bun run check:runtime
bun test packages/coding-agent/test/discovery-root.test.ts
bun test session-system/tests
bun run test:py:work-ledger:integration
bun run test:session:execute
OMP_INSTALLED_RELEASE=/absolute/releases/candidate-id \
OMP_INSTALLED_MANIFEST_SHA256=MANIFEST_SHA256 \
uv run --project python/omp-work --extra dev pytest \
  python/omp-work/tests/test_installed_runtime_isolation.py
```

The installed test starts real CLI and installed WorkService processes with
disposable PostgreSQL. It compares resident and fresh CLI behavior after
candidate edits and configuration poisoning, and rejects an altered artifact.
Missing installation inputs produce explicit pytest skips; skips do not qualify
a release. Required promotion automation must supply both inputs and reject skips.
CI retains the full installation manifest and installed-test JUnit report in its
`installed-runtime-qualification` artifact, including available evidence on failed
runs. A manifest alone or an absent/skipped test report does not qualify a release.

Existing halt, replay, canceled-grant, merge-head, and completion-evidence tests
remain part of the workflow suites. The execution smoke is now an explicit CI
step, but its synthetic session/transport parts remain documented limitations.
Full installed crash-after-effect and stale queued-input recovery, configured
model canaries, GitHub effects, and the proposed 20-trial adoption gate are
separate qualification work. Do not infer them from artifact integrity or CI.

## Deliberate promotion and rollback

1. Record exact candidate, manifest digest, executed checks/skips, installed
   configuration, and applicable issue/approval revisions. Keep release staging,
   ledger acceptance, and live activation distinct.
2. Review native/CLI/service compatibility and migration changes. Follow the
   existing owner-attestation, backup, and restore requirements in
   [Work Ledger operations](work-ledger-operations.md). Do not fabricate approval.
3. Prepare units without touching live state:

   ```sh
   bash infra/work-ledger/install.sh --render-only \
     --python /absolute/releases/candidate-id/python/bin/python \
     --unit-dir /absolute/review-units
   ```

   Render-only mode requires an explicit output directory outside live systemd
   unit directories and refuses symlinks, shared inodes, and non-regular output
   files before writing any units.

4. Owner schedules cutover. Drain or stop affected workers and confirm current
   operations/effects. Install reviewed units and select the qualified launcher
   through existing operator procedures. Validate actual loaded service readiness
   and component identities before admitting work.
5. On failure, stop new admission and reconcile uncertain effects first. Restore
   the previous exact release only if it remains compatible with persisted schema
   and configuration. Switching directories does not undo a migration. Terminal
   grants remain terminal; retain their artifacts and start fresh authority when
   required.

Separate installation directories prevent accidental linkage. They do not prevent
an agent running as the same OS user from rewriting them. Production promotion
permissions must be distinct from candidate execution; process isolation and
manifest checks are not hostile-code containment or artifact signatures.

## Upstream maintenance

The optional configuration-root seam is generic; workflow policy remains under
`session-system`. Existing fork inventory tracks every changed upstream path.
The discovery-root executable tests protect semantic behavior across upstream
integration. Refit or retire this seam when upstream supplies equivalent
configuration isolation. The Git archive helper extends the existing central
Git utility; no parallel VCS wrapper or new workflow ledger was added.
