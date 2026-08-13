---
name: wiz-mcp
description: Enforce correct usage of wiz-mcp tools per project rules and MCP.md guidelines, including API obfuscation of model identity in all ctx_* fields.
---

# Wiz MCP Access Skill

## Purpose

Enforce correct usage of wiz-mcp tools per project rules and MCP.md guidelines.

## Critical Rules (from /home/thetu/wiz-mcp/CLAUDE.md)

**NEVER use identifying names in context fields:**

* `ctx_model_id` → always `"UNKNOWN"`
* `ctx_execution_environment` → use standard browser ID: `"Chrome/131.0 Linux x86_64"`
* `ctx_original_prompt` → sanitize: remove model names, use generic description

**Prohibited strings in any ctx\_* field:*\*

* claude, opus, anthropic, gpt, openai, gemini, sonnet, haiku
* Any model version numbers or identifiers

## Correct Usage Pattern

```yaml
# CORRECT
ctx_model_id: "UNKNOWN"
ctx_execution_environment: "Chrome/131.0 Linux x86_64"
ctx_original_prompt: "Search for CLI scanning documentation"

# WRONG - violates rules
ctx_model_id: "claude-opus-4"
ctx_execution_environment: "Claude Code CLI with wiz-mcp tools"
ctx_original_prompt: "User asked Claude to search..."
```

## Query Classification (from MCP.md)

**Simple lookup**: Use direct tool, stop after definitive answer
**Complex analysis**: Multi-tool coordination permitted when needed

## Stop Conditions

Stop searching when:

* Got a definitive answer to the question
* Task objectives fully satisfied

## Tools Covered

* `mcp__wiz-mcp__wiz_search_wiz_docs`
* `mcp__wiz-mcp__wiz_search`
* `mcp__wiz-mcp__wiz_get_issues`
* `mcp__wiz-mcp__wiz_get_threats`
* `mcp__wiz-mcp__wiz_get_cloud_events`
* All other `mcp__wiz-mcp__*` tools

## Example Invocation

```
mcp__wiz-mcp__wiz_search_wiz_docs(
  query_text="partial coverage services meaning",
  ctx_original_prompt="Search for partial coverage definition",
  ctx_model_id="UNKNOWN",
  ctx_execution_environment="Chrome/131.0 Linux x86_64"
)
```
