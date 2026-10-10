Review the database changes in commit b9793a797d403da7be32c84b90c22189635a97cb.

## Scope
The following files are in scope for this review:
- python/omp-work/src/omp_work/v1/store.py

## Instructions
1. Read the scope files to understand their context and current implementation.
2. Describe the change and inspect for any database issues, concurrency bugs, migration defects, or data integrity risks.
3. Make no edits to any files. This is a read-only review.

## Diff
```diff
diff --git a/python/omp-work/src/omp_work/v1/store.py b/python/omp-work/src/omp_work/v1/store.py
index d8c06bd..e1bfbfb 100644
--- a/python/omp-work/src/omp_work/v1/store.py
+++ b/python/omp-work/src/omp_work/v1/store.py
@@ -268,6 +268,14 @@ class WorkStore(Protocol):
         after: tuple[datetime, UUID] | None = None,
         limit: int = 100,
     ) -> dict[str, object]: ...
+    def events(
+        self,
+        workspace_id: UUID,
+        actor_id: UUID,
+        after: int = 0,
+        limit: int = 500,
+    ) -> dict[str, object]: ...
+


 class WorkStoreError(Exception):
@@ -542,6 +550,10 @@ class PostgresWorkStore:
             }
         )
         outcome = "refused" if result.get("status") == "refused" else "applied"
+        cur.execute(
+            "SELECT pg_advisory_xact_lock(hashtextextended('omp_audit.domain_events:' || %s, 0))",
+            (envelope.workspace_id,),
+        )
         cur.execute(
             "INSERT INTO omp_audit.domain_events(event_id,workspace_id,aggregate_type,aggregate_id,aggregate_version,actor_id,actor_kind,capability_id,request_id,correlation_id,operation_id,causation_id,event_type,outcome,payload,payload_sha256,previous_event_sha256,event_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
             (
@@ -5710,4 +5722,82 @@ class PostgresWorkStore:
                 "next_created_at": next_created_at,
                 "next_work_id": next_work_id,
             }
+
+    def events(
+        self,
+        workspace_id: UUID,
+        actor_id: UUID,
+        after: int = 0,
+        limit: int = 500,
+    ) -> dict[str, object]:
+        if not 1 <= limit <= 500:
+            raise WorkStoreError("invalid_request", ("limit must be between 1 and 500",))
+        if after < 0:
+            raise WorkStoreError("invalid_request", ("after must be non-negative",))
+
+        with self._transaction(workspace_id, actor_id) as cur:
+            query = (
+                "WITH watermark AS ("
+                " SELECT COALESCE(MAX(sequence), 0) AS wm"
+                " FROM omp_audit.domain_events"
+                " WHERE workspace_id = %s"
+                ") "
+                "SELECT "
+                " w.wm AS watermark_sequence, "
+                " e.event_id, "
+                " e.sequence, "
+                " e.workspace_id, "
+                " e.aggregate_type, "
+                " e.aggregate_id, "
+                " e.aggregate_version, "
+                " e.actor_id, "
+                " e.actor_kind, "
+                " e.capability_id, "
+                " e.request_id, "
+                " e.correlation_id, "
+                " e.operation_id, "
+                " e.causation_id, "
+                " e.event_type, "
+                " e.outcome, "
+                " e.payload, "
+                " e.payload_sha256, "
+                " e.previous_event_sha256, "
+                " e.event_sha256, "
+                " e.occurred_at "
+                "FROM watermark w "
+                "LEFT JOIN LATERAL ("
+                " SELECT * "
+                " FROM omp_audit.domain_events "
+                " WHERE workspace_id = %s "
+                "   AND sequence > %s "
+                "   AND sequence <= w.wm "
+                " ORDER BY sequence ASC "
+                " LIMIT %s"
+                ") e ON true "
+                "ORDER BY e.sequence ASC NULLS LAST"
+            )
+            cur.execute(query, (workspace_id, workspace_id, after, limit + 1))
+            rows = cur.fetchall()
+            watermark_sequence = int(rows[0]["watermark_sequence"]) if rows else 0
+            if not rows or rows[0]["event_id"] is None:
+                return {
+                    "events": (),
+                    "watermark_sequence": watermark_sequence,
+                    "next_after_sequence": after,
+                    "has_more": False,
+                }
+            has_more = len(rows) > limit
+            page_rows = rows[:limit] if has_more else rows
+            events = tuple(
+                {k: r[k] for k in r if k != "watermark_sequence"}
+                for r in page_rows
+            )
+            next_after_sequence = int(page_rows[-1]["sequence"])
+            return {
+                "events": events,
+                "watermark_sequence": watermark_sequence,
+                "next_after_sequence": next_after_sequence,
+                "has_more": has_more,
+            }
+


```