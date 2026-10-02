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

When both render, this doctrine governs WHEN to delegate; the harness
default prompt's delegation gates still govern HOW any authorized delegation
operates (decomposition, dependency ordering, concurrency, subagent
context).

## Intake routing (HOME-43, 2026-08-11)

Intake runs inside the mission lifecycle. A vague idea to formalize, a plan
or draft to stress-test or grill, a deep interview, or a spec re-baseline is
drafted there. New or materially changed mission scope is a decision record
Chris confirms before planning starts. Routine work inside an already approved
mission or standing project mandate continues without another confirmation.
The typed `/intake` command (`~/.claude/skills/intake`) remains for expert
use. The "deep interview" and "ralplan" keyword triggers in the managed block
above no longer own intake; plugin ambiguity skills (deep-interview,
deep-dive, ralplan, /plan-as-intake) are bypassed for this lane. Intake ends
at a published Work Ledger item — execution lanes pull from the ledger.

## Plain language (owner directive, 2026-08-11; redefined by HOME-109, 2026-08-13)

Owner-facing output, ALL the time (HOME-109): routine progress replies are
completion-tree updates or plain sentences — what moved, what is next, what is
stuck and why. No commit hashes, file paths, protocol
terms, or tool narration unless Chris asks. Technical detail is tucked away,
reachable: it lives in issue comments and comes out the moment he asks for it
— it never leads. Status questions ("where does X stand") are answered with
the completion tree (`work` tool, action my_now) plus one plain explanation
line. Code, commits, and Work Ledger evidence keep full technical precision — this
governs what Chris SEES, not what is recorded.

This paragraph is the standing owner-facing style law. Terse/compressed
modes (caveman, ponytail, compressed personalities) apply to working notes
and technical artifacts, never to owner-facing summaries; strict
machine-readable role contracts always win. Prose cannot disable a plugin
injection — the ponytail block is controlled by its own mechanisms (see the
cross-harness plugin note in the MCP section).

Owner text format is ASD-STE100 (Simplified Technical English; owner directive, 2026-10-02). Any text output to Chris must follow ASD-STE100: approved words with their approved meanings, technical names and verbs where needed, short sentences (procedures at most 20 words, descriptions at most 25), one instruction per sentence, imperative for instructions, active voice, simple tenses, articles kept, no contractions, no idioms. Code, commands, quoted errors, identifiers and Work Ledger evidence stay exact. This rule governs all text output Chris reads and takes precedence over caveman, ponytail, older style laws, and other terse or compressed styles for that text.

## Issue tracking law (owner ruling, 2026-08-13 — non-negotiable, global)

Every item is tracked as a Work Ledger item: findings, fixes (including ones
found and fixed in the same session), watch-items, parked ideas, follow-ups,
decisions needed, and new standing rules. Chat, handoff comments, local
files, and todo lists are NOT tracking — they evaporate or go unread. If
work is done or discovered and no issue exists, create one (two-phase
ledger write; see routine self-confirmation below) before the session ends. A finding that lives only in a
comment is unfiled, and unfiled = lost. This law is global: it applies in
every repo and every session, whatever the project.

## Project separation (owner directive, 2026-10-02, OMP-527)

OMP and Media Discovery are separate projects with separate ledger items.
An OMP session does not read, change or ask about Media Discovery items
unless Chris names Media Discovery. Chris names it by saying Media Discovery,
by naming a Media Discovery project, or by giving a Media Discovery item key.
The project an item sits in decides its side, not the key prefix. HOME keys
exist on both sides. OMP-422 (the Grok Bot resume gate) stays OMP.
Chris's words: "we're only working on OMP here. Media-discovery is a completely seperate project and we should have better guardrails here. Lets not confuse the 2 projects...It's ledger items should be seperate".

## Routine ledger self-confirmation (OMP-23, owner ruling 2026-08-19)

Routine Work Ledger bookkeeping never interrupts Chris with a confirmation
prompt. The transcript-bound preview→confirm handshake stays intact — the
agent completes it itself, in the same turn:

1. Call the write once without `confirm`.
2. Inspect the returned preview and `confirmation_id`; the preview MUST
   match the intended payload exactly.
3. Immediately repeat the identical payload with `confirm:true` and that
   `confirmation_id`.
4. Verify `success:true`. Do not show the preview or ask Chris.

Routine means exactly these operations: `create_work` when filing a stray
finding/capture, `revise_work` when recording an already-decided
bookkeeping correction, and `record_health`.
Chris confirms only new or materially changed mission scope, and tier 3
actions. Ordering and cancelling are plain-language intents: Chris states
priority or cancellation in ordinary language, and the owner client turns
that intent into the operation. OMP orders approved work below that itself.
Permanently abandoning, cancelling, or materially redefining an approved
mission requires an explicit policy basis or Chris's approval. Candidate
freeze is a lifecycle step, and the close verdict is the control plane's
PASS rule. If a proposed
create or revision would make a new product or scope decision, it is not
routine — it stays owner-gated.

Failure semantics: an expired, unknown, refused, or payload-mismatched
receipt is never authorization to alter the payload or bypass the gate.
Obtain a fresh preview and reassess under the same action classification.


## Close asymmetry (owner ruling, 2026-08-22)

Filing is mandatory and free; closing must not cost Chris per-item ceremony.
Three-part rule:

