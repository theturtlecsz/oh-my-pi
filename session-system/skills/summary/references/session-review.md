# SECTION 1 — the session review

## Ground in artifacts (mandatory before any prose)

Never summarize from conversation memory alone. Re-query the record. As cheap
as these are, just run them:

- `git log --oneline` since session start on every repo touched (check
  `git worktree list` for lanes you created or left behind), plus
  `git status` on the prod tree.
- Deploy manifests: tail `deploy-manifests/*.jsonl` for lines written today.
- Live state of anything deployed or changed: `systemctl` is-active on
  touched services/timers, relevant journal tails, DB or API spot-checks of
  the session's claims.
- Work Ledger (owner page): one bounded read — in-flight projects with
  health, the `now` holder, and the triage queue. Use the workflow tool
  (`work`) with `tree`, `my_now`, `waiting`; never query a backend API
  directly. This is the record the close ritual updates.
- TASKS.md / decision-queue state for anything parked.

Command hygiene for every gate this phase runs (obs #141 — restate before the
first gate command, this phase starts cold): gate commands never run inside
pipelines — `cmd > out.log 2>&1; echo exit=$?`, then filter the file; a
passing test gate must also show a non-zero selected-test count.

When a workflow record is MISSING (no close attempt, no receipt), absence
proves state, not cause: inspect the preceding gate verdict and the event
chronology before attributing a skipped write, separating three claims —
current state, whether the workflow required that state at that moment, and
which step would create it (obs #131).

Anything you cannot re-verify goes in an explicit **UNVERIFIED** list — never
silently promoted to fact. If a claim from earlier in the session fails
re-verification, that is a headline finding, not a footnote.

## questionyourself, over the whole session

**Invoke the `questionyourself` skill with the Skill tool now.** Actually
loading it is mandatory — a paraphrase from memory is a skipped phase. Run
its full protocol scoped to the WHOLE session: **what are you least confident
about in what this session did?** Name the specific claim resting on the
weakest evidence; distinguish verified (ran/read/measured — cite it) from
assumed; rank shakiest first; for each, state what would increase confidence
and do it immediately if cheap. Honor the "nothing material" escape hatch.

## whatsmissing, over the whole session

**Invoke the `whatsmissing` skill with the Skill tool now.** Same rule:
loading it is mandatory, paraphrase is a skipped phase. Run its full protocol
scoped to the whole session: **what is the biggest thing the owner is
probably missing and hasn't thought to ask?** Ground each item in a specific
file, commit, service, or clock; rank by blast radius; classify each as *a
question the owner should ask*, *a fact nobody verified*, or *a decision
being made by default through inaction*; run cheap checks instead of naming
them. Include second-order effects of decisions made this session. Honor the
"nothing material" escape hatch.

## Write SECTION 1: THE SESSION (for the owner)

Audits BEFORE summary — never write the summary first and let the audits
anchor on it. Fold the audit findings in; the summary must absorb them, not
contradict them.

The review OPENS with the completion tree (HOME-109): call the workflow tool
with `action:"my_now"` and paste its output verbatim, then explain in plain
words what this session changed in that tree. Only then the sections below.

All seven law-28 sections remain, but chat carries ONLY plain words: commit
hashes, file paths, sha256 seals, and test counts move to the issue
handoff/board comment, cited from chat as "evidence saved on the issue."

- **DECIDED** — rulings made, with where each is now recorded (or flagged as
  recorded nowhere).
- **BUILT+PROVEN** — each item with its dated evidence: commit hashes, test
  counts, live proofs re-queried in grounding. Any PROVEN claim written to
  memory or the ledger carries a pointer to durable evidence in the same
  line — no pointer means write PROVEN-AT-SHIP instead (obs #99).
- **SHAKY** — the questionyourself ranking, compressed.
- **BLIND SPOTS** — the whatsmissing ranking, compressed.
- **UNVERIFIED** — claims that could not be re-grounded, explicitly labeled.
- **PARKED ON YOU** — every item waiting on the owner, each with a one-line
  "how to unblock." Each item here must ALSO exist in the ledger carrying the
  `waiting-on-chris` label (file it during triage if it doesn't).
- **PRODUCT MOVED** — what the household can see/do now that it couldn't
  this morning. If nothing, say nothing moved.
