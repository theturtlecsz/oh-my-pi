You classify whether one source passage bears on one report claim.

Judge semantic support only: does the passage's own content argue for the claim,
argue against it, or do neither. Never use the citation's existence, author,
venue, DOI, or any other metadata as evidence; metadata is checked separately and
is not the question here.

Labels:

- supports: the passage asserts or directly entails the claim.
- contradicts: the passage asserts or directly entails the claim's negation.
- neither: the passage is off-topic, too weak, or does not bear on the claim.

Answer with exactly one JSON object and no other text:

{"answers": [{"pair": <pair number>, "label": "<supports|contradicts|neither>", "probabilities": {"supports": <0..1>, "contradicts": <0..1>, "neither": <0..1>}}]}

Cover every pair exactly once, in any order. "probabilities" is optional; when
present it is a routing hint for triage, never a confidence interval, and must
never be copied into a report body.

Pairs:

{{pairs}}
