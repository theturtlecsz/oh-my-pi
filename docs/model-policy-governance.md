# Model policy governance: assertion inventory

Inventory of every place that asserts which model, role or lane runs a piece of work. It is the base for the OMP-615 reconciliation of docs, agent definitions, config and callers. It lists assertions; it does not change routing.

Includes mission/programme assertions and packaged governance metadata. Declared routes, deterministic operations, session selections, test evidence and live qualification remain distinct. No execution or deployment is claimed here.

## Columns

- **ID**: unique `MP-nnn`.
- **Source/selector**: repo path, with `#selector` for a key, role or section.
- **Assertion**: the single claim the source makes.
- **Authority**: `executable` (code or data that decides a selection), `doctrine` (owner rule read by agents), `agent-definition` (frontmatter that names a role), `configuration` (documents or declares config), `catalog` (model metadata, discovery, KDL policy), `syntax` (grammar or examples only).
- **Caller**: who consumes the assertion at run time. `none` only for rows whose scope is not an executed path.
- **Scope**: `active-selection`, `preflight`, `doctrine/<topic>`, `syntax-example`, `catalog`, `historical`, `experimental`.
- **Disposition**: `verified` (source read, matches its caller), `unresolved` (needs a ruling), `extension` (left to the mission/programme extension).
- **Disagreement**: empty when none. Required text when `unresolved`.
- **Owner**: `Chris` for owner decisions, `-` otherwise.
- **Evidence**: repo path (with optional `#anchor`) that shows the claim.

Rows split when authority, caller or scope differs, even for one source.

## Authority and reconciliation

[Packaged routing-policy.v1.json](../python/omp-work/src/omp_work/routing/routing-policy.v1.json) is authoritative for Work stage routes, reviewer admission, tiers, retries, escalation, missions and concurrency caps. `load_policy()` loads it; `route_stage()` consumes stage identities. Its `governance` metadata qualifies declarations without granting authority. [models.md](models.md) describes coding-agent configuration, not Work routing; it is neither generated from this file nor a competing Work policy. Catalog/KDL owns model metadata, not mission assignments.

Current identities are the baseline. O2 in ORIGINAL-OBLIGATIONS and GOAL-PROMPT, and CONTINUATION-GOAL's Fable planning/Astra coordination assertions, do not mechanically restore historical models. Stage plan explicitly overrides the generic E2 tier. Audit route Kimi and admitted Sol medium reviewer are distinct selectors. ADR 0005 separates proposals from deterministic authorization; ADR 0006 D33 supersedes a model supervisor with deterministic software. ADR 0006's on-demand native reviewer grants no model-family substitution.

ADR 0001 and ACCEPTED-DECISIONS G5 retain REQUIRED independent audit, effective audit model, native acceptance, and item-specific Fable publication/native Sol audit gates until supported reconciliation. Route metadata cannot waive them. [substitutions.v1.json](../python/omp-work/src/omp_work/routing/substitutions.v1.json) and [omp-routing-substitutions.md](omp-routing-substitutions.md) are prior records: their pending governing-file notes are settled by packaged policy authority, but every `approval: null` stays null. Historical plan mismatch remains historical; overflow remains pending Chris approval. Shipped alternate behavior is not owner-approved overflow.

## Qualification and unresolved ownership

`test_model_policy_governance.py` qualifies metadata and exact lane/wire associations; `packages/coding-agent/test/model-policy-governance.test.ts` supplies offline session caller evidence. Scout resolves `@smol` to google-antigravity/gemini-3.8-flash and reviewer `@slow` to openai-codex/gpt-5.6-sol without assignments; configured default can override both, explicit slow overrides reviewer. Judge resolves Typesafe Jev or tiny/smol fallback; this proves session selection, not a Work judge route. Auditor YAML `@audit` and native runner follow isolated configured audit bindings and reject a missing binding even with a default. Fixture bindings are not owner assignments or live calls.

