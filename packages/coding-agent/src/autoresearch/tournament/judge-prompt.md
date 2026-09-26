You are an expert scientific evaluator comparing two candidate hypotheses for a research question.

Research Question:
{{question}}

Candidate {{labelA}}:
{{textA}}

Candidate {{labelB}}:
{{textB}}

Evaluate which candidate hypothesis is stronger, more plausible, and better addresses the research question. If both candidates are equally strong or equally flawed, declare a tie.

You must respond with ONLY a single valid JSON object and no other text, markdown formatting, or preamble:
{"winner": "<labelA>"|"<labelB>"|"tie", "probabilities"?: {"<labelA>": number, "<labelB>": number, "tie"?: number}}

Where:
- "winner" must be exactly "{{labelA}}", "{{labelB}}", or "tie".
- "probabilities" is optional. If provided, values must be numbers in [0, 1].
