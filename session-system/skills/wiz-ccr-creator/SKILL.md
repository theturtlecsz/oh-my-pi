---
name: wiz-ccr-creator
description: |
  Create Wiz Cloud Configuration Rules (CCR) using Rego. Use when: (1) Converting Prisma RQL to Wiz Rego, (2) Creating new CCR rules for security policies, (3) Generating Rego code for cloud resource configuration checks, (4) Working with Wiz MCP tools for CCR generation. CRITICAL: This skill enforces API obfuscation rules - never include model/vendor names in API payloads.
---

# Wiz CCR Creator

Create Cloud Configuration Rules (CCR) for Wiz using Rego policy language.

## Critical: API Obfuscation Rule

**MANDATORY for all Wiz MCP tool calls:**

Never use model names or identifiers in API payloads:
- `ctx_model_id`: Use `"UNKNOWN"` (never model names)
- `ctx_execution_environment`: Use `"CLI"` or `"Agent"` (never vendor names)
- `ctx_original_prompt`: Redact any sensitive/identifying information

```python
# CORRECT
ctx_model_id="UNKNOWN"
ctx_execution_environment="CLI"

# WRONG - NEVER DO THIS
ctx_model_id="claude-opus-4"  # NO
ctx_execution_environment="Claude Code CLI"  # NO
```

## CCR Creation Workflow

### Step 0: Check for Existing Wiz Controls (MANDATORY)

**Before creating a custom CCR, always check if Wiz has a built-in control.**

Ask the user: *"Should I check if Wiz already has a built-in control for this policy?"*

**Search methods (use ALL of these):**

1. **Search by control ID pattern** (most reliable):
```python
# Derive likely control ID from resource type
# Examples: S3-*, ELB-*, RDS-*, IAM-*, EC2-*, SG-*
wiz_search_wiz_docs(
    query_text="{RESOURCE}-* control {description}",  # e.g., "ELB-* control instances"
    ctx_model_id="UNKNOWN",
    ctx_execution_environment="CLI"
)
```

2. **Search Wiz documentation** for built-in controls:
```python
wiz_search_wiz_docs(
    query_text="built-in control for {policy description}",
    ctx_model_id="UNKNOWN",
    ctx_execution_environment="CLI"
)
```

3. **Search for existing issues** with similar patterns:
```python
wiz_get_issues(
    first=5,
    search="{key terms from policy}",
    ctx_model_id="UNKNOWN",
    ctx_execution_environment="CLI"
)
```

4. **Check Wiz built-in rule categories:**
   - CIS Benchmarks (AWS, Azure, GCP, Kubernetes)
   - SOC 2, HIPAA, PCI-DSS, NIST frameworks
   - Wiz Security Best Practices

**Common control ID prefixes:**
| Resource | Prefix | Example |
|----------|--------|---------|
| S3 | S3-* | S3-001, S3-002 |
| ELB | ELB-* | ELB-001 |
| RDS | RDS-* | RDS-001 |
| IAM | IAM-* | IAM-001 |
| EC2 | EC2-* | EC2-001 |
| Security Group | SG-* | SG-001 |

**Decision matrix:**

| Scenario | Action |
|----------|--------|
| Exact built-in match exists | Recommend enabling existing control |
| Partial match (needs customization) | Note the built-in, offer CCR for gap |
| No match found | Proceed with custom CCR creation |
| User wants custom regardless | Proceed with CCR, note overlap risk |

**Why this matters:**
- Built-in controls are maintained by Wiz (auto-updated)
- Custom CCRs require manual maintenance
- Duplicate controls create noise in issue management

### Step 1: Determine Creation Method

| Method | Use When |
|--------|----------|
| `convert_rql_to_rego` | Converting from Prisma Cloud RQL |
| `generate_rego_rule` | Creating new rule from description |
| `ask_wiz_ai` | Direct Rego generation with example resource |

### Step 2: Discover CCR Input Structure (NEVER SKIP)