Unresolved, Chris-owned: session `@audit` versus Work audit stage/reviewer; session plan versus declared plan route; operator session model/lane; absent production research/judge calls; unqualified lane/wire mappings; synthetic Chris attribution. `session-system/extensions/workflow/session-ledger.ts` resolves `@advisor` and calls `complete()` for a settled-turn note, using a synthetic user message rendered from `session-ledger-prompt.md`. That prompt addresses Chris and attributes the request to him; neither the attribution nor the advisor call proves operator-model binding. Session `/plan` uses session context, while `orchestrator/operations.py::run_plan` builds deterministic work packets. Neither establishes the declared plan model's invocation.

Supervisor is `deterministic_software`, caller `omp_work.orchestrator.service`, selectors empty, model/lane null (ADR 0005/0006). Service coordinates stage operations; `stages.py::decide` chooses lifecycle movement deterministically. Lifecycle `evaluate` runs a test command through `operations.py::run_evaluate`; it is not routing `verify`. No model binding can be inferred from deterministic checks, admission, reviewer strings, API availability or matching provider spellings. No live qualification, service installation or receipt is claimed.

Tables below use literal policy values; `null` means no binding, `; ` separates ordered selections/evidence. Lane means a concurrency key, not an API prefix. Role status is invocation status; route status is separate. Tier/alternate routing evidence proves selection only. Worker transport/configuration values in [orchestrator-worker.md](orchestrator-worker.md) remain examples, not approved identities or runs.

## role-bindings

| Role | Stages | Model | Lane | Effort | Reviewers | Route status | Caller | Status | Owner | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| plan | plan | gpt-5.6-sol | openai_codex | E2 | null | resolved | null | unverified | Chris | python/omp-work/src/omp_work/orchestrator/operations.py::run_plan |
| implement | implement | gemini-3.8-flash-high | gemini_flash | E1 | null | resolved | omp_work.model_proxy | test_verified | null | python/omp-work/src/omp_work/model_proxy.py; python/omp-work/tests/test_orchestrator_production_worker.py::test_production_worker |
| review | audit; acceptance_review | kimi-code/k3-256k; kimi-code/k3-256k | kimi; kimi | E3; E3 | openai-codex/gpt-5.6-sol:medium; kimi-code/k3-256k | resolved | null | unverified | Chris | python/omp-work/src/omp_work/routing/review.py::check_reviewer; python/omp-work/src/omp_work/orchestrator/operations.py::run_audit |
| research | research | deepseek-v4.1-flash:cloud | ollama_cloud | E1 | null | resolved | null | unverified | Chris | python/omp-work/src/omp_work/research_campaign.py::run_campaign; python/omp-work/src/omp_work/intake_support_admission.py::resolve_admission |
| judge | null | null | null | null | null | pending_owner | null | unverified | Chris | python/omp-work/src/omp_work/intake_support_admission.py::resolve_admission; docs/models.md |
| operator | null | null | null | null | null | pending_owner | null | unverified | Chris | docs/adr/0005-mission-first-architecture.md; docs/models.md |
| verify | verify | gemini-3.8-flash-high | gemini_flash | E1 | null | resolved | null | unverified | Chris | python/omp-work/src/omp_work/orchestrator/operations.py |

## stage-bindings

| Stage | Rule | Model | Lane | Effort | Reviewers | Max retries | Best of N | Caller | Status | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| plan | omp-241-plan | gpt-5.6-sol | openai_codex | E2 | null | 0 | null | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.plan |
| implement | omp-241-implement | gemini-3.8-flash-high | gemini_flash | E1 | null | 2 | null | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.implement |
| verify | omp-241-verify | gemini-3.8-flash-high | gemini_flash | E1 | null | 2 | null | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.verify |
| research | research | deepseek-v4.1-flash:cloud | ollama_cloud | E1 | null | 0 | {"n":2,"metric":"fact_coverage"} | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.research |
| audit | audit | kimi-code/k3-256k | kimi | E3 | openai-codex/gpt-5.6-sol:medium | 0 | null | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.audit |
| acceptance_review | acceptance_review | kimi-code/k3-256k | kimi | E3 | kimi-code/k3-256k | 0 | null | omp_work.routing.router::route_stage | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.acceptance_review |

## tier-bindings

