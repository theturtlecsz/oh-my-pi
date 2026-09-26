# ECC-centered OMP implementation plan

Prepared September 15, 2026.

**Decision:** Make ECC the default upstream for reusable engineering behavior and compatible utilities. Mirror its complete source, maintain a small OMP adaptation layer, and make relevant ECC modules available through OMP's existing execution system.

**Deliverable status:** This document is an implementation handoff. The described adapter, commands, manifests, and work packages are proposed; they have not been installed or implemented by this review. Work-package labels below are planning labels, not newly created Work Ledger items.

## 1. Outcome and decision rules

The intended outcome is an OMP installation that completes ordinary development tasks reliably with inexpensive workers, spends frontier capacity on specific reasoning needs, and benefits from ECC improvements without repeated local reinvention.

The user's latest direction supersedes the earlier “take approximately 15%” framing. There is no adoption percentage target. ECC gets the presumption of reuse.

Apply these rules in order:

1. Reuse a compatible ECC asset unchanged.
2. Adapt its host-specific metadata, paths, tool interface, or output contract with a small reviewed transformation.
3. Preserve its substantive method while mapping execution into an existing OMP facility.
4. Defer activation only for a named incompatibility, unresolved dependency, policy conflict, or maturity limitation.
5. Build a new OMP implementation when an existing OMP capability and the inspected ECC material cannot meet the requirement.

A policy exception must identify the exact conflicting behavior and where the controlling requirement comes from. “Our implementation is stronger” is not sufficient evidence to exclude reusable material.

The source mirror can be broad while active context remains task-specific. Keeping a complete local catalog does not require advertising every skill to every worker or running every hook.

### What the hackathon evidence means for this plan

ECC's author describes winning the Anthropic x Forum Ventures hackathon with Zenith built through agentic workflows. Zenith's own site identifies it as the winning project; the organizer documents the hackathon. This is useful practitioner provenance and supports an adoption preference. The award is associated with the product and its creators; this review has not established a controlled performance comparison of ECC against OMP. The implementation strategy is to preserve more of that working approach and test the OMP adaptations.