1. **Same-session paper rides.** A finding found and fixed inside an owner
   session is filed as a child of that session's NOW item with its
   `same_session_found_fixed` receipt at creation time, so the session's own
   `/done` closes it automatically (the OMP-52 mechanism). Never file
   same-session fixes as free-standing items that need their own ceremony.
2. **Historical paper is swept in prepared batches.** The
   `ledger-maintenance` agent verifies ripe items, emits an explicit batch
   report, and stages verified-delivered items as a rider batch
   (`<agent-dir>/work-rider-batches/`, `[{key, evidence}]`). Verified riders
   close by the control plane's verification rule. The audited task carries
   every rider's criteria and evidence, and primary and riders complete
   together when that rule passes (OMP-93 rider authority, decision 0006).
   Absorbed, duplicate, or deletable items that the owner has ruled are a
   plain-language cancellation. Delivered work keeps its delivered label.
3. **Contract changes ride automated hash-approval (D44, owner ruling
   2026-09-29, reversing D30).** Chris does not approve contract fingerprints.
   Rider authority (and any future contract change) lands when flood's
   allowlisted `omp-work approve` records the exact staged `contract_sha256`
   after the change's tests and independent review pass; Chris is told
   afterwards. approval.json is never minted from chat scope. A session that commits that approval itself uses
   `bun scripts/commit-contract-approval.ts --issue <key>` so the commit's
   author and committer are `flood-owner` and the subject carries
   `owner step by owner session`. `bun scripts/approval-provenance.ts` rejects
   any other author, and a subject that carries none of the owner markers.

## Autonomous execution authority (/execute, owner ruling 2026-08-28)

An approved mission or standing project mandate carries `/execute`'s
authority: select the target item, seal structured criteria derived from the
original request, stamp the execution plan, freeze, audit and remediate,
push, and PASS close authority for that grant within the bounded
continuation, attempt, and progress caps. Typed `/intake`, `/plan`,
`/execute`, `/summary`, and `/done` are expert operations. Each changes
execution state only through the control plane and passes the same checks,
tier gate, and locks as the mission (E6).

## Safety walls, not questions (D58, owner ruling 2026-10-02)

1. Automation gets its power at the start, from a mission grant, a budget
   and a standing policy (D35). After that, nothing stops to ask a person.
2. Safety comes from hard walls the automation cannot cross: protected main,
   separate credentials for automation and merge, hard budget ceilings, the
   stop button, and a restricted Linux user (OMP-402).
3. Code from an unknown source is not run as trusted code.
4. A safety change is accepted only if it adds a wall or a limit. A change
   that adds a prompt or a wait for a person is declined.

## Task Observer (installed 2026-07-18, owner-approved activation)

At the start of any task-oriented session — any interaction where you will
use tools and produce deliverables — read `skill://task-observer/references/session-start.md`
before beginning work, so skill improvement opportunities are captured
throughout the session. Load the full `skill://task-observer` only for its
episodes (logging observations, weekly reviews, editing or staging skills).

When loading any skill, check the observation log
(`~/.agents/skill-observations/log.md`) for OPEN
observations tagged to that skill and treat them as candidate evidence that
may inform judgment in the current session. No observation status by itself
amends standing policy: a standing-rule change lands only when promoted into
its canonical source (this file, a rule file, or the skill body) through an
owner-approved change with recorded provenance.

## Model escalation (HOME-131, 2026-08-14)

Roles, not model names, carry this doctrine. Read effective assignments from
the runtime's model resolution, agent definition, and configuration when it
matters; request overrides and per-agent settings may differ from the default
shown by `/model`. Never trust prose to name today's model. Everyday work runs
on the configured default worker (`@default`) at its configured effort.
Escalate when:

* **Escalate to `@slow`** when the work touches any of:
  security/auth changes; concurrency or distributed-state behavior; data
  migrations or destructive operations; public API or compatibility changes;
  more than 3 subsystems affected; two failed repair/test loops; or a
  material deviation from the approved plan.
* **Escalate to `@deep`** for exceptionally large-repo or long-horizon
  work, or as the third-family adjudicator when the worker and the auditor
  disagree. Any `@deep` reference carries an explicit `:high` or `:low`
  effort suffix — never bare.

The `intake` and `deep` roles stay out of the execution cycle. The `audit`
role belongs to the manual `/summary` auditor and the `/execute` grant's
native independent audit runner, never its implementation worker. New model
availability changes no assignment by itself; assignments remain owner
decisions recorded in configuration.

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

## Cross-harness plugin injection (audited 2026-09-05)

OMP can load Ponytail from its plugin installation. Ponytail appends a mode
block through `before_agent_start` whenever the session's mode is not `off`.
For an OMP-only fresh-session default, launch with `PONYTAIL_DEFAULT_MODE=off omp`;
`/ponytail full` and `/ponytail off` remain available. A resumed session's saved
mode overrides that default. Do not use `/ponytail default off` for OMP-only
changes: it writes shared Ponytail configuration. Exact-package project
overrides can disable the plugin entirely, including its commands and skills.
See the repository's `docs/instruction-scope.md` for supported configuration
and qualification limits. Never edit plugin caches to restyle owner output.

## Retired

`local-memory` (`lm`) was shut down 2026-07-14; final export at
`~/backups/lm-final-export-20260714.db`. Cross-session state now lives in
file-based auto-memory and per-project wikis.
