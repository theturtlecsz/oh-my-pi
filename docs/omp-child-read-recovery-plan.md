# S2.2 child continuation after a durable read

Continue OMP-246 revision `f5bb9640-3491-50b7-bdad-2a77f9c102e3` after PR #25
merge `ed2a959d323870e35754469f47e851b53f176f59`, the base for isolated branch
`codex/omp-child-read-recovery` in `/tmp/omp-recovery-stabilization`. The merge tree
`9a8f925fc151c40d694ff16514e2b79988e53fad` matches exact-head hosted qualification.
This is a reproduction-only plan under the standing continuous authorization;
production implementation requires an actual negative and a bounded follow-up plan.

## Boundary

Run the existing supported synchronous, flat, non-isolated bound task through the
installed CLI. Let the original child execute its real `read(result.txt)` and
persist the matching original assistant call and native tool result. Hold its next
actual provider response before writing response bytes, before any yield or native
task completion. This is a completed child step with a durable result, distinct
from uncertain parent hook processing or committed ledger response loss.

Reuse the immutable locally qualified `original-0bdaa5cf22` installation, source
`0bdaa5cf22aaa17a62b29913ad787097ea6e1302`, manifest
`6c8ac81caa9ede53c680b45664bc7f048e3f95330ca4776e3f830d863641b648`.
Its eleven installed cases passed; preserve that identity and the separate new
harness identity. PR #25 exact-head hosted qualification passed eleven cases with zero skips or
failures, and all fourteen production hashes match this local release. Do not relabel this runtime as the later merge/test-only commit.

## Fixture and evidence

Reuse the actual disposable WorkService/PostgreSQL, repository/bare remote, CLI
launcher, persisted parent/child sessions and provider transport fixture. Extend
the existing task provider with one named post-read response barrier. An initial
provider hold may record original binding/preparation before release, but there is
no initial crash, new assignment or fabricated journal/checkpoint.

At the held second child request require:

- Exact original parent call, forward binding and reverse child initialization.
- Original prepared input and actual `read` call/result durably present on the
  child's active branch, with complete journal bytes and matching call identity.
- Actual next provider request contains that same read call/result pair.
- No unresolved child tool call, yield/output completion, native-ready record,
  parent processing claim or original parent task result.
- No response bytes sent for the held next child request.
- Recorded launcher/shared CLI process identity, parent/child IDs, grant/item/
  revision/reservation and service/Git/workspace effect snapshot.

Kill the actual shared CLI process group with SIGKILL. Preserve signal/exit/EOF,
both journals, raw RPC/provider messages, native read output, service snapshots,
Git refs and process logs. Recheck the claimed durable prefix after death. Do not
count a missed, incomplete or later cut as this boundary.

## Restart and expected baseline

Restart the identical parent session and runtime without owner repair input or
state edits. Require the same original child to continue from the saved read
result, execute yield once, deliver the original parent result, and continue the
parent through its real call/result wire pair. There must be no second read,
replacement child, duplicated input/preparation, reservation or service/Git effect.
A subsequent restart must be quiet.

Current recovery rejects retained child assistant/tool history without a native
ready certificate. That refusal is a prediction until actually observed. Preserve
the positive recovery assertion as failed when unsupported, recording refusal
safety separately; never turn a safe refusal into a passing recovery claim.

Use one fresh disposable attempt after independent observer review. If its barrier
is not reached, preserve that miss and refine the observer before another attempt.
No production remedy follows without an actual negative and a separately admitted
bounded implementation plan. Do not invent per-tool receipts or general recovery
frameworks merely to write this reproduction.

## Scope and handoff

Test-only changes: installed recovery test/support, necessary static fixtures,
this plan, coverage map and fork inventory. Runtime and live checkout stay unchanged.
Record exact command/cwd/exit/JUnit, runtime/manifest and harness hashes, raw files
and observed effects through supported WorkService handoff/readback. S2.2 remains
open; this is not S2.3, native acceptance or live activation.