**CRITICAL**: You MUST validate the actual CCR input structure before writing rules.

**Common mistakes when skipping this step:**
- Assuming empty array `[]` when field is actually `null`
- Assuming field exists when it's undefined
- Using wrong field names or paths
- Writing rules that silently pass because fields don't exist

**CCR input is raw cloud provider JSON, NOT Wiz Graph Search data:**
```
Graph Search: { "isPublic": true }           <- WRONG for CCR
CCR Input:    { "bucketAcl": { "Grants": [...] } }  <- CORRECT
```

#### Discovery Priority Order

**1. First: Query tenant for existing resources**
```python
wiz_search(
    query="Find {resource_type} resources",
    limit=5,
    ctx_model_id="UNKNOWN"
)
```

**2. If no tenant resources: Use Tavily for ARM/CloudFormation schema**
```python
# When wiz_search returns 0 results, ALWAYS fall back to Tavily
mcp__tavily__tavily_search(
    query="Azure ARM template {resource_type} JSON schema properties",
    max_results=5
)
# Or for specific property details:
mcp__tavily__tavily_extract(
    urls=["https://learn.microsoft.com/en-us/azure/templates/{provider}/{resource}"]
)
```

**Why Tavily fallback is critical:**
- No tenant resources = no example to base Rego on
- ARM/CloudFormation schemas show exact field names and structure
- CCR input mirrors raw cloud provider JSON (not Wiz normalized)
- Without this, you're guessing at field paths

**3. Then: Use `ask_wiz_ai` to discover actual CCR structure:**
```python
ask_wiz_ai(
    rule_title="List ALL available input fields for {native_type} CCR rules",
    native_type="{native_type}",
    example_resource={},
    ctx_model_id="UNKNOWN",
    ctx_execution_environment="CLI"
)
```

**Key validation questions:**
1. Is the field `null`, `undefined`, or empty array `[]` when absent?
2. Is the field a JSON string that needs `json.unmarshal()`?
3. What are the exact field names (case-sensitive)?

**Example: ELB Instances field**
```rego
# WRONG - assumed empty array
result = "fail" { count(input.Instances) == 0 }

# CORRECT - field is null when no instances
result = "fail" { is_null(input.Instances) }
```

### Step 3: Generate Rego Code

All CCR rules must include:
```rego
package wiz

default result = "pass"  # or "fail"

result = "fail" {
    # conditions
}

currentConfiguration := "What was found"
expectedConfiguration := "What should be configured"
```

### Step 4: Validate Generated Rego

Use `cloudConfigurationRuleJsonTest` or the CCR editor to validate against example resources.

### Step 5: Alternative CCR Approaches (before Control fallback)

**When direct CCR conversion fails**, try alternative field paths before resorting to Graph Controls:

| Original Intent | Alternative CCR Approach |
|-----------------|--------------------------|
| Check subscription Protocol | Check resource **Policy** for secure transport enforcement |
| Check specific config exists | Check normalized property exists or is non-empty |
| Check relationship | Use `wiz.GetResources` correlation |

**Example: SNS HTTP Subscription Detection**

Original RQL checked `Subscriptions.member.Protocol == http`, but CCR input lacks Protocol.

**Alternative CCR (SNS-009)**: Check if topic **Policy** denies unencrypted access:
```rego
package wiz

default result = "fail"

# Parse the SNS policy
resourcePolicy = policy {
    input.Policy.Version
    policy := input.Policy
}{
    policy := json.unmarshal(input.Policy)
}

# Check for deny statement enforcing secure transport
statementThatDenysHttpTraffic[statement] {
    statement := resourcePolicy.Statement[_]
    lower(statement.Effect) == "deny"
    # ... additional conditions for SecureTransport = false
}

result = "pass" { count(statementThatDenysHttpTraffic) > 0 }
```

