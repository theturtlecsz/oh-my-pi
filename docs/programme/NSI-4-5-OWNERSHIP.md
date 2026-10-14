# NSI-4 and NSI-5 ownership — waiting for Chris

Chris chooses; both stay pending. This record is prepared, not an owner decision.

Source: [full programme audit](../report/omp-full-programme-audit-2026-09-18.md),
rows NSI4 and NSI5. Their full contracts and exit evidence remain required.

NSI-DISPOSITION: NSI-4=pending NSI-5=pending

```json
{
  "phase": "prepared",
  "NSI-4": {
    "row": "NSI4",
    "title": "Gap-directed questioning and owner decision loop",
    "status": "pending",
    "owner": null,
    "deferral": null,
    "date": null,
    "wording": null,
    "evidence": null
  },
  "NSI-5": {
    "row": "NSI5",
    "title": "Acceptance compilation, semantic ratification and readiness receipts",
    "status": "pending",
    "owner": null,
    "deferral": null,
    "date": null,
    "wording": null,
    "evidence": null
  }
}
```

Allowed status: `pending|owner|deferral`. Chris may name an owner or record a
deferral with keys `reason` and `revisit`; no choice is recorded here. Date,
wording and evidence stay null until Chris records that decision.

- OMP-266 bounded P12 is not NSI-4 and is not NSI-5.
- Missing NSI-4 owner or disposition refuses OMP-268 E2 admission.
- Missing NSI-5 owner or disposition leaves OMP-269 E3 admission unresolved.
- A deferral or a pending record is not runtime readiness and does not remove the E2/E3 admission gate.

Naming an owner or recording a deferral is not qualified runtime proof and does not admit E2 or E3.
