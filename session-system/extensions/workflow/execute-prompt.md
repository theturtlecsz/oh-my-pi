# Autonomous Delivery Cycle (/execute)

You are running an autonomous execution grant for a Work Ledger item.
Follow this deterministic sequence without skipping steps or asking for manual confirmations:

1. **Inspect item:** Call `work action:"get_execution"` to read current grant state, active work item, phase, and original request text.
2. **Criteria derivation:** If phase is `criteria_pending`:
   - Derive acceptance criteria ONLY from the original description text.
   - Never invent requirements not grounded in the original description.
   - Call `work action:"seal_execution_criteria", criteria: ["..."]` to seal criteria.
3. **Plan stamping:** If phase is `planning`:
   - Write the plan file at `local://execute-<lowercase-key>-plan.md` with exact level 2 headings `## Approach` and `## Verification` using dash (`- `) or numbered (`1. `) bullets.
   - List every repository-relative file path you will touch.
   - Call `work action:"stamp_execution_plan", plan_file: "local://execute-<lowercase-key>-plan.md", paths: ["path1", "path2"]`.
4. **Implement & Verify:** If phase is `executing`:
   - Edit only within the sealed path set.
   - Run focused checks and reproduction commands to verify each step.
   - Run the repo lint/format gate on the stamped paths before review: `bun x biome check <stamped paths>` (or `bun run check:tools`) and fix findings first — the freeze refuses lint-red candidates.
   - Call `work action:"begin_execution_review", body:"<exact test commands and reproduction results>"` to freeze candidate, record verification evidence, push remote commit, seal audit manifest, run native audit, and auto-complete on PASS.
5. **Remediation:** If audit reported `NEEDS_FIX` or `BLOCKED`:
   - The phase will be `remediating`.
   - Update plan at `local://execute-<lowercase-key>-plan.md` addressing only the named findings.
   - Call `work action:"stamp_execution_plan"` with the plan file and paths before making fixes.
   - Apply fixes and call `work action:"begin_execution_review", body:"<exact test commands and reproduction results>"`.
6. **Stopping:**
   - If blocked by unresolvable ambiguity or upstream failure, call `work action:"stop_execution", body: "explanation"` to cleanly stop the grant.
