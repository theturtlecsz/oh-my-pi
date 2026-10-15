## Typed completeness guidance

The draft is converted to a typed claim set and checked for completeness, so every claim must stand alone.

- Each requirement the source states becomes its own constraint or acceptance criterion with a stable id; never merge two requirements into one claim.
- Each constraint names one subject in `key` and one value; reuse the same `key` for statements about the same subject so conflicts surface.
- Each acceptance criterion states an observable outcome and names its oracle, or `null` when none is stated.
- Each unknown is material when leaving it unanswered would change scope, authorization or product behavior.
- Every claim cites the span that supports it; a claim with no supporting span is an assumption and belongs in `unknowns`.
