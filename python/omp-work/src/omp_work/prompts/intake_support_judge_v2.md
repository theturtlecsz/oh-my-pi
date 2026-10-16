You decide whether one quoted passage, read with the context around it, supports one claim.

Use only the passage and the context. Context outside the quote can defeat the quote. Ignore authors, venues, and identifiers; they are not evidence.

Choose one verdict:

- supports: the passage and the context state the claim or entail it directly. Requires direct entailment and an exact, verbatim supporting span copied from the passage.
- contradicts: the passage and the context state the opposite of the claim or entail that opposite directly.
- neither: the passage and the context do not establish the claim or its opposite. Adjacent facts alone yield neither.
- abstain: you cannot tell from the passage and the context.

Reply with one JSON object and no other text:

If the verdict is supports, include the exact supporting span:
{"verdict": "supports", "supporting_span": "<exact verbatim substring of passage>"}

Otherwise:
{"verdict": "contradicts|neither|abstain"}

Claim:
{{claim}}

Passage:
{{passage}}

Context:
{{context}}
