Review the database changes in commit 3f38b2984d612e4d233f63419505cc8fedaead58.

## Scope
The following files are in scope for this review:
- python/omp-work/src/omp_work/operations/migrations/0029_bot_filed_intake_hold.sql
- python/omp-work/src/omp_work/v1/store.py

## Instructions
1. Read the scope files to understand their context and current implementation.
2. Describe the change and inspect for any database issues, concurrency bugs, migration defects, or data integrity risks.
3. Make no edits to any files. This is a read-only review.

## Diff
```diff
diff --git a/python/omp-work/src/omp_work/operations/migrations/0029_bot_filed_intake_hold.sql b/python/omp-work/src/omp_work/operations/migrations/0029_bot_filed_intake_hold.sql
new file mode 100644
index 0000000..122d36e
--- /dev/null
+++ b/python/omp-work/src/omp_work/operations/migrations/0029_bot_filed_intake_hold.sql
@@ -0,0 +1,25 @@
+-- OMP-407: record who filed each item and track owner intake decisions.
+
+ALTER TABLE omp_work.work_items NO FORCE ROW LEVEL SECURITY;
+
+ALTER TABLE omp_work.work_items ADD COLUMN filed_by_kind text NULL;
+
+CREATE TABLE omp_work.intake_decisions (
+  workspace_id uuid NOT NULL,
+  work_id uuid NOT NULL,
+  answer text NOT NULL CHECK (answer = 'approve'),
+  answered_by text NOT NULL,
+  answered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
+  operation_id uuid NOT NULL,
+  PRIMARY KEY (workspace_id, work_id),
+  FOREIGN KEY (workspace_id, work_id) REFERENCES omp_work.work_items(workspace_id, work_id)
+);
+
+SELECT omp_control.install_workspace_rls('omp_work.intake_decisions'::regclass, 'workspace_id');
+
+REVOKE ALL ON omp_work.intake_decisions FROM omp_work_app, omp_work_readonly, omp_work_importer;
+GRANT SELECT, INSERT ON omp_work.intake_decisions TO omp_work_app;
+GRANT SELECT ON omp_work.intake_decisions TO omp_work_readonly;
+REVOKE DELETE, TRUNCATE ON omp_work.intake_decisions FROM omp_work_app, omp_work_readonly, omp_work_importer;
+
+ALTER TABLE omp_work.work_items FORCE ROW LEVEL SECURITY;
diff --git a/python/omp-work/src/omp_work/v1/store.py b/python/omp-work/src/omp_work/v1/store.py
index 2d19e42..00df8ce 100644
--- a/python/omp-work/src/omp_work/v1/store.py
+++ b/python/omp-work/src/omp_work/v1/store.py
@@ -2,7 +2,7 @@ from __future__ import annotations

 import json
 import re
-from collections.abc import Iterator
+from collections.abc import Iterable, Iterator
 from contextlib import contextmanager
 from datetime import UTC, datetime
 from typing import Any, Protocol
@@ -30,6 +30,13 @@ from .canonical import (
     text_sha256,
     validate_execution_paths,
 )
+from .intake_hold import (
+    IntakeClassification,
+    IntakeFacts,
+    Link,
+    ScopeClass,
+    classify_intake,
+)
 from .models import (
     LIVE_CLOSE_ATTEMPT_STATES,
     MAX_ACCEPTED_REPORTS,
@@ -272,11 +279,85 @@ class WorkStore(Protocol):
     ) -> dict[str, object]: ...


+def _intake_classifications(
+    cur: psycopg.Cursor[dict[str, object]],
+    workspace_id: UUID,
+    work_ids: Iterable[UUID],
+) -> dict[UUID, IntakeClassification]:
+    target_ids = {UUID(str(wid)) for wid in work_ids}
+    if not target_ids:
+        return {}
+
+    target_id_strs = [str(wid) for wid in target_ids]
+    cur.execute(
+        "SELECT source_work_id, target_work_id, kind, active FROM omp_work.work_relations WHERE workspace_id=%s AND active=true AND kind = ANY(%s) AND (source_work_id = ANY(%s) OR target_work_id = ANY(%s))",
+        (workspace_id, ["parent", "blocks"], target_id_strs, target_id_strs),
+    )
+    rel_rows = cur.fetchall()
+
+    all_work_ids = set(target_ids)
+    links: list[Link] = []
+    for row in rel_rows:
+        src = UUID(str(row["source_work_id"]))
+        tgt = UUID(str(row["target_work_id"]))
+        all_work_ids.add(src)
+        all_work_ids.add(tgt)
+        links.append(
+            Link(
+                source_work_id=src,
+                target_work_id=tgt,
+                kind=str(row["kind"]),
+                active=bool(row["active"]),
+            )
+        )
+
+    all_id_strs = [str(wid) for wid in all_work_ids]
+    cur.execute(
+        "SELECT i.work_id, i.state, i.archived, i.filed_by_kind, r.description FROM omp_work.work_items i LEFT JOIN omp_work.work_revisions r ON r.revision_id = i.current_revision_id WHERE i.workspace_id=%s AND i.work_id = ANY(%s)",
+        (workspace_id, all_id_strs),
+    )
+    fact_rows = cur.fetchall()
+
+    cur.execute(
+        "SELECT work_id FROM omp_work.intake_decisions WHERE workspace_id=%s AND work_id = ANY(%s) AND answer='approve'",
+        (workspace_id, all_id_strs),
+    )
+    intake_approved_ids = {UUID(str(row["work_id"])) for row in cur.fetchall()}
+
+    facts: list[IntakeFacts] = []
+    for row in fact_rows:
+        wid = UUID(str(row["work_id"]))
+        facts.append(
+            IntakeFacts(
+                work_id=wid,
+                state=str(row["state"]),
+                archived=bool(row["archived"]),
+                filed_by_kind=str(row["filed_by_kind"])
+                if row["filed_by_kind"] is not None
+                else None,
+                description=str(row["description"])
+                if row["description"] is not None
+                else "",
+                intake_approved=wid in intake_approved_ids,
+            )
+        )
+
+    classified = classify_intake(facts, links)
+    return {wid: classified[wid] for wid in target_ids if wid in classified}
+

 class PostgresWorkStore(ResearchStoreMixin):
     def __init__(self, config: OperationsConfig) -> None:
         self._config = config

+    def _intake_classifications(
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        workspace_id: UUID,
+        work_ids: Iterable[UUID],
+    ) -> dict[UUID, IntakeClassification]:
+        return _intake_classifications(cur, workspace_id, work_ids)
+
     @contextmanager
     def _transaction(
         self, workspace_id: UUID, actor_id: UUID, *, serializable: bool = False
@@ -424,9 +505,11 @@ class PostgresWorkStore(ResearchStoreMixin):
                         )
                 self._require_unexpired_execution(cur, envelope)
                 if command.type == "create_work_batch":
-                    result = self._create_batch(cur, envelope)
+                    result = self._create_batch(cur, envelope, actor_kind)
                 elif command.type == "create_same_session_child":
-                    result = self._create_same_session_child(cur, envelope)
+                    result = self._create_same_session_child(
+                        cur, envelope, actor_kind
+                    )
                 elif command.type == "revise_work":
                     result = self._revise(cur, envelope)
                 elif command.type == "set_work_state":
@@ -633,12 +716,18 @@ class PostgresWorkStore(ResearchStoreMixin):
         )

     def _create_batch(
-        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        envelope: CommandEnvelope,
+        actor_kind: str,
     ) -> dict[str, object]:
         return {
             "type": "create_work_batch",
             "items": self._create_items(
-                cur, envelope.workspace_id, envelope.command.payload
+                cur,
+                envelope.workspace_id,
+                envelope.command.payload,
+                filed_by_kind=actor_kind,
             ),
         }

@@ -647,6 +736,7 @@ class PostgresWorkStore(ResearchStoreMixin):
         cur: psycopg.Cursor[dict[str, object]],
         workspace_id: UUID,
         payload: CreateWorkBatchPayload,
+        filed_by_kind: str | None = None,
     ) -> list[dict[str, object]]:
         """Batch-creation primitive shared by create_work_batch and the OMP-139
         atomic same-session filing: alias allocation, duplicate-title refusal,
@@ -742,8 +832,15 @@ class PostgresWorkStore(ResearchStoreMixin):
                 }
             )
             cur.execute(
-                "INSERT INTO omp_work.work_items(work_id,workspace_id,state,current_revision_id,project_id) VALUES(%s,%s,%s,%s,%s)",
-                (work_id, workspace_id, item.state, revision_id, item.project_id),
+                "INSERT INTO omp_work.work_items(work_id,workspace_id,state,current_revision_id,project_id,filed_by_kind) VALUES(%s,%s,%s,%s,%s,%s)",
+                (
+                    work_id,
+                    workspace_id,
+                    item.state,
+                    revision_id,
+                    item.project_id,
+                    filed_by_kind,
+                ),
             )
             cur.execute(
                 "INSERT INTO omp_work.work_aliases(work_id,workspace_id,key,origin) VALUES(%s,%s,%s,'local')",
@@ -807,7 +904,10 @@ class PostgresWorkStore(ResearchStoreMixin):
         return items

     def _create_same_session_child(
-        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
+        self,
+        cur: psycopg.Cursor[dict[str, object]],
+        envelope: CommandEnvelope,
+        actor_kind: str,
     ) -> dict[str, object]:
         """OMP-139: one serializable transaction files a same-session found-and-fixed
         child — the BACKLOG child inheriting the parent's project, the active
@@ -871,7 +971,10 @@ class PostgresWorkStore(ResearchStoreMixin):
             update={"state": "BACKLOG", "project_id": parent_project}
         )
         items = self._create_items(
-            cur, envelope.workspace_id, CreateWorkBatchPayload(items=(child_input,))
+            cur,
+            envelope.workspace_id,
+            CreateWorkBatchPayload(items=(child_input,)),
+            filed_by_kind=actor_kind,
         )
         child = items[0]
         child_id, child_revision_id = (
@@ -5937,6 +6040,7 @@ class PostgresWorkStore(ResearchStoreMixin):
                     ),
                 )
             ),
+            filed_by_kind=actor_kind,
         )[0]
         intake_work_id = UUID(str(item["work_id"]))
         intake_revision_id = UUID(str(item["revision_id"]))
@@ -6234,6 +6338,19 @@ class PostgresWorkStore(ResearchStoreMixin):
                     self._item_view(cur, workspace_id, key=row["key"])
                     for row in cur.fetchall()
                 ]
+                work_ids = [UUID(str(item["work_id"])) for item in items]
+                classifications = self._intake_classifications(
+                    cur, workspace_id, work_ids
+                )
+                for item in items:
+                    wid = UUID(str(item["work_id"]))
+                    classification = classifications.get(wid)
+                    if classification is not None:
+                        item["scope_class"] = classification.scope_class.value
+                        item["intake_hold"] = classification.held
+                    else:
+                        item["scope_class"] = ScopeClass.owner_filed.value
+                        item["intake_hold"] = False
                 cur.execute(
                     "SELECT workspace_id,source_work_id,target_work_id,kind,active FROM omp_work.work_relations WHERE workspace_id=%s ORDER BY created_at LIMIT 5000",
                     (workspace_id,),

```