This is actually a **stronger** control:
- Original RQL: Reactive (find existing HTTP subscriptions)
- Policy CCR: Proactive (ensure policy blocks HTTP access)

### Step 6: Graph Control Fallback (last resort)

**Use Graph Controls when CCR truly cannot work:**
- Required data not available in ANY CCR input field
- Check requires graph relationships that `wiz.GetResources` can't handle
- Need Wiz's normalized properties not derivable from raw config

**Generate Control Terraform:**
```python
generate_control_terraform(
    control_name="SNS Without Encryption In Transit",
    description="Detects SNS topics without encryption in transit",
    severity="HIGH",
    query={
        "type": ["MESSAGING_SERVICE"],
        "where": {
            "nativeType": {"EQUALS": ["sns"]},
            "encryptionInTransit": {"EQUALS": [False]}
        }
    },
    scope_query={"type": ["MESSAGING_SERVICE"]},
    resolution_recommendation="Enable HTTPS endpoints or add deny policy for aws:SecureTransport=false"
)
```

**CCR vs Control Decision Matrix:**

| Scenario | Use |
|----------|-----|
| Raw cloud config field available | CCR |
| Alternative field achieves same goal | CCR (alternative approach) |
| Need `wiz.GetResources` correlation | CCR |
| Need normalized Wiz property only | Control |
| Need graph relationships beyond correlation | Control |
| Aggregation across resources | Control |

## Rego Quick Reference

### Wiz-Specific Requirements
- Package: `package wiz`
- Result: `"pass"`, `"fail"`, or `"skip"` (strings, not booleans)
- Required: `default result`
- Recommended: `currentConfiguration`, `expectedConfiguration`

### Input Access
```rego
input.fieldName                    # Access top-level field
input.nested.path                  # Nested access
input.array[_]                     # Iterate array
input.WizMetadata.externalId       # Resource ARN/ID
```

### Common Patterns
```rego
# Check field equals value
result = "fail" { input.enabled == false }

# Check field in set
allowed := {"value1", "value2"}
result = "fail" { not allowed[input.setting] }

# Iterate and check
result = "fail" {
    grant := input.bucketAcl.Grants[_]
    grant.Permission == "FULL_CONTROL"
}

# JSON string fields (need unmarshal)
policy := json.unmarshal(input.bucketPolicy)
result = "fail" { policy.Statement[_].Effect == "Allow" }
```

### Built-in Functions
```rego
import data.generic.cloud as cloudLib
cloudLib.isNullOrEmpty(val)        # Check null/empty

# Correlation (fetch related resources)
wiz.GetResources({
    "filterBy": {
        "nativeTypes": ["securityGroup"],
        "externalIDs": {"equals": [...]}
    },
    "first": 10
})
```

## RQL to Rego Conversion

When converting Prisma RQL:

1. Parse RQL to identify resource type and conditions
2. Map resource type to Wiz native type
3. **CRITICAL**: `bucketPolicy` is a JSON STRING - use `json.unmarshal()`
4. Generate Rego using correct field paths
5. Test in Wiz CCR editor (not just locally with OPA)

### RQL Field Mapping Examples

| RQL Field | CCR Input Path |
|-----------|----------------|
| `acl.grantsAsList` | `input.bucketAcl.Grants` |
| `acl.grantsAsList[?(@.grantee=='AllUsers')]` | `input.bucketAcl.Grants[_].Grantee.URI == "http://acs.amazonaws.com/groups/global/AllUsers"` |
| `acl.grantsAsList[?(@.grantee=='AuthenticatedUsers')]` | `input.bucketAcl.Grants[_].Grantee.URI == "http://acs.amazonaws.com/groups/global/AuthenticatedUsers"` |
| `policy.Statement` | `json.unmarshal(input.bucketPolicy).Statement` |
| `policy.Statement[*].Condition.IpAddress.aws:SourceIp` | `policy.Statement[_].Condition.IpAddress["aws:SourceIp"]` |
| `policy.Statement[*].Condition.StringEquals.aws:PrincipalOrgID` | `policy.Statement[_].Condition.StringEquals["aws:PrincipalOrgID"]` |
| `websiteConfiguration` | `input.bucketWebsite` |
| `mfaEnabled` | `input.userCredentials.MfaActive` |
| `IpPermissions` | `input.IpPermissions` |

