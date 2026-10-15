# NSI-4 and NSI-5 ownership — waiting for Chris

Chris chooses; both stay pending. This record is prepared, not an owner decision.

Source: [full programme audit](../report/omp-full-programme-audit-2026-09-18.md),
rows NSI4 and NSI5. Their full contracts and exit evidence remain required.

NSI-DISPOSITION: NSI-4=owner:OMP-634 NSI-5=owner:OMP-634

```json
{
  "phase": "recorded",
  "NSI-4": {
    "row": "NSI4",
    "title": "Gap-directed questioning and owner decision loop",
    "status": "owner",
    "owner": "OMP-634",
    "deferral": null,
    "date": "2026-10-15",
    "wording": "OMP-634 owns gap-directed questioning (NSI-4) and readiness withholding and receipts (NSI-5): it tests whether intake asks the owner about missing requirements and withholds ready-for-ratification until they are answered, and it carries the fixes.",
    "evidence": "NSI-6 shadow (docs/nsi6/shadow.json, report_sha256 2ec897320afb) showed 0 gap questions on missing-requirement cases and 13/19 false-ready in both routes."
  },
  "NSI-5": {
    "row": "NSI5",
    "title": "Acceptance compilation, semantic ratification and readiness receipts",
    "status": "owner",
    "owner": "OMP-634",
    "deferral": null,
    "date": "2026-10-15",
    "wording": "OMP-634 owns gap-directed questioning (NSI-4) and readiness withholding and receipts (NSI-5): it tests whether intake asks the owner about missing requirements and withholds ready-for-ratification until they are answered, and it carries the fixes.",
    "evidence": "NSI-6 shadow (docs/nsi6/shadow.json, report_sha256 2ec897320afb) showed 0 gap questions on missing-requirement cases and 13/19 false-ready in both routes."
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