| Tier | Model | Lane | Est tokens | Caller | Status | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| E1 | gemini-3.8-flash-high | gemini_flash | 40000 | omp_work.routing.escalation::next_route | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers.E1 |
| E2 | claude-opus-5-5 | anthropic_fable | 80000 | omp_work.routing.escalation::next_route | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers.E2 |
| E3 | kimi-code/k3-256k | kimi | 120000 | omp_work.routing.escalation::next_route | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers.E3 |
| E4 | gpt-6-sol | chatgpt_astra | 200000 | omp_work.routing.escalation::next_route | declared | python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers.E4 |

E0 is in the ladder E0 → E1 → E2 → E3 → E4, with no model tier. Global in-flight maximum: 10; lane caps below do not add to a global allowance. chatgpt_astra cap 0 does not authorize premium use. Proxy premium reserve separately defaults to USD 0.

## lane-bindings

| Lane | Cap | Wire | Caller | Status | Owner | Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| gemini_flash | 6 | google-generative-ai | omp_work.model_proxy | test_verified | null | python/omp-work/src/omp_work/model_proxy.py::ProxyConfig.__post_init__; python/omp-work/tests/test_orchestrator_production_worker.py::test_production_worker |
| ollama_cloud | 4 | ollama-chat | omp_work.model_proxy | test_verified | null | python/omp-work/src/omp_work/model_proxy.py::ProxyConfig.__post_init__; python/omp-work/tests/test_orchestrator_production_worker.py::test_production_worker |
| anthropic_fable | 2 | null | null | pending_owner | Chris | null |
| kimi | 2 | null | null | pending_owner | Chris | null |
| chatgpt_astra | 0 | null | null | pending_owner | Chris | null |
| openai_codex | 1 | null | null | pending_owner | Chris | null |

## alternate-bindings

| Rule | From lane | Model | Lane | Caller | Status | Owner | Approval | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gemini-flash-overflow | gemini_flash | deepseek-v4.1-flash:cloud | ollama_cloud | omp_work.routing.router::route_stage | pending_owner | Chris | null | python/omp-work/src/omp_work/routing/substitutions.v1.json#overflow |

## Retry, escalation and missions

`retry-same-tier` retains the selected route while attempts remain within stage bounds (implement/verify: 2; others: 0). `escalate-after-retries` on `failed_attempts` and `escalate-on-review-reject` on `review_rejected` each advance one ladder step through `escalation.py::next_route`, requiring an item budget and enough remaining estimated tokens; no higher tier refuses. These helpers do not prove production invocation of higher tiers.

`engineering.execute`: plan → implement → verify → audit → acceptance_review. `research.run`: research → acceptance_review. `router.py::route_mission` reads these sequences and refuses caller model/provider fields anywhere in requests (`caller_model_fields: refuse`). These are routing mission sequences, not `orchestrator/stages.py` lifecycle order (intake, confirm, plan, grant, implement, evaluate, freeze, push, audit, merge, close).

## agent-bindings

Session YAML selectors qualified by `packages/coding-agent/test/model-policy-governance.test.ts`; no Work lane association follows from these aliases.

| Agent | Selector | Caller | Status | Evidence |
| --- | --- | --- | --- | --- |
| packages/coding-agent/src/prompts/agents/scout.md | @smol | task tool | test_verified | packages/coding-agent/test/model-policy-governance.test.ts |
| packages/coding-agent/src/prompts/agents/reviewer.md | @slow | task tool | test_verified | packages/coding-agent/test/model-policy-governance.test.ts |
| session-system/agents/auditor.md | @audit | native audit runner | test_verified | packages/coding-agent/test/model-policy-governance.test.ts |

## Assertions

