# Instruction scope and optional output style

OMP's default, project, subagent, eager-task, orchestration, and plan-mode
templates share one completion rule: finish the currently authorized assignment.
A worker returns its slice; an auditor returns evidence and a verdict. Neither
must close the parent issue to finish its assigned role. Ordinary internal
steps continue while authority remains valid. Actual budget, cancellation,
grant, and gate boundaries stop work through the existing role/result interface,
with available state and the next legal action reported.

These are instruction changes, not new authority or provenance enforcement.
Tag syntax in file content, retrieved text, or tool output does not grant that
content authority. A successful edit proves that an edit ran; behavioral claims
still require suitable checks. Installed user/project doctrine controls when
to delegate; decomposition, dependency, concurrency, and context safeguards
apply whenever delegation is authorized.

## Intake and independent audit

Intake resolves facts from available evidence and records routine reversible
decisions as assumptions. Consequential scope, authorization, or product
behavior choices require owner input. The question ceiling is not a target:
ending an interview leaves unresolved questions visible and dependent readiness
blocked. A skipped question alone does not settle the requirement. An approved
deferral must bound the published scope and retain the unresolved dependency.

Even a zero-question interview retains lint, the exact publication preview,
and owner confirmation. Post-merge installation criteria stay with a blocked
activation child; candidate-worktree tests remain delivery evidence.

Manual `/summary` and `/done` retain their command gates. A valid `/execute`
grant carries its existing bounded native independent-audit and close route.
An auditor finishes by returning its report, including `BLOCKED` when required
evidence remains unavailable. Observation status never changes standing policy.
Model roles resolve from runtime configuration; this change assigns no models
and does not expand the `@deep` execution boundary.

## Ponytail: optional for one OMP invocation

When Ponytail is installed, start a fresh OMP session with:

```sh
PONYTAIL_DEFAULT_MODE=off omp
```

This environment assignment applies to that process and its children. It does
not change shared Ponytail defaults or another harness. The plugin's commands
remain available:

```text
/ponytail full
/ponytail off
```

The first enables Ponytail for the session; the second disables its system
prompt addition. A resumed session restores its saved `ponytail-mode`, which
overrides the environment default. Use `/ponytail off` in that resumed session
when needed. Changing mode affects subsequent requests, not a request already
sent to a provider.

Do not use `/ponytail default off` for an OMP-only change: that command writes
the shared Ponytail configuration. Do not patch the installed plugin cache.

## Disable the whole plugin for an OMP project

For durable, exact-package exclusion, merge this entry into the existing
`.omp/plugin-overrides.json` at OMP's effective project configuration root:

```json
{
  "disabled": ["@dietrichgebert/ponytail"]
}
```

Preserve unrelated keys and disabled entries. This disables the whole plugin,
including its commands and skills, on the next discovery/startup. Remove only
that package entry to make it discoverable again. Managed installations may
use a separate configuration root; editing a candidate worktree's settings
does not configure its supervising installation. See
[installation isolation](installation-isolation.md).

The generic `disabledExtensions` setting is less precise for this plugin:
its installed `pi-extension/index.js` entry currently receives the identifier
`extension-module:pi-extension`. Another plugin with the same entry-directory
name can share that identifier. Prefer exact-package exclusion or the
per-invocation environment control above.

## Verification boundaries

Qualification uses real plugin discovery, extension loading, `session_start`,
and `before_agent_start` in disposable configuration roots. It checks fresh
off, command-enabled full, command-disabled off, saved-mode precedence, and
exact-package exclusion. With off selected, the existing base prompt remains
byte-for-byte unchanged. No model request is needed for these checks.

Prompt assembly and scripted executor tests establish loading, schema,
tool-set, and termination behavior. Independent instruction scenarios review
the intended decisions. Neither establishes universal model compliance,
security enforcement from wording alone, or activation in a running session.
Repository delivery and OMP-249 live promotion remain separate.
