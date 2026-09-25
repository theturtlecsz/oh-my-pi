# CPK-0 Surface Inventory

This directory contains the machine-readable inventory of tools, extension/hook interfaces, and authority crossings required for the CPK-0 kernel/plugin boundary architecture.

## Overview

The inventory is generated from the source code by `scripts/cpk0-inventory.ts` and committed at [`surface-inventory.json`](./surface-inventory.json). A test in the test suite verifies that the committed JSON matches the generator output exactly, preventing silent drift.

To regenerate or verify the inventory:

```bash
# Regenerate surface-inventory.json from source
bun scripts/cpk0-inventory.ts

# Verify committed inventory is up to date (fails with exit code 1 if stale)
bun scripts/cpk0-inventory.ts --check
```

## Inventory Structure and Field Reference

The inventory contains three primary sections: `tools`, `extension_surfaces`, and `authority_crossings`.

### 1. Top-Level Fields

- `schema_version` (`string`): Schema identifier (`"cpk0/v1"`).
- `task` (`string`): Task identifier (`"OMP-287"`).
- `description` (`string`): High-level description of the inventory artifact.
- `tools` (`ToolInventoryEntry[]`): Complete list of first-party tools the coding agent can register.
- `extension_surfaces` (`object`): Exported interfaces and types for extensions and hooks.
- `authority_crossings` (`AuthorityCrossingEntry[]`): Capability classifications detailing authority and gating mechanisms.

---

### 2. Tools (`tools`)

Every first-party tool that the coding agent can register in any session mode is enumerated from source. This includes standard builtins (`BUILTIN_TOOL_NAMES`), hidden/system tools (`HIDDEN_TOOL_NAMES`), custom injected tools (`generate_image`, `tts`), ephemeral mode tools (`vibe_*`), advisor tools (`advise`), and session-system workflow tools (`work`).

| Field | Type | Description |
|---|---|---|
| `name` | `string` | Canonical name of the tool as exposed to the model or registry (e.g. `"read"`, `"bash"`, `"generate_image"`). |
| `source_file` | `string` | Repository-relative path to the primary declaration file (e.g. `"packages/coding-agent/src/tools/read.ts"`). |
| `approval_tier` | `string` | Declared approval tier: `"read"`, `"write"`, `"exec"`, or `"dynamic"` if computed by a function at runtime. |
| `dynamic` | `boolean` | `true` if the tool resolves its approval tier dynamically based on invocation arguments; `false` if statically declared. |
| `tiers` | `string[]` | Complete array of possible resolved tiers (`"read"`, `"write"`, `"exec"`). |
| `kind` | `string` | Origin classification: `"builtin"`, `"hidden"`, `"custom"`, `"vibe"`, `"advisor"`, or `"session_system"`. |
| `description` | `string` (optional) | Summary description extracted from the tool's source declaration. |

---

### 3. Extension & Hook Surfaces (`extension_surfaces`)

Contains full type signatures for both `extensions` (`packages/coding-agent/src/extensibility/extensions`) and `hooks` (`packages/coding-agent/src/extensibility/hooks`).

Each subsystem (`extensions` and `hooks`) defines:
- `interface_count` (`number`): Count of unique exported interfaces.
- `type_count` (`number`): Count of unique exported type aliases.
- `interfaces` (`TypeSurfaceEntry[]`): Exported interface declarations with all members.
- `types` (`TypeSurfaceEntry[]`): Exported type alias declarations with definitions and members (for type literals).

#### `TypeSurfaceEntry` Fields

| Field | Type | Description |
|---|---|---|
| `name` | `string` | Name of the exported interface or type (e.g. `"ExtensionUIContext"`, `"HookUIContext"`). |
| `kind` | `"interface" \| "type"` | Declaration kind. |
| `source_file` | `string` | Repository-relative path to the file where the interface or type is declared. |
| `extends` | `string[]` (optional) | Names of extended interfaces (interfaces only). |
| `definition` | `string` (optional) | Raw type definition text (for type aliases without object members). |
| `members` | `MemberInventoryEntry[]` | Member declarations (methods, properties, index signatures, call signatures). |

#### `MemberInventoryEntry` Fields

| Field | Type | Description |
|---|---|---|
| `name` | `string` | Member identifier (or `"<call>"`, `"<index>"`). |
| `kind` | `string` | `"method"`, `"property"`, `"call_signature"`, or `"index_signature"`. |
| `optional` | `boolean` | `true` if marked optional (`?`); `false` otherwise. |
| `parameters` | `string` (optional) | Parameter list string for methods and call signatures. |
| `return_type` | `string` (optional) | Return type annotation string for methods and call signatures. |
| `type` | `string` (optional) | Type annotation for properties and index signatures. |

Key interfaces documented:
- `ExtensionUIContext`: The full TUI/interactive UI capability surface available to extensions, including modals, notifications, overlays, editors, widgets, and themes.
- `HookUIContext`: The restricted UI capability surface available to hooks executed within the agent turn loop.
- `ExtensionAPI` / `HookAPI`: Registration APIs and session action facades.
- `ExtensionContext` / `HookContext`: Runtime execution contexts passed to event handlers and slash commands.

---

### 4. Authority Crossings (`authority_crossings`)

Classifies every major capability granted to extensions and hooks across 7 authority crossing dimensions.

| Field | Type | Description |
|---|---|---|
| `capability` | `string` | Identifier for the capability (e.g. `"command_registration"`, `"execution"`, `"tool_invocation"`). |
| `label` | `string` | Human-readable title of the authority crossing. |
| `description` | `string` | Summary of what the capability provides. |
| `methods` | `string[]` | AST-verified method/property references implementing the capability across `ExtensionAPI`, `HookAPI`, etc. |
| `authority_granted` | `string` | Detailed analysis of what authority the capability confers upon extension code. |
| `gate` | `string` | Description of the security, validation, or lifecycle gate that guards the crossing (or `"None (ungated)"`). |
| `gate_type` | `string` | Gate category: `"user_invocation"`, `"approval_policy"`, `"none"`, `"session_lifecycle"`, `"lifecycle_tracking"`, `"os_permissions"`, `"pipeline_gate"`. |

#### Summary of Classified Capabilities

1. **`command_registration`**: Registers custom slash commands. Gated by explicit user invocation.
2. **`tool_registration`**: Registers LLM-callable tools. Gated by tool approval policies (`tools.approvalMode`), interactive human confirmation prompts, and parameter schema validation.
3. **`execution`**: Direct child process and shell execution (`api.exec`). **Ungated**: runs with the host user's full OS privileges without prompts.
4. **`messaging`**: Synthetic message injection and session journal persistence. Gated by session lifecycle state (runtime initialization check) and schema validation.
5. **`timers`**: Background timer scheduling. Gated by `ManagedTimerTracker` lifecycle tracking (automatic cancellation and unreferencing on session disposal).
6. **`filesystem_facing_apis`**: Working directory and session file path access. Gated by host OS user/group file permissions; no in-process filesystem sandbox.
7. **`tool_invocation`**: Tool call/result interception and same-name delegation (`invokeTool`). Gated by sequential pipeline handler execution, blocking short-circuiting, same-tool delegation bounds, and recursion depth limits.