| ID | Source/selector | Assertion | Authority | Caller | Scope | Disposition | Disagreement | Owner | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MP-001 | session-system/agents/auditor.md#model | Auditor agent names role `@audit`. | agent-definition | Task tool spawn of the auditor agent | active-selection | verified | | - | session-system/agents/auditor.md |
| MP-002 | session-system/agents/auditor.md#description | Auditor runs once per owner-entered `/summary` or in the `/execute` native audit runner; never for implementation. | doctrine | `/summary` and `/execute` engine | doctrine/audit-ownership | verified | | - | session-system/agents/auditor.md |
| MP-003 | session-system/agents/omp-AGENTS.md#Model escalation | Everyday work runs on the configured default worker `@default` at its configured effort. | doctrine | every session that loads omp-AGENTS.md | doctrine/default-worker | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-004 | session-system/agents/omp-AGENTS.md#Model escalation | Escalate to `@slow` on security/auth, concurrency, migrations, public API, more than 3 subsystems, two failed repair loops, or plan deviation. | doctrine | worker sessions | doctrine/slow-triggers | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-005 | session-system/agents/omp-AGENTS.md#Model escalation | `@deep` is for very large or long-horizon work; every `@deep` reference carries an explicit `:high` or `:low` suffix. | doctrine | worker sessions | doctrine/deep-effort-suffix | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-006 | session-system/agents/omp-AGENTS.md#Model escalation | `@deep` is the third-family adjudicator when worker and auditor disagree. | doctrine | worker and audit flow | doctrine/deep-adjudication | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-007 | session-system/agents/omp-AGENTS.md#Model escalation | `intake` and `deep` roles stay out of the execution cycle. | doctrine | `/execute` engine | doctrine/intake-deep-exclusion | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-008 | session-system/agents/omp-AGENTS.md#Model escalation | `audit` role belongs to the manual `/summary` auditor and the native audit runner, never to the implementation worker. | doctrine | `/summary` and `/execute` engine | doctrine/audit-role-ownership | verified | | - | session-system/agents/omp-AGENTS.md |
| MP-009 | session-system/agents/omp-AGENTS.md#Model escalation | Role assignments are owner decisions recorded in configuration. | doctrine | owner and routing changes | doctrine/owner-assignments | verified | | Chris | session-system/agents/omp-AGENTS.md |
| MP-010 | session-system/agents/omp-AGENTS.md#Model escalation | New model availability changes no assignment by itself. | doctrine | model review and routing changes | doctrine/no-automatic-replacement | verified | | Chris | session-system/agents/omp-AGENTS.md |
| MP-011 | session-system/agents/omp-AGENTS.md#Model escalation | Doctrine names roles `@default`, `@slow`, `@deep`, `intake`, `audit`; only `default` and `slow` are built-in roles. The `deep`, `intake` and `audit` assignments live in user config outside this repo. | doctrine | role resolver via user `modelRoles` | doctrine/custom-role-assignments | unresolved | Repo cannot show the effective `deep`, `intake`, `audit` assignments; a missing `modelRoles.audit` makes the audit preflight throw. | Chris | packages/coding-agent/src/config/model-roles.ts#MODEL_ROLES |
| MP-020 | packages/coding-agent/src/config/model-roles.ts#MODEL_ROLES | Built-in roles: default, smol, slow, vision, plan, commit, tiny, memory, task, advisor (chat); image, web, speech, dictation, judge (kind). | executable | role pickers and every `@role` lookup | active-selection | verified | | - | packages/coding-agent/src/config/model-roles.ts |
| MP-021 | packages/coding-agent/src/config/model-roles.ts#accepts | Each role filters its candidate pool by model kind; tiny, memory and judge also accept tiny models. | executable | `roleCandidatePool` and model browser | active-selection | verified | | - | packages/coding-agent/src/config/model-roles.ts |
| MP-022 | packages/coding-agent/src/config/model-roles.ts#getKnownRoleIds | Custom roles (for example `audit`, `deep`) come in through `modelRoles`, `modelTags` or `cycleOrder` and accept chat models. | executable | role selector UI and resolver | active-selection | verified | | - | packages/coding-agent/src/config/model-roles.ts |
| MP-023 | packages/coding-agent/src/config/model-roles.ts#prefixes | `@role` is the canonical selector, `*` means default, `pi/` is a legacy prefix. | syntax | resolver parsing | syntax-example | verified | | - | packages/coding-agent/src/config/model-roles.ts |
| MP-024 | packages/coding-agent/src/config/model-roles.ts#LEGACY_MODEL_ROLE_ALIAS_PREFIX | `pi/` role prefix is kept only for old configs. | syntax | resolver parsing | historical | verified | | - | packages/coding-agent/src/config/model-roles.ts |
| MP-030 | packages/coding-agent/src/config/model-settings.ts#modelRoles | `modelRoles` holds role-to-selector assignments; default storage is global, optional per-project. | configuration | settings layer, resolver | active-selection | verified | | - | packages/coding-agent/src/config/model-settings.ts |
| MP-031 | packages/coding-agent/src/config/model-settings.ts#cycleOrder | Default model cycle order is smol, default, slow. | configuration | model cycling | active-selection | verified | | - | packages/coding-agent/src/config/model-settings.ts |
| MP-032 | packages/coding-agent/src/config/model-settings.ts#modelPresets | Presets snapshot all role assignments and default thinking level. | configuration | `/modelpreset` and model hub | active-selection | verified | | - | packages/coding-agent/src/config/model-settings.ts |
| MP-033 | packages/coding-agent/src/config/model-settings.ts#enabledModels | `enabledModels`, `enabledProviders`, `disabledProviders`, `modelProviderOrder` narrow and order the pool. | configuration | model scope resolution | active-selection | verified | | - | packages/coding-agent/src/config/model-settings.ts |
| MP-040 | packages/coding-agent/src/config/model-resolver.ts#resolveConfiguredRolePattern | Precedence for a role: configured assignment, then configured fallback role (advisor to slow if set; tiny to smol; memory to tiny), then configured `default` (smol and slow only), then priority.json chain. | executable | `resolveModelRoleValue`, `resolveRoleChain` | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-041 | packages/coding-agent/src/config/model-resolver.ts#ROLE_PRIORITY_ALIAS | advisor uses the slow chain; tiny and memory use the smol chain. | executable | `rolePriorityDefaults` | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-042 | packages/coding-agent/src/config/model-resolver.ts#ROLE_CONFIGURED_FALLBACK | advisor never inherits the primary model; it needs a configured slow or its own chain. | executable | `resolveAdvisorRoleSelection` | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-043 | packages/coding-agent/src/config/model-resolver.ts#resolveRoleChain | A role chain is the configured selector, then `retry.fallbackChains[role]` if set, else priority.json defaults; the first available match wins and duplicates collapse. | executable | retry and fallback logic | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-044 | packages/coding-agent/src/config/model-resolver.ts#resolveAgentModelSelection | First nonempty resolved selection wins: request model, settings override, then agent frontmatter. Resolved `@task` (or legacy `pi/task`) uses its assignment; single default aliases inherit. Otherwise unresolved or absent frontmatter falls back to active session model, caller fallback, then configured default. | executable | task tool agent spawn | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-045 | packages/coding-agent/src/config/model-resolver.ts#resolveAgentAdvisorSelection | Per-agent `advisor` frontmatter: true uses the advisor role, a string is a custom pattern. | executable | task tool agent spawn | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-046 | packages/coding-agent/src/config/model-resolver.ts#findSmolModel | Legacy helpers `findSmolModel` and `findSlowModel` walk the priority chain directly with substring fallback, then the first available model. Direct priority.json readers also exist in commit and skill-description code. | executable | commit message generator, skill descriptions, commit model selection | active-selection | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-047 | packages/coding-agent/src/config/model-resolver.ts#parseModelPattern | Pattern grammar: `provider/id`, fuzzy id match, `:thinking` suffix, comma lists. | syntax | CLI and settings parsing | syntax-example | verified | | - | packages/coding-agent/src/config/model-resolver.ts |
| MP-050 | packages/coding-agent/src/priority.json#smol | smol chain starts `google-antigravity/gemini-3.8-flash` and ends `-mini`. | executable | smol, tiny, memory roles; commit and skill-description code | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-051 | packages/coding-agent/src/priority.json#slow | slow chain starts `openai-codex/gpt-5.6-sol`, then Fable 5.1, Fable 5, Kimi K3, GLM 5.3, Opus 5.5. | executable | slow and advisor roles; reviewer agent | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-052 | packages/coding-agent/src/priority.json#slow | Tail entries `claude-opus-4-8`, `gpt-5.5` and older names remain live last-resort matches; model age does not make fallback policy historical. | executable | slow and advisor roles | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-053 | packages/coding-agent/src/priority.json#image | image chain starts `openai/gpt-image-2`. | executable | image role in settings, `generate_image` tool | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-054 | packages/coding-agent/src/priority.json#web | web chain starts `web/parallel` and lists providers, not models. | executable | web search role in settings | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-055 | packages/coding-agent/src/priority.json#speech | speech chain starts `local/kokoro`. | executable | speech role, TTS tool | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-056 | packages/coding-agent/src/priority.json#dictation | dictation chain is `local/parakeet-tdt-0.6b-v3`. | executable | dictation role, STT controller | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-057 | packages/coding-agent/src/priority.json#judge | judge chain starts `typesafe/jev-latest`, then OpenRouter route, `@tiny`, `@smol`, `@default`. | executable | judge role, judgment runner | active-selection | verified | | - | packages/coding-agent/src/priority.json |
| MP-060 | packages/coding-agent/src/prompts/agents/reviewer.md#model | reviewer agent pins `@slow`. | agent-definition | task tool reviewer spawn | active-selection | verified | | - | packages/coding-agent/src/prompts/agents/reviewer.md |
| MP-061 | packages/coding-agent/src/prompts/agents/scout.md#model | scout agent pins `@smol`. | agent-definition | task tool scout spawn | active-selection | verified | | - | packages/coding-agent/src/prompts/agents/scout.md |
| MP-062 | packages/coding-agent/src/prompts/agents/frontmatter.md#model | Template emits `model:` only when the agent has one; effective selection follows request, settings override and inheritance precedence in MP-044. | syntax | agent definition rendering | syntax-example | verified | | - | packages/coding-agent/src/prompts/agents/frontmatter.md |
| MP-063 | packages/coding-agent/src/prompts/agents/model-mention.md#selector | A mentioned model becomes a general-purpose agent pinned to that selector. | agent-definition | user model mention | active-selection | verified | | - | packages/coding-agent/src/prompts/agents/model-mention.md |
| MP-064 | packages/coding-agent/src/prompts/agents/task.md | task, init and security-reviewer definitions set no model; they inherit through the task role or session model. | agent-definition | task tool spawn | active-selection | verified | | - | packages/coding-agent/src/prompts/agents/task.md |
| MP-070 | session-system/extensions/workflow/auditor-runner.ts#resolveAuditModel | Resolves `@audit` through `ctx.models` and throws when `modelRoles.audit` is unset. | executable | native audit runner | preflight | verified | | - | session-system/extensions/workflow/auditor-runner.ts |
| MP-071 | session-system/extensions/workflow/auditor-runner.ts#preflight | Preflight checks provider credentials and one live transport probe on the audit model before launch. | executable | native audit runner | preflight | verified | | - | session-system/extensions/workflow/auditor-runner.ts |
| MP-072 | session-system/extensions/workflow/auditor-runner.ts#modelRole | The audit subprocess runs with `modelRole: "audit"` and the agent's `model` as override. | executable | audit subprocess | active-selection | verified | | - | session-system/extensions/workflow/auditor-runner.ts |
| MP-073 | session-system/extensions/workflow/auditor-runner.ts | `@audit` is a role resolved by the runner and the agent definition; it is not a routing stage by itself. | executable | native audit runner | doctrine/audit-not-routing-stage | unresolved | Policy audit stage and admitted reviewer do not bind session @audit; effective assignment and invocation remain unverified. | Chris | session-system/extensions/workflow/auditor-runner.ts |
| MP-080 | docs/models.md#Role aliases and settings | Documents the role table, alias syntax, inheritance, advisor/tiny/memory fallbacks. | configuration | readers; mirrors resolver | active-selection | verified | | - | docs/models.md |
| MP-081 | docs/models.md#Model presets | Documents preset save and switch. | configuration | readers | syntax-example | verified | | - | docs/models.md |
| MP-082 | docs/models.md#models.yml shape | Provider and model definition sections (`models.yml` shape, validation, merge order, discovery, auth). | configuration | model registry loader | catalog | verified | | - | docs/models.md |
| MP-083 | docs/models.md#Practical examples | Example configs only; they assign no mission role. | syntax | none | syntax-example | verified | | - | docs/models.md |
| MP-084 | docs/models.md#Context promotion | Context promotion and `retry.fallbackChains` are request-failure fallbacks, not role assignment. | configuration | session retry logic | active-selection | verified | | - | docs/models.md |
| MP-090 | packages/catalog/src/models.json | Bundled model metadata (generated). | catalog | model registry | catalog | verified | | - | packages/catalog/src/models.json |
| MP-091 | packages/catalog/src/provider-models/descriptors.ts | Provider discovery descriptors and default models. | catalog | model discovery | catalog | verified | | - | packages/catalog/src/provider-models/descriptors.ts |
| MP-092 | packages/catalog/src/compat/rules/README.md | KDL rule tree owns identity, model-lineage, provider and runtime policy. | catalog | `resolveModelPolicy`, `buildModel` | catalog | verified | | - | packages/catalog/src/compat/rules/README.md |
| MP-093 | packages/catalog/src/compat/rules/runtime/behavior.kdl | Runtime heuristics (api routes, limits, exclusions); no mission assignment. | catalog | `resolveModelPolicy` | catalog | verified | | - | packages/catalog/src/compat/rules/runtime/behavior.kdl |
| MP-094 | docs/cpk6-advisor-supervision-retirement.md | Legacy advisor path is retired behind the structured supervisor; owner signs off deletion. | configuration | advisor supervision setting | experimental | verified | | - | docs/cpk6-advisor-supervision-retirement.md |
| MP-095 | docs/model-review.md | Qualification precedes routing; new listings trigger evaluation, never replacement. | doctrine | model review process | doctrine/no-automatic-replacement-review | verified | | - | docs/model-review.md |
| MP-100 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages | Declared stages, reviewers, tiers, caps and alternates are tabulated above; route authority does not prove calls. | executable | load_policy and route_stage | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-101 | python/omp-work/src/omp_work/routing/router.py#route_mission | Policy mission order and caller model/provider refusal govern routing. | executable | mission routing | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/router.py |
| MP-102 | python/omp-work/src/omp_work/model_proxy.py | Implement task grants select primary/overflow routes; exact Gemini/Ollama transport association has test evidence only. | executable | implementation worker | active-selection | verified |  | - | python/omp-work/src/omp_work/model_proxy.py |
| MP-103 | python/omp-work/src/omp_work/orchestrator/operations.py#run_plan | Plan builds packets; evaluate executes tests; audit launches verification; none establishes declared plan/verify/review model invocation. | executable | orchestrator service | active-selection | unresolved | Declared roles lack invocation evidence. | Chris | python/omp-work/src/omp_work/orchestrator/operations.py |
| MP-104 | python/omp-work/src/omp_work/orchestrator/service.py | Supervisor coordinates durable stage operations as software, not an LLM. | executable | orchestrator service | active-selection | verified |  | - | python/omp-work/src/omp_work/orchestrator/service.py |
| MP-105 | python/omp-work/src/omp_work/orchestrator/stages.py#decide | Deterministic lifecycle evaluate is separate from routing verify; stage movement invokes no model. | executable | orchestrator service | active-selection | verified |  | - | python/omp-work/src/omp_work/orchestrator/stages.py |
| MP-106 | python/omp-work/src/omp_work/intake_support_admission.py#resolve_admission | Research route budget admission and supplied judge usage are not research/judge invocation. | executable | intake support admission | active-selection | unresolved | No verified production research/judge caller or judge binding. | Chris | python/omp-work/src/omp_work/intake_support_admission.py |
| MP-107 | docs/programme/ORIGINAL-OBLIGATIONS.md#O2 | Historical exact model names are not restored; current policy is baseline. | doctrine | programme reconciliation | historical | verified |  | - | docs/programme/ORIGINAL-OBLIGATIONS.md |
| MP-108 | docs/programme/GOAL-PROMPT.md | Historical economy/planning assertions retain independent audit and judge verification obligations, not proof of execution. | doctrine | programme reconciliation | historical | verified |  | - | docs/programme/GOAL-PROMPT.md |
| MP-109 | docs/programme/CONTINUATION-GOAL.md | Historical Fable planning and Astra coordination yield to current routes and D33; acceptance obligations remain. | doctrine | programme reconciliation | historical | verified |  | - | docs/programme/CONTINUATION-GOAL.md |
| MP-110 | docs/programme/ACCEPTED-DECISIONS.md#G5 | Quality and independence govern reviewer reconciliation; item-specific Fable publication/native Sol audit gates remain. | doctrine | acceptance reconciliation | doctrine/audit-obligations | verified |  | - | docs/programme/ACCEPTED-DECISIONS.md |
| MP-111 | docs/adr/0001-autonomous-execution-and-qualification.md | REQUIRED independent audit, effective audit model and native acceptance remain mandatory. | doctrine | qualification | doctrine/qualification | verified |  | - | docs/adr/0001-autonomous-execution-and-qualification.md |
| MP-112 | docs/adr/0005-mission-first-architecture.md | Run Owner is a role, not a persistent LLM; model proposals grant no authority. | doctrine | mission orchestrator | doctrine/run-owner | verified |  | - | docs/adr/0005-mission-first-architecture.md |
| MP-113 | docs/adr/0006-on-demand-roles.md | D33 supersedes supervisor-model judgment; roles are on-demand, with no new model-family rule. | doctrine | mission orchestrator | doctrine/deterministic-supervisor | verified |  | - | docs/adr/0006-on-demand-roles.md |
| MP-114 | docs/orchestrator-worker.md | Worker ids, transport and configuration values are examples, not routing assignments or live qualification. | syntax | none | syntax-example | verified |  | - | docs/orchestrator-worker.md |
| MP-115 | session-system/extensions/workflow/session-ledger.ts | Advisor note generation and synthetic Chris attribution do not bind operator model/lane. | executable | settled-turn note | active-selection | unresolved | Operator session assignment and synthetic attribution need Chris reconciliation; advisor is not operator evidence. | Chris | session-system/extensions/workflow/session-ledger.ts |
| MP-116 | python/omp-work/src/omp_work/routing/substitutions.v1.json | Prior substitution records have null approvals; authority settled, overflow approval still pending. | configuration | substitution reconciliation | historical | unresolved | Overflow lacks Chris approval; prior plan disposition retained, not approved here. | Chris | python/omp-work/src/omp_work/routing/substitutions.v1.json |
| MP-117 | docs/omp-routing-substitutions.md | Prior governing-file uncertainty settled by packaged policy; pending approvals retained. | configuration | substitution reconciliation | historical | unresolved | No overflow approval recorded. | Chris | docs/omp-routing-substitutions.md |
| MP-118 | python/omp-work/src/omp_work/routing/escalation.py#next_route | Retry retains route; rejection or exhausted retries escalates within remaining item budget. | executable | retry/escalation routing | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/escalation.py |
| MP-119 | packages/coding-agent/test/model-policy-governance.test.ts | Offline YAML/resolver/native audit evidence distinguishes session selections from Work lanes and owner assignments. | configuration | governance qualification | preflight | verified |  | - | packages/coding-agent/test/model-policy-governance.test.ts |
| MP-120 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#tiers | E1..E4 tier model/lane and estimated-token bindings govern escalation, not initial stage overrides. | executable | next_route | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-121 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#concurrency | Global in_flight_max is 10; six provider caps are lane partitions, not API identities. | executable | router, parallel stream admission and model proxy | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-122 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#alternates | Gemini saturation selects Ollama when room exists; otherwise routing refuses provider_saturated. Pending approval is not inferred from shipped behavior. | executable | route_stage | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-123 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#retry | retry-same-tier retains selected provider/model while stage retries remain. | executable | next_route | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-124 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#escalation | Failure exhaustion or review rejection advances one tier within remaining item budget. | executable | next_route | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-125 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#missions | engineering.execute and research.run select their ordered stage routes, not orchestrator lifecycle operations. | executable | route_mission | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-126 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.audit.reviewers | Admitted reviewer openai-codex/gpt-5.6-sol:medium is distinct from audit route kimi/kimi-code/k3-256k. | executable | check_reviewer | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
| MP-127 | python/omp-work/src/omp_work/routing/routing-policy.v1.json#stages.acceptance_review.reviewers | Admitted reviewer kimi-code/k3-256k establishes admission, not a call or lane/wire mapping. | executable | check_reviewer | active-selection | verified |  | - | python/omp-work/src/omp_work/routing/routing-policy.v1.json |
