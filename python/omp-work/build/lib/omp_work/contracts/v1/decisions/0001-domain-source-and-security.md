# 0001 — Domain source and security

## Decision

`work.omp.dev/v1` is the sole future authority for HOME-team workflow data. PostgreSQL UUIDv7 IDs are canonical; immutable globally unique `HOME-<number>` and transactionally allocated `OMP-<number>` aliases are human handles. Revisions append only. `parent`, `blocks`, and `duplicate_of` are directed acyclic relations; an item has at most one active parent. `related` is symmetric.

Every canonical row is workspace-scoped. Future DDL enables and forces RLS, rejects absent transaction-local workspace/actor claims, revokes `PUBLIC`, and grants no runtime role superuser or `BYPASSRLS`. Reserved roles: `omp_work_owner` (NOLOGIN), `omp_work_migrator`, `omp_work_app`, `omp_work_importer`, `omp_work_readonly`, `omp_work_backup`.

Scopes are `work.read`, `work.candidate.read`, `work.mutate`, `work.approve`, `work.close`, `work.import`, and `work.operate`. The owner host may hold read/mutate/approve/close. Task agents and auditors receive only candidate-bounded read access; importer/operator capabilities are separate and non-model.

DSNs stay in WorkService/operator processes. Mode-0600 operator-managed bearer files stay at the top-level extension host, are stripped from task-agent environments, never serialized, and rotate after compromise, role change, cutover, or recovery. Extensions and agents never receive PostgreSQL credentials.

The source boundary is all HOME-team workflow data: worlds/initiatives, surfaces/projects/health, promises/milestones, issues in all states, states/labels/relations, OMP-marked comments, attachment metadata/URLs, and referenced users. Other teams, unrelated documents, and attachment bytes are excluded unless explicitly evidence. Legacy comments remain history and grant no authority.
