# Flood Lockdown Report

Evaluation of flood's job user, spend limits, CLI permission flags, and reviewer write access. Facts are from flood.json on 2026-09-28. Column definitions and the divergence taxonomy are in [README.md](./README.md).

## Facts

Flood runs third-party CLIs and never runs omp (owner rule, recorded on the `deepseek` provider: never use omp as a worker).

Implementers (`providers`):

- `claude`: `--dangerously-skip-permissions`, `--allowedTools Read,Edit,Write,Bash`, hooks off (`--settings` with `disableAllHooks`).
- `codex`: `--dangerously-bypass-approvals-and-sandbox`.
- `agy`: `--dangerously-skip-permissions`.
- `deepseek` via qwen (`/home/thetu/qwen-0.24/bin/qwen`, `OPENAI_MODEL=deepseek-v4.1-flash:cloud`): `--yolo`.
- `grok`: `--permission-mode bypassPermissions`.

Planner `claude-planner` (`role` `plan`, model `claude-opus-5-5`): `--dangerously-skip-permissions`, with the same allowed tools and hooks off as `claude`.

Reviewers (`reviewer`):

- kimi (`provider` `kimi-reviewer`, `kimi -m kimi-code/k3-256k`) has no read-only mode, so flood reverts the worktree after each review (`git checkout -- . && git clean -fd`).
- `grok-reviewer` runs `--sandbox read-only`. That command also passes `--permission-mode bypassPermissions`; the sandbox is what stops the review from writing the worktree.

Worktrees are this repo's branches, not PR heads (`repo` `/home/thetu/flood-repos/oh-my-pi`, `main_branch` `main`, `worktrees_dir` `/home/thetu/flood-repos/oh-my-pi-wt`, one branch per task).

Jobs run as the host user: `providers.*.cmd` and `reviewer[].cmd` name host CLIs and set no user, sudo, or `User=`. `default_max_minutes` is 90 and `test_max_minutes` is 45 (reviewer entries set `max_minutes` to 30). No token/cost cap yet. Flood jobs run ledger items, which D17 budgets (not built).

## Proposed Flood Locks

These locks sit in flood's config, outside this repository, so each upstream divergence is `none`.

| ID | Lock | Location | Affected runs | Breaks | Upstream divergence |
| --- | --- | --- | --- | --- | --- |
| L-FL-01 | Run flood jobs as a restricted user distinct from the host account (decided: D15, OMP-402) | external: flood.json (2026-09-28): providers.*.cmd and reviewer[].cmd name host CLIs and set no user, sudo, or User=; jobs inherit the host user | Flood implement, plan, review, and test jobs | Jobs that use thetu's keys | none |
| L-FL-02 | Cap tokens and cost per flood job (decided: D17, OMP-404) | external: flood.json (2026-09-28): default_max_minutes is 90 and test_max_minutes is 45; no token or cost cap field | Flood jobs on ledger items | Long jobs stopped mid-change | none |
| L-FL-03 | Run implementers and the planner in sandboxed modes instead of bypass flags | external: flood.json (2026-09-28): providers.claude.cmd --dangerously-skip-permissions, providers.codex.cmd --dangerously-bypass-approvals-and-sandbox, providers.agy.cmd --dangerously-skip-permissions, providers.deepseek.cmd --yolo, providers.grok.cmd --permission-mode bypassPermissions, providers.claude-planner.cmd --dangerously-skip-permissions | Flood implementer and planner jobs | Tests that need postgres or the network | none |
| L-FL-04 | Accept only read-only reviewers (replace or wrap kimi; grok-reviewer complies) | external: flood.json (2026-09-28): reviewer kimi-reviewer has no sandbox flag and flood reverts with git checkout -- . && git clean -fd; reviewer grok-reviewer.cmd --sandbox read-only | Flood review jobs | kimi reviews | none |
