# session-system

Chris's ADHD session-management system: the Work Ledger (`work.omp.dev/v1`) is the
owner authority, omp is the CLI surface. The canonical home is `session-system/`
inside the oh-my-pi fork (`theturtlecsz/oh-my-pi`, checkout `/home/thetu/oh-my-pi`),
merged with full history 2026-08-13; `zimmermanc/session-system` is the frozen
pre-merge archive. The live locations are symlinks created by `install.sh`.

## Work Ledger backend

`python/omp-work/` owns the ratified `work.omp.dev/v1` contract and the PostgreSQL
Work Ledger service. `install.sh` installs `work-now.ts` (loopback-only, never crosses
the network boundary). The service is the sole workflow authority; Linear history is
retained offline as static export archives and provenance records.
## Layout → live location

| Repo path | Symlinked from | What it is |
|---|---|---|
| `extensions/work-now.ts` | `~/.omp/agent/extensions/work-now.ts` | Work Ledger backend entry: thin wiring onto workflow/host.ts + workflow/work.ts |
| `extensions/workflow/` | `~/.omp/agent/extensions/workflow/` | Shared workflow host + backend adapter: NOW footer, start digest, /now /done /capture /center (OMP-25 read-only orientation turn), unified `work` tool (two-phase owner-gated typed writes), transcript-bound receipts |
| `extensions/model-bookends.ts` | `~/.omp/agent/extensions/model-bookends.ts` | HOME-131 bookends: /intake auto-routes to the intake role (Fable-high) and forwards to the skill |
| `agents/auditor.md` | `~/.omp/agent/agents/auditor.md` | The `auditor` task agent: blocking, fresh-context final acceptance audit on `@audit`, verdict-structured report |
| `rules/work-plan.md` | `~/.omp/agent/rules/work-plan.md` | omp plan-mode rule: ground plans in the ledger tree |
| `agents/AGENTS.md` | `~/AGENTS.md` | Home-directory session protocol (bookends contract) |
| `skills/summary` | `~/.agents/skills/summary` | /summary close ritual (cross-harness: pi + Claude Code) |
| `skills/questionyourself` | `~/.agents/skills/questionyourself` | Confidence self-audit |
| `skills/whatsmissing` | `~/.agents/skills/whatsmissing` | Blind-spot audit |
| `prompts/archive/` | (nothing — archive only) | Retired session charters. Ruling 2026-08-10: work routes through the ledger, never prompt files |
| `tests/` | (nothing — fork-persistence tests) | Extension loads against current omp source; installer integrity + idempotency |
| `update.sh` | (nothing — run by hand) | Pinned upstream merge + gate runner: full-40-hex commit only, never pushes, never touches live links |
| `refresh-natives.sh` | (nothing — run by update.sh) | Keeps the drop-in native addon matched to the current omp version |

`~/.claude/skills/summary` reaches the skill via `~/.agents/skills/summary`.

## Not in this repo

- `~/.config/omp-work/client.json` — Work Ledger bearer + endpoint (without it the backend stays dormant with a warning).
- `~/.omp/agent/work-now.json` — runtime cache, regenerates itself.

## Dev loop

Edit here → omp loads extensions at startup only → restart omp → prove live
(drive the exact action; typed evidence receipt on the ledger item). Batch all
extension edits per restart. If a restart ever fails to load the extension
via symlink, `install.sh --copy` falls back to copying files into place —
then keep edits in the repo and re-run it.

`omp` on PATH runs from this fork checkout (`bun run setup` in
`/home/thetu/oh-my-pi` source-links it; re-run only if the link breaks).
Natives: `build:native` needs bazelisk (absent here), so the exact-version
npm binaries sit in `packages/natives/native/` — `refresh-natives.sh` keeps
them matched to `packages/natives/package.json` (the workspace loader skips
the version sentinel, so without this gate a stale binary fails nothing).
Re-run `bun run setup` only if natives change and bazelisk is available.

Update loop: `bash session-system/update.sh <full-40-hex-upstream-commit>` from
an integration branch (never `main` — the script refuses to merge there, and it
refuses moving refs like `upstream/main`). Before any fetch, merge, or install
it scans `/proc` for live mappings: if any same-owner process — including the
session running the script — maps code from the checkout, it refuses and
prints the exact recovery `update.sh: run the upgrade from a session already
on the stable build, then retry`. The rule is exactly: any same-owner process
whose maps the kernel refuses to expose (session infrastructure like `systemd
--user` and the ssh/gpg agents, or anything self-shielded) is warned and
skipped — an owner-accepted blind spot (OMP-157, 2026-08-26) — and any
readable mapping under the checkout refuses. The first run merges the pinned commit with
`--no-ff` and stops — on conflicts, resolve by hand, commit, and re-run. Once
the target is an ancestor, a re-run executes the frozen install
(`bun install --frozen-lockfile`), `refresh-natives.sh` (which stages the new
addons on the same filesystem and renames them into place, so already-running
processes keep their old mapped inodes while new launches resolve the new
files), and the full gate
list (handoff verifier, session-system + work-client + verifier tests,
session-system tsc, `check:ts`, cargo fmt/clippy, `test:ts`, `test:scripts`,
`test:py`, cargo nextest, and the PostgreSQL session smoke), refusing success
if the tracked tree ends dirty. The script never pushes and never installs
live links: cutover to the live checkout is a separate, separately-gated
procedure (see OMP-156's `docs/upstream-18.0.6-upgrade.md` for the reference
run), and `install.sh --print-manifest` provides the read-only live-link
manifest that cutover and rollback compare against. Restart omp afterward.

## New project (day 1)

Everything native loads from this repo no matter the directory — extension,
AGENTS files, and skills. To scope a brand-new project into the Work Ledger
workflow:

1. `git init` if it isn't a repo yet — markers resolve from the git root.
2. `echo "Exact Project Name" > .work-project` at the repo root.
   From then on `/now` filters to that project and capture/create file into
   it. The extension validates the exact marker on session start and bare `/now`;
   a stale or misspelled marker is an on-screen error, not an "empty project."
3. Work as usual — digest, NOW footer, `/now`, `/done`, `/capture`, intake.

Without a marker in a git repo, `/now` still works (unfiltered map) but
`/capture` and `create_issue` are refused until the repo is scoped or a
project is passed explicitly — the session-start notification and the digest
SCOPE line both say so. Non-git scratch dirs keep the old behavior (filing
lands on team HOME, or NOW's project). Enforcement is native to the
extension, never an LLM rule.

## Execution lane & branch protection (OMP-212)

When default branches enforce pull request rulesets (GH006 protection), execution grants push candidate commits to dedicated execution branches (e.g. `refs/heads/execution/<key>`) bound at grant admission. `pushCandidate` supports non-direct candidate push refs via `targetRemoteRef`, allowing `begin_execution_review` to complete candidate verification without requiring direct unapproved push to `main`. Work item completion remains gated on merge confirmation: a PR against the protected default branch with required checks passing and ancestry verification before ledger completion. Pushing an execution branch alone never marks the item delivered.

**Bootstrap semantics:** the updated execution branch push path takes effect in a fresh session after this change itself is merged.

## History

Built 2026-08-10 (HOME-27) after the end-to-end review: system previously
lived as loose dotfiles with no history. Walkthrough + hardening proofs live
as comments on HOME-22/30/31/32.
