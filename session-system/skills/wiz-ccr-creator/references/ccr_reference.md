# CCR Reference Documentation

## Table of Contents
- [CCR Input Architecture](#ccr-input-architecture)
- [Rego Language Basics](#rego-language-basics)
- [Variables and Documents](#variables-and-documents)
- [Iteration and Looping](#iteration-and-looping)
- [Built-in Functions](#built-in-functions)
- [Common CCR Patterns](#common-ccr-patterns)

---

## CCR Input Architecture

### Critical Insight

**Graph Search properties ≠ CCR input structures**

| Source | Data Type | Example Fields |
|--------|-----------|----------------|
| Graph Search | Wiz-normalized properties | `name`, `region`, `isPublic` |
| CCR Input | Raw cloud provider JSON | `bucketAcl.Grants`, `bucketPolicy`, `NetworkInterfaces` |

### Example: S3 Bucket

**Graph Search returns (WRONG for CCR):**
```json
{
  "name": "my-bucket",
  "region": "us-east-1",
  "isPublic": true
}
```

**CCR input actually receives (CORRECT):**
```json
{
  "bucketAcl": {
    "Owner": {"ID": "...", "DisplayName": "..."},
    "Grants": [
      {
        "Grantee": {
          "Type": "Group",
          "URI": "http://acs.amazonaws.com/groups/global/AllUsers"
        },
        "Permission": "READ"
      }
    ]
  },
  "bucketPolicy": "{\"Version\":\"2012-10-17\",\"Statement\":[...]}",
  "WizMetadata": {
    "externalId": "arn:aws:s3:::my-bucket",
    "nativeType": "bucket"
  }
}
```

### Discovering CCR Input Structure

Use Wiz AI to discover actual fields:
```
rule_title: "List ALL available input fields for {native_type} CCR rules"
native_type: "{native_type}"
example_resource: {}
```

---

## Rego Language Basics

### Package Declaration
Every CCR must start with:
```rego
package wiz
```

### Result Values
Wiz CCR results must be strings (not booleans):
- `"pass"` - Resource meets the security requirement
- `"fail"` - Resource violates the security requirement
- `"skip"` - Rule doesn't apply to this resource

### Rule Structure
```rego
assignment {
   conditions
}
```

The assignment occurs only if ALL conditions are true.

### Default Values
```rego
default result = "pass"  # Use when checking for violations

default result = "fail"  # Use when checking for safe configurations
```

---

## Variables and Documents

### Constant Definition (`:=`)
```rego
a := 1
b := "Rego"
c := {"company":"Wiz", "office":["tlv","nyc"]}
```

### Default Definition
```rego
default allow = false
default result = "pass"
```

### Data Types

**Scalars:**
```rego
s := "string value"
n := 3.14
b := true
null_val := null
```

**Sets (unordered, unique):**
```rego
x := {1, 2, 3}
x[3]      # true - 3 is in set
```

**Arrays (ordered, indexed):**
```rego
y := [1, 2, 3]
y[0]      # returns 1
```

**Objects/Dictionaries:**
```rego
user := {"name": "john", "role": "admin"}
user["name"]  # returns "john"
user.name     # same as above
```

---

## Iteration and Looping

### Basic Iteration
```rego
employee := ["foo", "bar", "john"]

# Using index variable
allow { employee[i] == "foo" }

# Using underscore (don't need index)
allow { employee[_] == "foo" }
```

### Iterating Objects
```rego
# Input: {"dogArray": [{"firstName": "Timmy", "lastName": "Mymon"}, ...]}

# Same index across conditions (AND)
allowSameSymbol {
  input.dogArray[j].firstName == "Timmy"
  input.dogArray[j].lastName == "Reznik"  # Same j
}

# Different indices (OR across combinations)
allowDifferent {
  input.dogArray[j].firstName == "Timmy"
  input.dogArray[k].lastName == "Reznik"  # Different k
}
```

### Cross-Array Comparison
```rego
arr1 := [1, 2, 3]
arr2 := [4, 5, 6]

# Compare all combinations
allow { arr1[i] == arr2[j] }

# Compare same index
allow { arr1[i] == arr2[i] }
```

---

## Built-in Functions

### Wiz Cloud Functions

**isNullOrEmpty:**
```rego
import data.generic.cloud as cloudLib
cloudLib.isNullOrEmpty(input.some_attribute)
```

**wiz.GetResources (Correlation):**
```rego
securityGroups := wiz.GetResources({
    "filterBy": {
        "nativeTypes": ["securityGroup"],
        "externalIDs": {"equals": ["sg-12345"]}
    },
    "first": 10
})
```

### Standard Rego Functions

**Time:**
```rego
time.now_ns()                           # Current time in nanoseconds
time.parse_duration_ns("2160h")         # 90 days in nanoseconds
time.parse_rfc3339_ns(value)            # Parse RFC3339 timestamp
```

**Network:**
```rego
net.cidr_contains("10.0.0.0/8", "10.1.2.3")  # IP in CIDR
net.cidr_intersects(cidr1, cidr2)            # CIDRs overlap
```

**JSON:**
```rego
json.unmarshal(json_string)             # Parse JSON string to object
```

---

## Common CCR Patterns

### Check Field Value
```rego
package wiz

default result = "pass"

result = "fail" {
    input.encryptionEnabled == false
}

currentConfiguration := sprintf("Encryption: %v", [input.encryptionEnabled])
expectedConfiguration := "Encryption should be enabled"
```

### Check Field in Allowed Set
```rego
package wiz

default result = "fail"

allowed_regions := {"us-east-1", "us-west-2", "eu-west-1"}

result = "pass" {
    allowed_regions[input.WizMetadata.region]
}
```

### Check Array for Violations
```rego
package wiz

default result = "pass"

allIps := {"0.0.0.0/0", "::/0"}

result = "fail" {
    rule := input.IpPermissions[_]
    allIps[rule.IpRanges[_].CidrIp]
}

currentConfiguration := "Security group allows unrestricted access"
expectedConfiguration := "Security group should restrict source IPs"
```

### Parse JSON String Field
```rego
package wiz

default result = "pass"

policy := json.unmarshal(input.bucketPolicy)

result = "fail" {
    statement := policy.Statement[_]
    statement.Effect == "Allow"
    statement.Principal == "*"
}
```

### Check for Missing/Null Fields
```rego
package wiz
import data.generic.cloud as cloudLib

default result = "fail"

result = "pass" {
    not cloudLib.isNullOrEmpty(input.loggingConfiguration)
    input.loggingConfiguration.enabled == true
}
```

### Time-Based Check
```rego
package wiz

default result = "pass"

now_ns := time.now_ns()
ninety_days_ns := time.parse_duration_ns("2160h")

result = "fail" {
    created_ns := time.parse_rfc3339_ns(input.CreateDate)
    (now_ns - created_ns) > ninety_days_ns
}

currentConfiguration := sprintf("Resource age exceeds 90 days")
expectedConfiguration := "Resource should be rotated within 90 days"
```

### Correlation Rule Example
```rego
package wiz

default result = "pass"

listOfSecurityGroups[sg] {
    sg := input.NetworkInterfaces[_].Groups[_].GroupId
}

securityGroups := wiz.GetResources({
    "filterBy": {
        "nativeTypes": ["securityGroup"],
        "externalIDs": {"equals": split(concat(",", listOfSecurityGroups), ",")}
    },
    "first": 8
})

allIps := {"0.0.0.0/0", "::/0"}

result = "fail" {
    rule := securityGroups[_].IpPermissions[_]
    allIps[rule.IpRanges[_].CidrIp]
}
```

---

## Native Type Examples

### AWS S3 Bucket Fields
```
input.Name
input.CreationDate
input.bucketAcl.Owner, input.bucketAcl.Grants
input.bucketEncryptionConfiguration
input.bucketLifecycleConfiguration
input.bucketLocation
input.bucketLogging
input.bucketPolicy (JSON string)
input.bucketPolicyStatus
input.bucketPublicAccessBlock
input.bucketTags
input.bucketVersioning
input.bucketWebsite
input.WizMetadata.region, input.WizMetadata.externalId
```

### AWS IAM User Fields
```
input.Arn
input.UserName
input.UserId
input.CreateDate
input.Path
input.AttachedManagedPolicies
input.GroupList
input.UserPolicyList
input.Tags
input.userCredentials.MfaActive ("true"/"false" strings)
input.userCredentials.AccessKey1Active
input.userCredentials.AccessKey1LastRotated
input.userCredentials.PasswordEnabled
input.WizMetadata.region
```

### AWS Security Group Fields
```
input.GroupId
input.GroupName
input.VpcId
input.IpPermissions[].IpProtocol
input.IpPermissions[].FromPort
input.IpPermissions[].ToPort
input.IpPermissions[].IpRanges[].CidrIp
input.IpPermissions[].Ipv6Ranges[].CidrIpv6
input.IpPermissionsEgress[]
```
