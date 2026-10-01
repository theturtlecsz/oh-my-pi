# Child Budgets Lockdown Report

Evaluation of runtime budget controls for subagents spawned by the task executor, and of the gaps that leave a child's spend unbounded.

## Overview

Subagents are spawned by the coding-agent task executor, which today bounds only two quantities: assistant request count (the soft request budget) and, optionally, wall-clock lifetime. Nothing bounds the tokens a child burns or the money it spends, and the one wall-clock cap that exists is off by default because a single global default cannot tell a Work Ledger item from an ordinary `omp` run.

See [README.md](./README.md) for report scope, column definitions, and the upstream divergence taxonomy.

## Existing controls

- `task.softRequestBudget` — default 200 assistant requests per child. Crossing it injects a wrap-up steering notice; at 1.5x the budget the free-running turn is force-stopped and the child must yield its partial findings. Location: [task/settings.ts](../../../packages/coding-agent/src/task/settings.ts): `task.softRequestBudget`, enforced in [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `createSubagentRunMonitor`, `SOFT_REQUEST_BUDGET`.
- `task.maxRuntimeMs` — default 0 (cap exists, off). The wall-clock cap is implemented but disabled unless a run sets a positive value. Location: [task/settings.ts](../../../packages/coding-agent/src/task/settings.ts): `task.maxRuntimeMs`, enforced in [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `maxRuntimeMs` timer.
- robomp `task_timeout_seconds` 2400 plus `task_timeout_hard_grace_seconds` 60 — the outer runtime's per-task wall-clock bound and its hard-kill grace. Location: [config.py](../../../python/robomp/src/config.py): `task_timeout_seconds`, `task_timeout_hard_grace_seconds`.
- flood `max_minutes` 90 — external policy controlling how long a flood worktree job may run; not part of this repository.
- No token or cost cap exists today for children: neither the executor nor the settings schema bounds spend by tokens or money.

## Proposed Child Budget Locks

| ID | Lock | Location | Affected runs | Breaks | Upstream divergence |
| --- | --- | --- | --- | --- | --- |
| L-CB-01 | Cap tokens spent per child; abort or force-yield when the cap is crossed (decided: D17, OMP-404) | [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `createSubagentRunMonitor`, usage accumulation | robomp, interactive omp | Long refactors and exploratory children that legitimately consume a large context are cut off mid-task | adds |
| L-CB-02 | Cap cost spent per child; abort or force-yield when the cap is crossed (decided: D17, OMP-404) | [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `createSubagentRunMonitor`, usage accumulation | robomp, interactive omp | Models without price data cannot be costed, so the cap is unenforceable for those children | adds |
| L-CB-03 | Default `task.maxRuntimeMs` to a non-zero wall-clock cap | [task/settings.ts](../../../packages/coding-agent/src/task/settings.ts): `task.maxRuntimeMs` | robomp, interactive omp | Slow-model children that need more wall-clock than the default cap are aborted before they finish | adds |
| L-CB-04 | Give a parent and its children a single shared budget instead of a per-child budget (decided: D17, OMP-404) | [executor.ts](../../../packages/coding-agent/src/task/executor.ts): `createSubagentRunMonitor`, child budget resolution | robomp, interactive omp | Big fan-outs exhaust the shared budget on the first few children and starve the rest | adds |

## Q4 tie

D17 (OMP-404) sets budgets — money, tokens, wall-clock, and sub-agent count — at intake per ledger item, runtime-enforced for the item and its sub-agents, with no provider caps. It decides L-CB-01, L-CB-02, and L-CB-04. It does not settle L-CB-03: a global `task.maxRuntimeMs` default hits every `omp` run, ledger item or not, so that choice remains Chris's.