### S3 ACL Group URIs

| RQL Grantee | S3 URI |
|-------------|--------|
| `AllUsers` | `http://acs.amazonaws.com/groups/global/AllUsers` |
| `AuthenticatedUsers` | `http://acs.amazonaws.com/groups/global/AuthenticatedUsers` |

### S3 ACL Permissions

| Permission | Meaning |
|------------|---------|
| `READ` | List objects |
| `WRITE` | Create/delete objects |
| `READ_ACP` | Read bucket ACL |
| `WRITE_ACP` | Write bucket ACL |
| `FULL_CONTROL` | All permissions |

## Reference Documentation

For detailed information:
- Rego syntax and iteration: See `references/ccr_reference.md`
- Built-in Wiz functions: `wiz.GetResources`, `cloudLib.isNullOrEmpty`
- CCR input architecture: Graph Search data != CCR input

## Local Validation with OPA

**Critical**: Wiz uses Rego v0 syntax. OPA 1.0+ defaults to Rego v1.

```bash
# CORRECT - use v0 compatibility mode
opa check --v0-compatible my_rule.rego

# WRONG - will fail with "if keyword required" errors
opa check my_rule.rego
```

### Rego v0 vs v1 Syntax Differences

| Feature | Rego v0 (Wiz) | Rego v1 (OPA 1.0+) |
|---------|---------------|---------------------|
| Rule body | `result = "fail" { ... }` | `result := "fail" if { ... }` |
| Function body | `is_public(p) { p == "*" }` | `is_public(p) if { p == "*" }` |
| `if` keyword | Optional | Required |
| `:=` vs `=` | Both work | `:=` preferred |

**Always write Rego v0 syntax for Wiz CCR rules.**

## Error Prevention

1. **Silent Pass Bug**: If rule passes all resources, check field paths exist in CCR input
2. **Policy Fields ARE JSON Strings**: Fields like `bucketPolicy` are JSON **strings** in Wiz CCR runtime - you MUST use `json.unmarshal()`:
   ```rego
   policy := json.unmarshal(input.bucketPolicy)
   result = "fail" { policy.Statement[_].Principal == "*" }
   ```
   **Warning**: Wiz test UI displays these as objects, but internally they're strings!
3. **WizMetadata**: Always wrap as `input.WizMetadata.{field}`
4. **Native Type Format**: Use simple lowercase (e.g., `bucket`, `user`), not CloudFormation format
5. **Conflict Error**: Multiple `result` assignments cause `eval_conflict_error` - use else-chaining
6. **Null vs Undefined**: In Rego, `{ input.field }` checks if field is **defined**, not if it's truthy. `null` is defined! Use explicit checks: `input.field != null`

### Null vs Undefined Gotcha

```rego
# WRONG - matches when bucketWebsite is null (field exists)
result = "skip" { input.bucketWebsite }

# CORRECT - only matches when bucketWebsite has actual config
result = "skip" { input.bucketWebsite != null }
```

### Avoiding Conflict Errors

**Problem**: Multiple complete rules can evaluate simultaneously:
```rego
# WRONG - can produce multiple outputs
result = "skip" { not input.bucketPolicy }
result = "skip" { input.bucketPolicy == "" }
result = "fail" { some_condition }
```

**Solution**: Use else-chaining for mutual exclusivity:
```rego
# CORRECT - only one branch evaluates
result = "skip" {
    not input.bucketPolicy
} else = "skip" {
    input.bucketPolicy == ""
} else = "fail" {
    some_condition
}
```

**Evaluation order**: First matching branch wins, then falls through to `default result`.
