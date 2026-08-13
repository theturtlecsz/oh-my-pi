# Owner doctrine (moved from ~/.claude/CLAUDE.md, HOME-53)

## Delegation policy

Owner directive of 2026-07-18, restored 2026-07-25 after the OMC upgrade
reverted it.

Every subagent costs a full extra request stream. Work directly in the main
loop by default — including multi-file changes, reviews, verification, and
research. Delegate only when a task genuinely needs isolation (scanning huge
or untrusted input that would flood context) or true parallelism the main
loop cannot provide, and then use the cheapest model that fits.

Never use Workflow or multi-agent orchestration unless asked for by name in
the current session, whatever an effort mode suggests. Prefer `/effort high`
over ultracode for ops sessions.

Keep tool output filtered (`grep`/`jq`/`tail`); never dump raw logs or
re-read large files.

## Intake routing (HOME-43, 2026-08-11)

All intake-shaped requests — a vague idea to formalize, a plan or draft to
stress-test or grill, a deep interview, a spec re-baseline — route to the
`/intake` skill (`~/.claude/skills/intake`). The "deep interview" and
"ralplan" keyword triggers in the managed block above no longer own intake;
plugin ambiguity skills (deep-interview, deep-dive, ralplan, /plan-as-intake)
are bypassed for this lane. Intake ends at a published Linear issue —
execution lanes pull from Linear.

## Plain language (owner directive, 2026-08-11; redefined by HOME-109, 2026-08-13)

Owner-facing output, ALL the time (HOME-109): routine progress replies are
completion-tree updates or plain sentences — what moved, what's next, what's
stuck and why, in household terms. No commit hashes, file paths, protocol
terms, or tool narration unless Chris asks. Technical detail is tucked away,
reachable: it lives in issue comments and comes out the moment he asks for it
— it never leads. Status questions ("where does X stand") are answered with
the completion tree (linear tool, action my_now) plus one plain explanation
line. Code, commits, and Linear comments keep full technical precision — this
governs what Chris SEES, not what is recorded.

## Issue tracking law (owner ruling, 2026-08-13 — non-negotiable, global)

Every item is tracked as a Linear issue: findings, fixes (including ones
found and fixed in the same session), watch-items, parked ideas, follow-ups,
decisions needed, and new standing rules. Chat, handoff comments, local
files, and todo lists are NOT tracking — they evaporate or go unread. If
work is done or discovered and no issue exists, create one (owner-confirmed
two-phase write) before the session ends. A finding that lives only in a
comment is unfiled, and unfiled = lost. This law is global: it applies in
every repo and every session, whatever the project.


## Task Observer (installed 2026-07-18, owner-approved activation)

At the start of any task-oriented session — any interaction where you will
use tools and produce deliverables — invoke the `task-observer` skill before
beginning work, so skill improvement opportunities are captured throughout
the session.

When loading any skill, check the observation log
(`~/.agents/skill-observations/log.md`) for OPEN
observations tagged to that skill and apply their insights to the current
work, even if the skill file hasn't been updated yet.

# MCP — gotchas only

Server catalogs, tool schemas, and per-server usage instructions are injected
by the servers themselves. This file holds only what those injections don't say.

## Android / device automation

Household targets: Google TV Streamers — bedroom `192.168.0.17`, living room
`192.168.0.19`. Run `adb connect <ip>:5555` first if a device isn't listed.

* **deepadb** — default for anything ADB. 204 tools with deferred schemas:
  search for the tool, don't guess names. Source at `~/tools/DeepADB`
  (`npm run build`, then restart Claude Code).
* **maestro** — repeatable UI journey tests (Maestro YAML), not one-off taps.
* **android-mcp** — uiautomator2 fallback when deepadb UI tools misbehave.

Never drive one device from all three at once — the adb server contends.

## Cost and concurrency traps

* `readwise` export returns whole documents; stop after `list_highlights`
  unless full content is genuinely needed.
* `supabase` and browser automation (playwright/puppeteer) are not
  parallel-safe — serialize calls to each.

## Rebuilds need a restart

An MCP server cannot reload in place. After changing and building one, the
session must restart before the new code is live.

## NotebookLM

CLI/HTTP only; commands live in the `notebooklm` skill.

Auth is the non-obvious part: it rides on a Chrome profile, so a headless
server needs an X11-forwarded browser login once.

```bash
chromium --user-data-dir=~/.local/share/notebooklm-mcp/chrome_profile \
         --password-store=basic --no-first-run https://notebooklm.google.com
# log in, close the browser, restart Claude Code, then: notebooklm doctor
```

## Retired

`local-memory` (`lm`) was shut down 2026-07-14; final export at
`~/backups/lm-final-export-20260714.db`. Cross-session state now lives in
file-based auto-memory and per-project wikis.
