# Work Ledger — planning and work tracking

The owner's work lives in the Work Ledger: WORLDS, SURFACE nouns with health,
PROMISE sentences (owner verdict closes), work items. The `work` tool is
installed: reads are free; gated writes always preview first; routine OMP-23
bookkeeping (finding/capture filing, bookkeeping revisions, record_health)
self-confirms silently; decision-bearing or destructive
writes await Chris.

## Uniform memory law (owner-ratified 2026-08-13, HOME-112)

state lives in the ledger; knowledge lives in files; files change when reality
changes — never on a session clock. The close ritual is the only
mandatory per-session write set; no other store may carry a mandatory
per-session write. Knowledge files (wikis, runbooks, references) update in
the same pass as the work that changes them.

When in plan mode or asked to plan:

1. Consume the current NOW item. If NOW is unset, direct Chris to `/intake` or
   `/now`; NEVER create a duplicate item from `/plan`.
2. Ground the plan in that item, the live tree, and bounded `my_now`/`waiting`
   reads. Plan against existing surfaces and promises; never invent a parallel
   tracker.
3. Owner approval is the execution boundary. The host stamps the final plan
   digest on the item before allowing execution; no manual comment or NOW
   selection is required.
4. Upgrade/cutover plans that mutate a checkout or its dependencies MUST put
   a `/proc/<pid>/maps` proof before the first mutation: show that no live
   process — including the session executing the plan — maps code from that
   tree, and when any mapping exists, run the mutation from a
   stable/rollback-linked session instead. Live-link manifests protect only
   future launches and are not a substitute for the mapping proof. In
   oh-my-pi, `session-system/update.sh` enforces this fence
   (`assert_tree_unmapped`); never bypass it. Owner-accepted carve-out
   (OMP-157, 2026-08-26): same-owner processes with kernel-unreadable maps
   are warned and skipped; any readable mapping under the tree refuses.

Always: capture stray findings as work items instead of chasing them. `/summary`
records the typed session review; `/done` alone closes after that review.
Keep ledger reads bounded; never dump the backlog into context.

## Routing law (owner-ratified 2026-08-10) — no prompt files, no file trackers

Work routes through the ledger, period. Open loops live as work items; session
handoffs go through the NOW pointer and item comments; per-item context
lives ON the item it belongs to. NEVER write a `~/PROMPT-*.md`, handoff
file, or any local file that tracks work state — that includes "on stop,
rewrite this charter file" patterns from retired charters (archived in the
session-system repo, `prompts/archive/`). A fresh session orients with the
start digest, then `/now` — not by reading a file. Exception by standing
law: media-discovery's TASKS.md is that product's engineering roadmap
(separate world, separate ruling) — do not extend the exception.

## Workflow sequence (HOME-122)

The work item is the durable cross-session state:

1. `/intake` publishes and natively selects the first item or batch parent.
2. `/plan` consumes NOW; owner approval stamps the final plan hash, approach,
   and verification list before execution starts.
3. Execution stops with one typed `Execution handoff` when needed.
4. Owner-entered `/summary` posts one typed `Session review` containing all
   evidence and exact resume state.
5. Owner-entered `/done` requires the current plan plus a later review, asks for
   one verdict, closes, and clears NOW.

Evidence comments never settle handoff/review debt. Reads and NOW selection never
imply execution. Closeout remains literal-command-only: never infer `/summary`
or `/done` from completion, and a keep-open verdict blocks closeout.

Enforcement lives in the workflow host (`work-now` extension):
the start bookend and tool description inject one canonical sequence, plan
approval fails closed if the stamp cannot land, typed comments advance only
their own stage, and the footer shows only `⚠` while a hidden checkpoint
remains owed.
