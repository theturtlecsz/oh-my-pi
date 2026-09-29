Unapplied proposal implementing `docs/adr/0005-mission-first-architecture.md`. Chris applies this wording in s08. It leaves `session-system/agents/omp-AGENTS.md` unchanged.

Sources: `/home/thetu/master-report/MANDATE-RECONCILIATION.md` (C1, C2, C4, C6) and `/home/thetu/master-report/DECISIONS.md`.

Each section quotes one passage from that doctrine file, then the replacement. Current is the text today. Proposed is what s08 pastes in its place.

## Intake routing

Current:

```
All intake-shaped requests — a vague idea to formalize, a plan or draft to
stress-test or grill, a deep interview, a spec re-baseline — route to the
`/intake` skill (`~/.claude/skills/intake`). The "deep interview" and
"ralplan" keyword triggers in the managed block above no longer own intake;
plugin ambiguity skills (deep-interview, deep-dive, ralplan, /plan-as-intake)
are bypassed for this lane. Intake ends at a published Work Ledger item —
execution lanes pull from the ledger.
```

Proposed:

```
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
```

Why: D23, D29.

## Routine ledger self-confirmation

Current:

```
Everything else stays visibly owner-confirmed: formal `/intake`
publication, `queue_work`, `set_now`, `cancel_work`, the `/summary`
candidate-freeze dialog, and the `/done` close verdict.
```

Proposed:

```
Chris confirms only new or materially changed mission scope, and tier 3
actions. Ordering and cancelling are plain-language intents: Chris states
priority or cancellation in ordinary language, and the owner client turns
that intent into the operation. OMP orders approved work below that itself.
Permanently abandoning, cancelling, or materially redefining an approved
mission requires an explicit policy basis or Chris's approval. Candidate
freeze is a lifecycle step, and the close verdict is the control plane's
PASS rule.
```

Why: C2, D25, D29, D32, D35, D40.

## Close asymmetry part 2

Current:

```
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
```

Proposed:

```
2. **Historical paper is swept in prepared batches.** The
   `ledger-maintenance` agent verifies ripe items, emits an explicit batch
   report, and stages verified-delivered items as a rider batch
   (`<agent-dir>/work-rider-batches/`, `[{key, evidence}]`). Verified riders
   close by the control plane's verification rule. The audited task carries
   every rider's criteria and evidence, and primary and riders complete
   together when that rule passes (OMP-93 rider authority, decision 0006).
   Absorbed, duplicate, or deletable items that the owner has ruled are a
   plain-language cancellation. Delivered work keeps its delivered label.
```

Why: C4, D25, D29.

## Autonomous execution authority

Current:

```
`/execute <key> [--queue]` is the sole one-command exception to manual closeout
ceremony. A literal owner `/execute` command grants authority to select the
target item, seal structured criteria derived from the original request, stamp
the execution plan, freeze, audit/remediation, push, and PASS close authority
for the bounded grant within bounded continuation, attempt, and progress caps.
Manual `/plan`, `/summary`, and `/done` retain their explicit-command gates.
```

Proposed:

```
An approved mission or standing project mandate carries `/execute`'s
authority: select the target item, seal structured criteria derived from the
original request, stamp the execution plan, freeze, audit and remediate,
push, and PASS close authority for that grant within the bounded
continuation, attempt, and progress caps. Typed `/intake`, `/plan`,
`/execute`, `/summary`, and `/done` are expert operations. Each changes
execution state only through the control plane and passes the same checks,
tier gate, and locks as the mission (E6).
```

Why: D29, C2, E6, D42.
