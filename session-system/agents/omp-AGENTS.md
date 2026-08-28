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
are bypassed for this lane. Intake ends at a published Work Ledger item —
execution lanes pull from the ledger.

## Plain language (owner directive, 2026-08-11; redefined by HOME-109, 2026-08-13)

Owner-facing output, ALL the time (HOME-109): routine progress replies are
completion-tree updates or plain sentences — what moved, what's next, what's
stuck and why, in household terms. No commit hashes, file paths, protocol
terms, or tool narration unless Chris asks. Technical detail is tucked away,
reachable: it lives in issue comments and comes out the moment he asks for it
— it never leads. Status questions ("where does X stand") are answered with
the completion tree (`work` tool, action my_now) plus one plain explanation
line. Code, commits, and Work Ledger evidence keep full technical precision — this
governs what Chris SEES, not what is recorded.

## Question format (owner ruling, 2026-08-27 — non-negotiable, global)

Never present pros and cons or pro/con breakdowns for options in questions,
ask dialogs, or decision menus. State options neutrally and concisely by what
they do, with facts only — never a pros/cons comparison.

## Issue tracking law (owner ruling, 2026-08-13 — non-negotiable, global)

Every item is tracked as a Work Ledger item: findings, fixes (including ones
found and fixed in the same session), watch-items, parked ideas, follow-ups,
decisions needed, and new standing rules. Chat, handoff comments, local
files, and todo lists are NOT tracking — they evaporate or go unread. If
work is done or discovered and no issue exists, create one (two-phase
ledger write; see routine self-confirmation below) before the session ends. A finding that lives only in a
comment is unfiled, and unfiled = lost. This law is global: it applies in
every repo and every session, whatever the project.

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
Everything else stays visibly owner-confirmed: formal `/intake`
publication, `queue_work`, `set_now`, `cancel_work`, the `/summary`
candidate-freeze dialog, and the `/done` close verdict. If a proposed
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
   (`<agent-dir>/work-rider-batches/`, `[{key, evidence}]`). At the next
   literal /summary the host shows the exact keys + batch digest and, on
   Chris's yes, seals them into that close attempt; the audited task carries
   every rider's criteria and evidence, and the /done completes primary +
   riders atomically (OMP-93 rider authority, decision 0006). Owner-ruled
   absorbed/duplicate/deletable items go to cancel inside an owner-entered
   /done session instead — never relabel delivered work as canceled.
3. **Contract changes ride owner hash-approval.** Rider authority (and any
   future contract change) lands only when Chris approves the exact staged
   `contract_sha256`; approval.json is his attestation and is never minted
   from chat scope.

## Autonomous execution authority (/execute, owner ruling 2026-08-28)

`/execute <key> [--queue]` is the sole one-command exception to manual closeout
ceremony. A literal owner `/execute` command grants authority to select the
target item, seal structured criteria derived from the original request, stamp
the execution plan, freeze, audit/remediation, push, and PASS close authority
for the bounded grant within bounded continuation, attempt, and progress caps.
Manual `/plan`, `/summary`, and `/done` retain their explicit-command gates.

## Task Observer (installed 2026-07-18, owner-approved activation)

At the start of any task-oriented session — any interaction where you will
use tools and produce deliverables — read `skill://task-observer/references/session-start.md`
before beginning work, so skill improvement opportunities are captured
throughout the session. Load the full `skill://task-observer` only for its
episodes (logging observations, weekly reviews, editing or staging skills).

When loading any skill, check the observation log
(`~/.agents/skill-observations/log.md`) for OPEN
observations tagged to that skill and apply their insights to the current
work, even if the skill file hasn't been updated yet.

## Model escalation (HOME-131, 2026-08-14)

Everyday work runs on the default worker (Terra-medium). Escalation is
explicit routing — no automatic machinery:

* **Escalate to Sol-xhigh (`@slow`)** when the work touches any of:
  security/auth changes; concurrency or distributed-state behavior; data
  migrations or destructive operations; public API or compatibility changes;
  more than 3 subsystems affected; two failed repair/test loops; or a
  material deviation from the approved plan.
* **Escalate to K3-high (`@deep`)** for exceptionally large-repo or
  long-horizon work, or as the third-family adjudicator when the worker and
  the auditor disagree.

Any K3 reference carries an explicit `:high` or `:low` suffix — never bare
(K3 always thinks and defaults to max effort). The bookend roles (`intake`,
`audit`, `deep`) stay out of the execution cycle; /intake and the /summary
auditor own them.

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
