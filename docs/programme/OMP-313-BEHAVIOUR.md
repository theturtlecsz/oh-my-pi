# OMP-313 outcome behaviours

Three behaviours decide the autoresearch outcome. Presence checks (`code_presence`, `fixture_only`, `self_score`) and `unknown_as_zero` never satisfy one.

```json
{
  "item": "OMP-313",
  "satisfied": false,
  "behaviours": [
    {
      "id": "adaptivity-vs-baseline",
      "observation": "Compare B3 with B2 on a matched budget.",
      "pass": "B3 changes its next selection when a trusted observation changes, on B2's budget.",
      "fail": "Untrusted score or equivalent repeat.",
      "inconclusive": "Matched B2 and B3 arms are missing.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    },
    {
      "id": "held-out-transfer",
      "observation": "confirmation outside development.",
      "pass": "Every outside-development confirmation delta is positive and the content is disjoint.",
      "fail": "Not disjoint, or an outside delta is non-positive.",
      "inconclusive": "No outside-development delta is recorded.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    },
    {
      "id": "measured-improvement",
      "observation": "unknown cost leaves the interval open.",
      "pass": "The interval lies entirely above zero and every cost axis is known.",
      "fail": "Untrusted, repeated, unknown-as-zero, or below zero.",
      "inconclusive": "Zero is inside the interval, a bound or cost is unknown, or drift has no fresh window.",
      "rejects": ["code_presence", "fixture_only", "self_score", "unknown_as_zero"]
    }
  ],
  "rules_text": [
    "Unknown safety or authority is not permission.",
    "Noncritical uncertainty is a bounded investigation with the assumption recorded.",
    "Only a consequential unresolved decision goes to Chris.",
    "A null or negative comparison is not measured improvement.",
    "Five seeds are not programme qualification.",
    "No useful benefit leaves the capability off and offers Chris revise, defer, or retire.",
    "Technical delivery at merge is not product acceptance."
  ],
  "rules": {
    "q1_unknown_authority_is_permission": false,
    "q2_null_is_improvement": false,
    "q2_five_seeds_are_programme_qualification": false,
    "q3_merge_is_product_acceptance": false
  },
  "packages": {
    "prerequisite": ["R00", "R01", "R02", "R03", "R04", "R05", "R06", "R07", "R08", "R09", "R12", "R14", "R15", "R16", "R17"],
    "adaptivity": ["R10", "R11", "R13"],
    "improvement": ["R18"],
    "all_three": ["R19"]
  }
}
```
