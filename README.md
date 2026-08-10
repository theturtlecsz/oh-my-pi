# session-system

Chris's ADHD session-management system: Linear (team HOME) is the owner page,
omp is the only CLI surface (ruling 2026-08-10). This repo is the canonical
home and dev workspace for every piece; the live locations are symlinks
created by `install.sh`.

## Layout → live location

| Repo path | Symlinked from | What it is |
|---|---|---|
| `extensions/linear-now.ts` | `~/.omp/agent/extensions/linear-now.ts` | The omp extension: start digest, NOW footer, /now //done //capture, bounded `linear` tool (two-phase owner-gated writes) |
| `rules/linear-plan.md` | `~/.omp/agent/rules/linear-plan.md` | omp plan-mode rule: ground plans in the Linear tree |
| `agents/AGENTS.md` | `~/AGENTS.md` | Home-directory session protocol (bookends contract) |
| `skills/summary` | `~/.agents/skills/summary` | /summary close ritual (cross-harness: pi + Claude Code) |
| `skills/questionyourself` | `~/.agents/skills/questionyourself` | Confidence audit, invoked by summary |
| `skills/whatsmissing` | `~/.agents/skills/whatsmissing` | Blind-spot audit, invoked by summary |
| `prompts/PROMPT-*.md` | `~/PROMPT-*.md` | Session charters (walkthrough, hardening loop, verdict drain, historical) |

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

## History

Built 2026-08-10 (HOME-27) after the end-to-end review: system previously
lived as loose dotfiles with no history. Walkthrough + hardening proofs live
as comments on HOME-22/30/31/32.
