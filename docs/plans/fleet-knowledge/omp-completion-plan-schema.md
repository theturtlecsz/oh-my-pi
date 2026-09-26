# omp-completion.plan/1 schema excerpt

Plan JSON must contain:

```json
{
  "contractVersion": "omp-completion.plan/1",
  "planId": "nonempty",
  "baseline": {"kind":"file-manifest","root":"/abs","hashAlgorithm":"sha256","files":{"relative/path":{"sha256":"64hex","bytes":1}},"declaredAbsent":[]},
  "ownedPaths": {"root":"/abs","authoring":["relative/path"],"deletions":[],"evidenceRoot":"/abs/evidence/plan"},
  "evalDefaults": {"runner":"/abs/runner","baseArgv":[],"cwdKind":"candidateRoot","templateVariables":["{{candidateRoot}}","{{artifactDir}}","{{evalId}}","{{scratch}}"],"env":{},"envForbidden":[],"oracle":"...","outputCapBytes":1048576,"timeoutMs":900000},
  "evals": [{"id":"E01","required":true,"argv":[],"minTests":1,"asserts":["observable contract"]}],
  "criteria": [{"id":"criterion","text":"observable requirement","evals":["E01"]}],
  "repairPolicy": {"maxRepairLoopsPerStep":2},
  "slices": [{"id":"S1","file":"relative/path","op":"create","requires":[],"content":"..."}],
  "provenance": {"pinnedDeepImports": {}}
}
```

All paths are absolute only where shown. `ownedPaths.authoring` and slice files are safe paths
relative to `baseline.root`. Every baseline hash is exact; no null placeholders. Every required
eval is referenced by criteria and has model-free behavior assertions. `provenance` is extra plan
metadata accepted by runner; pinned imports may be `{}`.
