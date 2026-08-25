---
name: summary
description: End-of-session closing ritual — actually invokes the questionyourself and whatsmissing skills over the whole session, writes a SESSION REVIEW for the owner, runs the Work Ledger close ritual (verification, sealed audit, review checkpoint, close request), and writes a standing loop-session prompt that keeps the current long-running goal iterating across sessions. Use ONLY when the owner has literally entered /summary — never because a session seems to be ending, work finished, or wrap-up wording appeared (HOME-114); /done runs its own separate flow.
---

# /summary — session closing ritual (dispatcher)

The close ritual is LEDGER-OWNED (OMP-47): the WorkService holds every gate —
the close attempt, the sealed auditor task, launch budgets, report
normalization, audit receipts, delivery receipts, and closeout state. This
skill carries only policy and the ordered ritual; when a ledger refusal and
any text here disagree, the ledger's typed event wins. `rule-map.md` names
where every removed instruction now lives.

Read these references NOW, in this exact order, then execute their phases in
order:

1. `references/policy.md` — the literal-/summary gate, identity and limits,
   portability, and the plain-language rule for owner questions.
2. `references/session-review.md` — ground in artifacts, run the
   `questionyourself` and `whatsmissing` skills (actually invoke them), then
   write SECTION 1: THE SESSION.
3. `references/ledger-close.md` — the ordered close ritual: health, triage,
   verification (which seals the audit manifest), the ONE reserved auditor
   task built from `work get_work`'s sealed AUDIT TASK, and the closeout review
   which atomically records the review and requests closeout once delivered.
4. `references/loop-charter.md` — SECTION 2: NEXT SESSION, the standing loop
   prompt.

Leave NOW unchanged. NEVER invoke `/done`, clear NOW, post a second handoff,
or close an issue from `/summary`; Chris enters `/done` separately when he
wants the reviewed issue closed.