Sources: [ECC background](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/README.md#background), [Zenith](https://zenith.chat/), [organizer](https://www.forumvc.com/forum-ventures-x-anthropic-ai-hackathon).

### Working constraints retained from the agreed OMP design

- WorkService remains the authority for scope, authorization, work identity, evidence, cancellation, and completion.
- Preserve the existing backlog and find related work before creating duplicate implementation items.
- Preserve independent audit, immutable candidate binding, and required command gates.
- Keep the Task Observer session-start requirement. Optional profiles do not silently disable an owner-approved mandatory read.
- Keep the selected OMP memory backend as the durable learning destination.
- Preserve bounded inexpensive best-of-N where already planned or authorized; generation, selection, and acceptance remain separate responsibilities.
- Resolve actual model assignments and effort from effective configuration. A role name is not proof of price, capability, or availability.
- Use a qualified installation for real work and a separate development checkout for changes to OMP.
- Treat the existing stabilization plan as the production qualification route. Historical checkboxes and old CI statements in that document are not current runtime evidence.

## 2. Source baseline and reuse boundary

Reviewed repository revisions:

| Repository | Revision | Role |
| --- | --- | --- |
| affaan-m/ECC | 8321021c54d670126ce3b2969d5deb880b4b0c2a | Upstream behavior, assets, examples, and utilities |
| theturtlecsz/oh-my-pi | 1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1 | Native host, authority integration, and existing implementation |

Both main references were checked for this handoff. The ECC package at that revision identifies itself as version 2.2.1. The source branch does not establish what is installed on the user's machine.

ECC already provides install profiles, module and component manifests, install planning, target adapters, link rewriting, ownership tracking, and lifecycle inspection. Start from these facilities. They are internal implementation interfaces, not a documented stable external SDK; pin their revision and qualify the consumed interfaces.

There is no OMP target in the inspected install-target registry. The Pi adapter's special handling for compiled OMP/Bun concerns hook execution. It does not establish complete compatibility with this fork's WorkService workflow.

| Responsibility | Owner after implementation |
| --- | --- |
| General engineering methods and specialist reference content | ECC, with recorded OMP adaptations |
| Profile/module taxonomy and reusable install helpers | ECC wherever its contracts fit |
| OMP discovery, role/tool mapping, and native output translation | Small OMP adapter |
| Task scope, grants, candidate identity, evidence, and completion | Existing WorkService and workflow host |
| Agent execution, tool grants, cancellation, model resolution | Existing OMP runtime |
| Durable observations and learned material | Existing Task Observer and selected memory backend |
| Active deployment and rollback | Existing qualified installation process |
| Adoption and retirement decisions | Existing owner-approved change process, informed by task results |

## 3. Repository and installation design

### Source layout

Use a pinned Git submodule for the complete ECC source. This is the default packaging choice for the handoff; if OMP has an established equivalent vendor mechanism at implementation time, use that mechanism and record the mapping.

| Proposed path | Purpose |
| --- | --- |
| session-system/ecc/upstream/ | Exact upstream checkout; no edits to canonical files |
| session-system/ecc/manifest.json | OMP asset mapping, upstream identity, transforms, activation, qualification |
| session-system/ecc/overlays/ | Small reviewed semantic patches with reasons |
| session-system/ecc/adapter/ | OMP target, transforms, inventory, and qualification tooling |
| session-system/ecc/README.md | Update, build, activation, diagnosis, and rollback instructions |
| session-system/tests/ecc-adaptation.test.ts | Meaningful discovery, translation, dependency, and ownership tests |
| session-system/tests/ecc-install.test.ts | Project-scoped installation and removal tests |

The full mirror is maintenance/reference material. Only an explicitly selected build output is exposed through native discovery paths.

Build adapted assets into the qualified release. Runtime links must resolve to that release, never to a moving development checkout. Include the source identity and adapted output hashes in the release manifest. CI must initialize the pinned source before building; qualified runtime operation must not require fetching upstream.

### Installation behavior

- Start with project-local installation.
- Use namespaced rule filenames and skill names to avoid overriding local assets.
- Keep upstream names and paths in metadata so updates remain traceable.
- Install only the selected assets and the dependency closure needed to use them correctly.
- Preserve relative references, helper scripts, and supporting files when required. A working top-level SKILL.md is not a complete installation if its instructions refer to missing files.
- Reuse ECC's link rewriting where applicable. Its inspected helper handles a limited class of Markdown links; separately check command paths, reference-style links, internal skill URLs, and renamed identifiers.
- Preserve user-owned files and locally modified installed assets. Report conflicts instead of taking ownership silently.
- Installation state describes owned files; it does not become another task or permission authority.
- Removal deletes only artifacts still identified as owned by this installation. Handle user modifications explicitly.
- Do not install command aliases that displace native /intake, /plan, /execute, /summary, or /done.
- A failed optional module activation must leave the previous installation usable. It must not produce a partially updated live bundle.

### Reuse the existing installer seams

Inspect and consume these upstream facilities before implementing equivalents:

- manifests/install-profiles.json
- manifests/install-modules.json
- manifests/install-components.json
- scripts/lib/install-manifests.js
- scripts/lib/install-targets/registry.js
- scripts/lib/install/link-rewrite.js
- scripts/lib/install/ownership-guard.js
- scripts/lib/install-lifecycle.js

The initial OMP adapter should add a host mapping over the upstream manifests. ECC's target restrictions remain meaningful: create an explicit compatibility mapping for OMP instead of pretending an existing target is equivalent or bypassing validation.

Keep any unavoidable modifications to upstream planning functions as small tracked patches. Prefer an OMP wrapper when it can use existing read-only planning and transformation interfaces without importing unrelated state-store or CLI behavior.

## 4. Per-asset contract

Each asset record needs:

- Stable OMP identifier and original upstream path.
- Upstream repository, tag/version label if available, and resolved commit SHA.
- Original content hash, transformation version, and adapted content hash.
- Destination, installation scope, and selected profiles.
- Dependencies, including script runtimes and internal references.
- Adaptation type: unchanged, metadata translation, path translation, or semantic overlay.
- A concise reason for every semantic deviation.
- Activation mechanism and its actual guarantee.
- Model role and explicit tool grant where relevant.
- Expected persistent state and its existing owner.
- Mechanical test references and last qualified release.
- Behavioral trial evidence where useful; keep this distinct from mechanical tests.
- Context overhead estimate and its estimation method; actual usage when observed.
- Owned-file removal and rollback behavior.

Example record shape, with placeholders intentionally left unresolved:

~~json
{
  "id": "ecc-rust-style",
  "upstream": {
    "repository": "affaan-m/ECC",
    "commit": "<resolved commit SHA>",
    "path": "rules/rust/coding-style.md",
    "sha256": "<original content hash>"
  },
  "target": {
    "path": ".omp/rules/ecc-rust-style.md",
    "scope": "project"
  },
  "adaptation": {
    "kind": "metadata-and-semantic-overlay",
    "transformVersion": "<adapter revision>",
    "overlay": "<reviewed patch path>",
    "sha256": "<adapted content hash>"
  },
  "activation": {
    "mode": "rulebook",
    "globs": ["**/*.rs"],
    "description": "Rust engineering guidance relevant to this project",
    "selection": "model-driven retrieval"
  },
  "dependencies": [],
  "permissions": [],
  "qualification": {
    "mechanicalTests": ["<test references>"],
    "behavioralTrial": null,
    "qualifiedRelease": null
  },
  "cost": {
    "catalogTokensEstimate": null,
    "bodyTokensEstimate": null,
    "estimator": null
  },
  "rollback": {
    "removeOwnedArtifactOnly": true
  }
}
~~

The final dependencies list must describe the adapted content. Empty dependencies are valid only after inherited references and helper requirements have actually been resolved.

### Activation semantics

| Mechanism | Mechanical guarantee | Behavioral question |
| --- | --- | --- |
| Rulebook: description and optional globs | Entry appears in the rule catalog and is addressable through rule:// | Does the model retrieve it on relevant tasks? |
| Always-apply rule | Full body is included automatically in the applicable prompt | Is its recurring context cost justified? |
| TTSR condition or AST condition with scope | Native matcher can trigger on the specified stream/path conditions | Does intervention help without repetitive noise? |
| Skill discovery | Enabled skill is described and can be loaded through the native skill path | Does selection match its intended task? |
| Advisor specialization | Configured advisor receives its allowed context and tool grant | Are its findings useful and economical? |

ECC language rules use paths metadata. OMP's inspected native rule parser does not translate it. Translate it into an appropriate native contract; do not mark an asset qualified simply because discovery found the file.

For broad language guidance, default to rulebook retrieval. A Rust edit does not mechanically force a rulebook read, and the catalog entry may remain visible in a TypeScript session. Test discoverability mechanically; assess appropriate retrieval behavior in the task trial.

Reserve TTSR for narrow checks that need native triggering. Use fresh sessions when testing positive and negative activation so previous retrieval does not contaminate the result.

A skill's manifest trigger is an intended selection condition, not an invented host-enforced metadata field.

## 5. Adoption map

Mirror every upstream asset. Give each a disposition so “not active yet” is visible and explainable:

- Supported without changes.
- Supported with an OMP adaptation.
- Available as reference material.
- Deferred pending a specific dependency or compatibility task.
- Excluded from the current phase because of a named policy or authority conflict.

A newly added upstream asset starts mirrored and inactive until classified. Catalog coverage is a maintenance measure, not a productivity score.

| ECC family | Adoption direction | OMP integration |
| --- | --- | --- |
| Engineering workflow skills | Preserve the methods and useful checklists broadly | Existing intake, planning, execution, and review stages |
| Language and database guidance | Enable matching project packs | Native rulebook and skill discovery |
| Specialist agent prompts | Reuse role knowledge and review questions | Existing agent definitions or adapted advisors |
| Commands | Reuse useful prompt content | Existing native command owners; avoid alias collisions |
| Hook profiles and dispatch helpers | Reuse compatible code and conventions | Native lifecycle events with explicit event contracts |
| Context and session continuity | Reuse retrieval, compression, and handoff methods | Existing context machinery and authoritative state reload |
| Continuous learning and memory vault ideas | Reuse evidence, scope, trust, and retrieval patterns | Task Observer, learn/manage_skill, active backend |
| Configuration security | Qualify the separate AgentShield package | Diagnostic first; add a gate only for demonstrated coverage |
| Evaluations and review workflows | Reuse fixtures, structured output, and suitable utilities | Existing OMP tests and independent acceptance route |
| Plan Canvas | Reuse presentation and annotation capabilities later | Native owner review and ledger-backed decisions |
| ECC2 and alternative orchestration runtimes | Retain in the mirror as reference | No competing authority/runtime activation in this phase |

### Initial workflow pack

These are exact upstream skill directories present at the reviewed revision:

- search-first
- intent-driven-development
- contract-first
- verification-loop
- iterative-retrieval
- dynamic-workflow-mode
- agentic-engineering
- product-capability
- tdd-workflow

Preserve their useful method. Adapt references to Claude-only paths, tools, model names, and supplementary commands. Apply task-appropriate verification requirements from the approved scope. Do not reintroduce a blanket coverage target through a second selected skill after removing it from the first.

### Maintenance and diagnosis pack

- agent-architecture-audit
- loop-design-check
- benchmark-optimization-loop
- skill-stocktake
- rules-distill
- context-budget

Make these available for appropriate diagnostic work rather than adding a maintenance agent to every task. Adapt the stocktake/distillation helper paths and result locations; reuse their helpers where they fit.

Context-budget estimates remain estimates. Token counts from real requests and existing provider accounting are the performance evidence.

### Domain packs for OMP

- Rust: rules/rust/, rust-patterns, rust-testing, and adapted rust-reviewer.
- TypeScript: rules/typescript/ and relevant general architecture/review skills.
- Python: rules/python/, python-patterns, python-testing, and adapted python-reviewer.
- PostgreSQL: postgres-patterns, database-migrations, and adapted database-reviewer.
- Security: relevant security rules, security-reviewer, and a separately qualified security-scan integration.

Python and PostgreSQL are first-class here because they cover the WorkService authority layer. There is no typescript-patterns skill directory at the inspected revision; do not invent it.

OMP's bundled Rust and TypeScript TTSR rules are narrow checks. Compare for conflicts and overlap, while retaining broader ECC guidance that adds value.

### Model and tool translation

Map explicit upstream provider/model names to OMP roles. Resolve those roles from effective configuration at execution time. Preserve strict output contracts over conversational styling preferences.

Translate tool names and instructions into tools actually granted in the destination role. A prompt that asks for Bash is not permission to add shell access.

For an advisor adaptation:

- Supply a specific review purpose.
- Use read, grep, and glob initially.
- Remove instructions to run the full test suite on every update.
- Supply the actual candidate/work boundary; do not infer it from HEAD~1.
- Route findings through the existing advise interface.
- Preserve the fact that advice is not acceptance authority.

## 6. Work packages

Create or update the corresponding Work Ledger entries during implementation. Search the backlog first. Keep each package independently reviewable and do not claim completion until its actual acceptance route passes.

### WP1 — Establish the installed baseline and complete mirror

Deliver:

1. An effective-configuration inventory that records the loaded CLI, extension, service, schema, auditor, and relevant instruction identities.
2. Resolved model roles, effort, fallback choices, enabled advisors, memory backend, skill/rule providers, and optional hooks.
3. The pinned source mirror and a catalog derived from ECC manifests plus a filesystem reconciliation for unclassified assets.
4. An initial compatibility/disposition report for every asset family.

Avoid logging secrets or raw credentials. Distinguish configured, discovered, enabled, and actually invoked.

Acceptance:

- Upstream revision and content identity are reproducible.
- No imported source is automatically executed or injected merely because it was mirrored.
- The inventory can explain which source supplied an active rule/skill/role.
- The existing installation and native commands behave as before.
- Missing access to the real installation is reported as unknown; source defaults are not substituted as observations.

### WP2 — Build the OMP adapter and qualified installation

Deliver:

1. OMP profile and destination mappings over ECC's existing module taxonomy.
2. Deterministic metadata and path transforms.
3. Reviewed semantic overlays.
4. Dependency closure, reference validation, and ownership-aware installation.
5. Inspection, planned-change preview, validation, and removal entry points using the existing installer conventions where possible.

Initially consume the upstream helpers without requiring its entire CLI/control plane. Pin internal helper contracts.

Acceptance:

- A rulebook rule is addressable with the expected adapted content.
- Matching/selection metadata is preserved accurately.
- Skills can load their actual references and required helper scripts.
- User-owned files and native command names are preserved.
- Repeated installation is idempotent.
- Removal and interrupted activation leave a coherent installation.
- Tests cover at least one language rule, one script-dependent skill, and one advisor prompt mapping.
- Disabled and reference-only assets never appear as automatically active.

Prefer generated release files over a web of links into mutable source. Where symlinks are used, verify their stable release targets.

### WP3 — Mirror ECC's development workflow through OMP

Deliver a project-scoped developer pack covering the initial workflow skills and matching domain guidance.

Map the workflow as follows:

| Stage | ECC method retained | Native result |
| --- | --- | --- |
| Intake | Clarify intent, examine existing solutions, distinguish fixed requirements from preferences | Existing intake record and accepted criteria |
| Plan | Identify contracts, dominant risks, verification, and work boundaries | Existing approved plan and execution scope |
| Implementation | Retrieve relevant context, use applicable language patterns, run task-appropriate checks | Candidate changes and real check evidence |
| Review | Structured findings, evidence, contextual verification | Existing independent audit route |
| Completion | Record result, useful lessons, unresolved work | Existing closeout and observation paths |

Do not add a separate planning document hierarchy or second backlog. A reuse record should live in the existing intake/plan material and state what was inspected, what was adopted, and why a custom approach was needed.

Run a small ordinary coding trial with model assignments and existing assistance settings held stable. Start with 5–10 representative tasks; include Python/PostgreSQL work, a TypeScript change, a Rust task when available, and work outside OMP itself.

Acceptance:

- Native command ownership remains intact.
- Broad guidance is retrievable without injecting full bodies into every prompt.
- Relevant skills resolve real tools and paths.
- No unintended mandatory reviewer, coverage rule, commit, push, or approval action is introduced.
- The trial records results, interventions, total usage, and unexpected workflow expansion.

Portable, low-impact content does not require an individual research experiment for every paragraph. Mechanical checks plus a bounded bundle trial are sufficient for the first adoption decision.

### WP4 — Make independent audit output structurally reliable

Deliver a typed internal audit result and a deterministic renderer for the existing external headed report.

Suggested internal fields:

- Verdict.
- Findings with severity, acceptance-criterion references, source locations, evidence, impact, and remediation.
- Acceptance coverage.
- Out-of-scope material.
- Checks run and their evidence.
- Remaining questions.

Preserve independent context and candidate binding. Derive trusted task/candidate identity from the runner and WorkService; do not accept model-supplied identity as authoritative.

Use ECC's structured review workflow as an implementation reference. It is a pilot, so qualify the adapted behavior within OMP.

Acceptance:

- A valid typed result renders the existing report contract consistently.
- Missing evidence, contradictory verdicts, invalid coverage, unknown required values, and wrong candidate identity cannot become PASS through defaults or formatting.
- Tool/runner failures remain distinguishable from review findings.
- Updated agent schemas and renderer code are included in the relevant judge/release fingerprints.
- Resume across an incompatible audit contract is refused precisely.
- Existing mandatory audit model policy is preserved. Qualifying a cheaper auditor is a separate decision.

The renderer removes formatting work from the model. It does not establish correctness of the review.

### WP5 — Account for all usage and apply economical execution recipes

Deliver per-task usage aggregation and an effective execution recipe using existing model roles.

Count:

- Worker and subagent requests.
- Planning or architecture calls.
- Every best-of-N candidate, including discarded candidates.
- Candidate selection.
- Advisors and repeated verification.
- Audit transport probes and audit attempts.
- Retries and recovery calls.
- Learning, summarization, and model-assisted retrieval when enabled.

The inspected audit preflight happens before a launch reservation. Record its usage with the parent session/work correlation even when no audit launch is reserved. Do not fabricate an attempt identifier to make accounting convenient.

Use stable request identifiers to prevent double counting across parent summaries, subprocess reports, and telemetry. Separate measured usage, estimated prices, premium-request consumption, and unknown/unpriced usage. A missing price is not zero cost.

Recipes should be small data/config choices:

| Task | Default assistance |
| --- | --- |
| Bounded understood change | Qualified inexpensive worker, relevant ECC guidance, deterministic checks, required audit |
| Uncertain bug | Bounded investigation and discriminating checks before implementation |
| Consequential contract change | Focused architecture/planning reasoning, then bounded implementation |
| Candidate-generation task | Existing bounded inexpensive best-of-N, independent selection, required acceptance |

Do not introduce a model router agent that spends a request deciding the model for every request. Use task classification already available in intake/plan/configuration. Frontier escalation should record a concrete remaining reasoning need.

Preserve existing grant/attempt limits. Add per-role or per-task limits only through the normal approved configuration path. Freeze the selected recipe during a comparison run.

Acceptance:

- A task receipt reconciles constituent requests, including preflight and discarded work.
- Unknown usage remains visible.
- Limits and cancellation behave predictably.
- No mandatory audit or Task Observer requirement is removed by an optional profile.
- A cheaper model label is never treated as proof that the role is qualified.

### WP6 — Add one adapted advisor specialization

Start with one relevant ECC specialization. Select security, Rust, or database review based on the actual task mix and observed failures.

Deliver:

- Read-only investigative grant.
- Explicit activation and stop conditions.
- Bounded review scope.
- Evidence-oriented finding instructions.
- Usage and intervention accounting.

Reuse OMP's existing emission guard, deduplication, interruption rules, and cancellation behavior. Do not rebuild them.

Trial the specialization separately from content changes. Compare useful findings, false alarms, repeated checks, total usage, and additional worker turns. Retain the current advisor safeguards until the proposed replacement demonstrates acceptable coverage.

Acceptance:

- No implied command execution from copied reviewer prose.
- No recursive review of its own advice.
- Late advice respects cancellation and existing session behavior.
- Findings identify concrete evidence and relevant scope.
- The advisor does not become the best-of-N discriminator or acceptance authority.

A discriminator may use the same underlying model, but it receives frozen candidates and shared criteria in its own bounded selection task.

### WP7 — Adapt learning and lifecycle behavior into existing OMP paths

Implement as two separately qualified changes.

**WP7A: Learning records and retrieval**

Reuse ECC's scoped instinct structure and the vault's inspectable, unreviewed-record approach in the existing observation/learn path.

Records should include trigger, proposed action, scope, source task, evidence, supporting and contradicting outcomes, review state, and provenance. Keep revisions or append-only evidence so later corrections remain traceable.

Use both a record-count cap and a token budget for optional retrieved lessons. Six records is a reasonable starting parameter borrowed from ECC, not a proven optimum. Never count mandatory task criteria, authority state, or approved standing instructions against an optional-lessons cap.

Keep owner-approved promotion and the current staging process. Scheduled review, where already authorized, may stage qualifying changes under its existing policy. It does not acquire new authority to install standing rules.

Prefer a code or tool correction when a repeated failure has a deterministic cause. Retain scoped guidance for genuine judgment and reusable technique.

**WP7B: Lifecycle helpers and context recovery**

Inventory native event contracts before wiring any ECC helper.

| Event | Useful ECC behavior | OMP boundary |
| --- | --- | --- |
| Session start | Bounded relevant context | Mandatory Observer digest and canonical instructions remain intact |
| Tool completion | Optional formatting/checking support | Avoid duplicate full checks; respect task scope and cancellation |
| Compaction/resume | Recover useful working context | Reload authority and operation state from native sources |
| Session end | Usage summary and candidate lessons | No automatic grant, promotion, or closeout |
| Skill use | Record invocation context | Success is linked later to task outcome, not merely a non-error tool return |

Reuse appropriate upstream helpers directly when their dependencies and contracts fit. OMP's adapter can invoke a qualified Node helper without importing a second general runtime. Make runtime availability explicit; compiled OMP must never recursively launch itself as a Node hook runner.

Optional helper failures produce a diagnosis and preserve native operation. Required authorization and evidence gates retain their own failure behavior.

Acceptance:

- No duplicate memory authority or background observer daemon is added in this phase.
- A rejected or contradicted lesson is not silently promoted.
- Recovery after compaction/restart preserves current scope and cancellation.
- Narrative memory cannot authorize an operation.
- Lifecycle helpers remain bounded and do not start unrequested model loops.

### WP8 — Qualify security coverage, updates, and production promotion

Deliver:

1. A pinned AgentShield evaluation against actual OMP configuration fixtures.
2. A maintained upstream update procedure.
3. Release evidence integrated with the existing stabilization process.

AgentShield is a separate dependency. Begin with a read-only diagnostic. Demonstrate recognition of the relevant files and detection of seeded defects before adding a gate. Record blind spots and false positives. Do not substitute its score for an audit of native grants or WorkService behavior.

For updates:

1. Fetch a candidate upstream revision into the mirror.
2. Compare the upstream modules/assets actually consumed.
3. Reapply deterministic transforms and reviewed overlays.
4. Report new dependencies, changed activation, changed commands, and conflicts.
5. Run the affected mechanical tests.
6. Trial behavioral changes at the appropriate scope.
7. Build and promote a qualified release; retain the previous compatible release.

Start with an explicit maintenance operation. Add scheduling only through the user's existing automation choices. New upstream files are classified before activation.

For production, follow the existing stabilization plan's installed-version checks, recovery cases, evidence checks, and consecutive-trial requirement. The plan specifies five known-solvable task types and 20 consecutive accepted trials without unplanned workflow repair. Do not mix role/routing changes into a running qualification streak.

Rollback must consider service schemas and persisted state compatibility. Repointing a directory alone is not sufficient for every runtime change.

Acceptance:

- Update review shows exact asset changes and overlay conflicts.
- A content update cannot silently install hooks, agents, or new authority.
- Live identity matches the qualified artifact.
- Existing recovery/cancellation and acceptance-negative cases still pass.
- The actual Work Ledger acceptance process determines completion.

## 7. Qualification strategy

Use the cheapest evidence that answers the actual risk.

### Mechanical and deterministic tests

Test real boundaries, including:

- Metadata translation and rule bucketing.
- Skill reference/helper resolution.
- Project scope and duplicate naming.
- Native command ownership.
- Owned-file preservation, installation interruption, and removal.
- Typed audit schema/renderer behavior.
- Candidate identity and acceptance-evidence binding.
- Lost-response recovery and original-operation reconciliation.
- Cancellation followed by stale queued work.
- Missing auditor or unavailable provider transport.
- Preflight usage attribution and aggregation deduplication.
- Compatibility refusal across contract/version changes.

Reuse existing fixtures and suites. Do not create a new testing framework for ECC adaptation.

### Behavioral content trial

Use representative coding tasks with identical model/effort settings and independent acceptance criteria. Record relevant retrieval, useful implementation behavior, unnecessary process expansion, review findings, and intervention.

Known bad candidates test whether acceptance remains discriminating. Successful content retrieval alone is not task success.

The initial 5–10 task pilot is a screening exercise. It is not a statistical claim of a universal speedup. Expand only to resolve a concrete remaining question.

### Main outcome measures

- Accepted work completed without unplanned workflow repair.
- Human interventions and intervention time.
- Total model usage, including all supporting activity.
- Premium-request consumption and unpriced usage.
- Elapsed time, stalls, and recovery.
- False acceptance and false blocking, reported separately.
- Maintenance effort required to keep upstream adaptations current.

Do not use skill count, a generic harness score, or the number of successful tool invocations as the productivity objective.

An external host plus ECC comparison can be useful for a later architecture decision. It is not a prerequisite for importing compatible content. If used, account for equivalent required governance and acceptance obligations.

## 8. Rollout order and stop conditions

### First usable milestone

WP1 → WP2 → WP3.

The first deliverable is a reproducible ECC source mirror, an OMP adapter, matching project packs, and a small real-task trial. It should provide useful ECC behavior before the larger runtime improvements are finished.

### Reliability and economics milestone

WP4 and WP5 as distinct qualified changes, followed by WP6.

Complete task accounting should be available before drawing conclusions about advisor economics. Typed output qualification does not automatically change the audit model.

### Learning and continuity milestone

WP7A and WP7B separately, with the update and release discipline from WP8 operating throughout.

The broad catalog remains available for additional project packs without reopening the basic architecture.

### Later review interface

After stabilization, qualify Plan Canvas as a review and annotation surface. Bind feedback to the candidate/plan version and authenticated actor. Native workflow actions determine approval and scope changes; a UI button alone does not create authority.

ECC2, alternative orchestration runtimes, full plugin command installation, and additional background learning processes remain excluded from this phase. Revisit an exclusion when a specific compatible capability would simplify the system.

### Stop or narrow a rollout when

- A required policy or authority boundary changes unintentionally.
- A required dependency cannot run in the qualified installation.
- A transform loses meaning, references, or activation behavior.
- A change increases false acceptance or breaks recovery/cancellation.
- Supporting model activity grows without useful task outcomes.
- Update conflicts require a substantial independent fork of a component that was supposed to be reused.

For an isolated optional asset failure, disable that asset and keep the qualified native workflow usable. Do not stop unrelated accepted work simply because a diagnostic pack needs repair.

## 9. First instructions for the implementation agent

1. Read the repository's current implementation instructions and resolve the actual installed/development boundary.
2. Reconcile these work packages with existing Work Ledger entries.
3. Implement WP1 and WP2 without enabling new runtime hooks, changing mandatory Observer activation, or replacing native command owners.
4. Build the WP3 workflow/domain pack with recorded adaptations.
5. Run the relevant mechanical checks and the bounded content trial.
6. Return the exact source identities, adaptation diff, qualification evidence, measured overhead, and remaining compatibility work through the existing review route.
7. Advance WP4 onward as separate qualified items.

Routine path normalization, namespacing, and compatibility work within the approved package do not need repeated user decisions. Escalate a real change to owner policy, model obligations, authority, or deployment through the existing process.

## 10. Source references

File references below are pinned to the inspected revisions. Recheck the installed version and any newer approved source baseline when implementation begins.

### ECC

- [Repository and background](https://github.com/affaan-m/ECC)
- [Install profiles](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/manifests/install-profiles.json)
- [Install modules](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/manifests/install-modules.json)
- [Install components](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/manifests/install-components.json)
- [Manifest loading and planning](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/scripts/lib/install-manifests.js)
- [Install target registry](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/scripts/lib/install-targets/registry.js)
- [Link rewriting](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/scripts/lib/install/link-rewrite.js)
- [Ownership guard](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/scripts/lib/install/ownership-guard.js)
- [Install lifecycle](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/scripts/lib/install-lifecycle.js)
- [Pi adapter](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/.pi/README.md)
- [Architecture audit](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/skills/agent-architecture-audit/SKILL.md)
- [Dynamic workflow mode](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/skills/dynamic-workflow-mode/SKILL.md)
- [Structured review pilot](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/workflows/orch-review.workflow.js)
- [Workflow pilot status](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/workflows/README.md)
- [Learning](https://github.com/affaan-m/ECC/tree/8321021c54d670126ce3b2969d5deb880b4b0c2a/skills/continuous-learning-v2)
- [Memory-vault design](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/docs/design/ecc-memory-vault.md)
- [Security scan](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/skills/security-scan/SKILL.md)
- [Plan Canvas](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/docs/design/plan-canvas.md)
- [Roadmap](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/docs/ROADMAP.md)
- [ECC2 status](https://github.com/affaan-m/ECC/blob/8321021c54d670126ce3b2969d5deb880b4b0c2a/ecc2/README.md)

### OMP

- [Rule-matching pipeline](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/rulebook-matching-pipeline.md)
- [Rule parser](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/discovery/helpers.ts)
- [Rule bucketing](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/capability/rule-buckets.ts)
- [Bundled rules](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/discovery/builtin-rules/index.ts)
- [Installer](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/session-system/install.sh)
- [Advisor behavior](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/advisor-watchdog.md)
- [Advisor configuration](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/advisor/config.ts)
- [Auditor definition](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/session-system/agents/auditor.md)
- [Audit runner and preflight](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/session-system/extensions/workflow/auditor-runner.ts)
- [Task Observer](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/session-system/skills/task-observer/SKILL.md)
- [Observer review policy](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/session-system/skills/task-observer/references/weekly-review.md)
- [Model roles](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/config/model-roles.ts)
- [Memory backend types](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/memory-backend/types.ts)
- [Learning tool](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/tools/learn.ts)
- [Managed skills](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/tools/manage-skill.ts)
- [Security scanning](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/packages/coding-agent/src/tools/security-scan.ts)
- [Instruction scope](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/instruction-scope.md)
- [Stabilization plan](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/omp-stabilization-plan.md)
- [Installation isolation](https://github.com/theturtlecsz/oh-my-pi/blob/1b78b801edebc0ce8c4fbd90c8a192e6d5b6bec1/docs/installation-isolation.md)

