# Intake support v2 labelling rule

This document is the labelling rule for the intake-support corpus, version 2.
The JSON block under "Rules object" is the complete rules object.
`rule_sha256` and `approval_sha256` in that object are null.

## Labels

A label is decided from the passage and its context read together.

- `supports` is direct entailment. The passage, read with its context, states the claim or directly entails it.
- `contradicts` is the opposite. The passage, read with its context, states the opposite of the claim or directly entails that opposite.
- `neither` means the passage, read with its context, does not establish the claim or its opposite.

## Context

Context defeats quotes. The label follows the passage and the context together. Context around a quote can defeat that quote as an assertion of the claim.

## Near-miss kinds

Every negative is tagged with exactly one of these kinds. A supports pair is untagged.

- `topic_wrong_claim`: the passage is on the claim's topic, and the claim concerns a different fact the passage never states.
- `negation`: the passage matches the claim's wording and asserts the negation.
- `scope_mismatch`: the claim is broader than the passage, or it covers a different scope than the passage.
- `quote_out_of_context`: the passage text matches the claim, and the context defeats that text as an assertion.
- `adjacent_inference`: adjacent facts alone imply neither. The passage states facts next to the claim, and those facts do not establish the claim or its opposite.

## Composition

A passage is verbatim source text of at least 60 and at most 300 whitespace-delimited words, and of at least two sentences. A word is one non-empty piece after splitting the passage on whitespace. A sentence terminator is `.`, `!`, or `?` followed by whitespace or by the end of the passage. Count those terminators; a passage needs at least two.

The train split contains at least 20 negatives. The held-out split (`heldout`) contains at least 60 negatives. Each negative has one matching positive. Each group holds that pair: one tagged negative and one untagged supports positive, on the same passage and context, in the same split. The positive's passage, read with its context, directly entails the positive's claim.

Each of the five near-miss kinds appears at least once in train and at least once in held-out.

A source stays in one split. A group stays in one split. Related groups stay in one split: groups that share a source, and groups that are otherwise related, do not cross between train and held-out.

## Provenance

The passage is copied verbatim from the source lines. The author checks that copy against the named revision. The check is not a fetch. The rules loader does not retrieve the revision.

Source metadata for each passage is:

- a canonical repo-relative path
- a revision of 40 lowercase hexadecimal characters
- an inclusive 1-based line range, both ends included, with the file's first line numbered 1

A canonical repo-relative path is relative, uses POSIX separators, has no leading slash, and has no empty, `.`, or `..` segments.

## Digests

The rules object keeps `rule_sha256` and `approval_sha256` null. Writing either digest into this file would change the file's bytes, so the digests stay outside the file.

An approval JSON object has `version` 2, `rule_sha256` set to the SHA-256 of this file's exact bytes, and `approval_sha256` null. Sealing writes a separate sealed approval. The sealed `approval_sha256` is the SHA-256 of the approval file's exact bytes, the bytes in which `approval_sha256` is still null. The sealed record does not replace those bytes, so the approval hash does not cover itself.

## Rules object

```json
{
  "version": 2,
  "labels": {
    "supports": "The passage, read with its context, states the claim or directly entails it.",
    "contradicts": "The passage, read with its context, states the opposite of the claim or directly entails that opposite.",
    "neither": "The passage, read with its context, does not establish the claim or its opposite."
  },
  "near_miss_kinds": {
    "topic_wrong_claim": "The passage is on the claim's topic, and the claim concerns a different fact the passage never states.",
    "negation": "The passage matches the claim's wording and asserts the negation.",
    "scope_mismatch": "The claim is broader than the passage, or it covers a different scope than the passage.",
    "quote_out_of_context": "The passage text matches the claim, and the context defeats that text as an assertion.",
    "adjacent_inference": "Adjacent facts alone imply neither. The passage states facts next to the claim, and those facts do not establish the claim or its opposite."
  },
  "context_rule": "Context defeats quotes. The label follows the passage and the context together. Context around a quote can defeat that quote as an assertion of the claim.",
  "rule_sha256": null,
  "approval_sha256": null
}
```
