# session-system

Chris's ADHD session-management system: Linear (team HOME) is the owner page,
omp is the only CLI surface (ruling 2026-08-10). The canonical home is
`session-system/` inside the oh-my-pi fork (`theturtlecsz/oh-my-pi`,
checkout `/home/thetu/oh-my-pi`), merged with full history 2026-08-13;
`zimmermanc/session-system` is the frozen pre-merge archive. The live
locations are symlinks created by `install.sh`.

## Layout → live location

| Repo path | Symlinked from | What it is |
|---|---|---|
| `extensions/linear-now.ts` | `~/.omp/agent/extensions/linear-now.ts` | The omp extension: start digest, NOW footer, /now //done //capture, bounded `linear` tool (two-phase owner-gated writes) |
| `rules/linear-plan.md` | `~/.omp/agent/rules/linear-plan.md` | omp plan-mode rule: ground plans in the Linear tree |
| `agents/AGENTS.md` | `~/AGENTS.md` | Home-directory session protocol (bookends contract) |
| `skills/summary` | `~/.agents/skills/summary` | /summary close ritual (cross-harness: pi + Claude Code) |
| `skills/questionyourself` | `~/.agents/skills/questionyourself` | Confidence audit, invoked by summary |
| `skills/whatsmissing` | `~/.agents/skills/whatsmissing` | Blind-spot audit, invoked by summary |
| `prompts/archive/` | (nothing — archive only) | Retired session charters. Ruling 2026-08-10: work routes through Linear (issues, NOW, comments), never prompt files |
| `tests/` | (nothing — fork-persistence tests) | Extension loads against current omp source; installer integrity + idempotency |
| `update.sh` | (nothing — run by hand) | The update loop: fetch upstream → merge → `bun install` → native refresh → fork tests → push origin |
| `refresh-natives.sh` | (nothing — run by update.sh) | Keeps the drop-in native addon matched to the current omp version |

`~/.claude/skills/summary` reaches the skill via `~/.agents/skills/summary`.

## Not in this repo

- `~/.config/linear.env` — the Linear API key. Never commit it. Back it up
  separately (it is the only secret the system needs).
- `~/.omp/agent/linear-now.json` — runtime cache, regenerates itself.

## Dev loop

Edit here → omp loads extensions at startup only → restart omp → prove live
(drive the exact action; evidence comment on the Linear issue). Batch all
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

Update loop: `bash session-system/update.sh` from the fork root — fetches
upstream, merges `upstream/main`, runs `bun install` and the fork tests
(`session-system/tests/`, the drift alarm proving the extension still loads
against the new omp source), then pushes origin. Restart omp afterward.

## History

Built 2026-08-10 (HOME-27) after the end-to-end review: system previously
lived as loose dotfiles with no history. Walkthrough + hardening proofs live
as comments on HOME-22/30/31/32.
