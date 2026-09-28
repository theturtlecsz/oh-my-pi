# Fleet Knowledge Release Criteria Crosswalk

This table maps the Fleet Knowledge v1 release criteria to their defending automated contract tests and owning task identities.

| Criterion | Automated (`path::test`) | Owner IDs |
| :--- | :--- | :--- |
| Exact task capture and retained source | `python/omp-knowledge/tests/test_learning_record.py::test_drain_traces_finished_item_record_and_keeps_no_lesson_path`<br>`python/omp-knowledge/tests/test_learning_capture_execute.py::test_execute_completion_is_captured_and_accepted_on_pass`<br>`python/omp-knowledge/tests/test_source_import.py::test_fixtures_publish_isolated_by_repository` | OMP-278-s07 |
| Attributed proposal and native acceptance | `python/omp-knowledge/tests/test_learning_proposals.py::test_accepted_creates_procedure_v1_with_attribution`<br>`python/omp-knowledge/tests/test_learning_capture_execute.py::test_execute_completion_is_captured_and_accepted_on_pass` | - |
| Fresh-task reuse with recorded outcome | `python/omp-knowledge/tests/test_learning_uses.py::test_matching_context_supplies_procedure` | OMP-278-s07 |
| Counter-evidence, narrowing, and retraction | `python/omp-knowledge/tests/test_learning_corrections.py::test_withdraw_hides_procedure_while_cleanup_pending`<br>`python/omp-knowledge/tests/test_fk7_failure_modes.py::test_stale_cache` | OMP-278-s07 |
| Two actual repositories verified | `python/omp-knowledge/tests/test_fk7_journey.py::test_two_repository_journey` | OMP-312-s08, OMP-278-s06, OMP-278-s07 |
| Candidate A/B real Cognee isolation | `python/omp-knowledge/tests/test_snapshot_isolation.py::test_real_engine_snapshot_ab_isolation_and_publishing`<br>`python/omp-knowledge/tests/test_vector_projection.py::test_building_b_leaves_a_rows_and_digests_unchanged` | - |
| Repository identity and scope stability | `python/omp-work/tests/test_knowledge_source.py::test_resolve_identity_worktree_sibling_and_url_forms`<br>`python/omp-knowledge/tests/test_context_sources.py::test_identity_from_view_binds_work_key_and_candidate` | - |
| Context budget and dispatch revalidation | `python/omp-knowledge/tests/test_context_compiler.py::test_budget_trims_optional_items_and_stays_within_budget`<br>`python/omp-knowledge/tests/test_context_sources.py::test_exact_items_render_revision_and_current_receipt`<br>`python/omp-knowledge/tests/test_context_routes.py::test_vector_items_render_for_permitted_published_and_denied_for_unpermitted` | OMP-278-s06 |
| Service resilience and conflict handling | `python/omp-knowledge/tests/test_error_taxonomy.py::test_idempotency_conflict_carries_operation_and_both_digests`<br>`python/omp-knowledge/tests/test_fk7_failure_modes.py::test_generator_outage`<br>`python/omp-knowledge/tests/test_inference_routes.py::test_duplicate_role_is_refused` | OMP-278-s06 |
| Backup, rebuild, and rollback demonstrated | `python/omp-knowledge/tests/test_maintenance_backup.py::test_backup_while_writer_active_records_match_copy`<br>`python/omp-knowledge/tests/test_vector_projection.py::test_backup_and_rollback_restore_vectors_sqlite_byte_identical`<br>`python/omp-knowledge/tests/test_context_routes.py::test_backup_without_context_routes_sqlite_rebuilds` | OMP-312-s08, OMP-278-s06 |
| Qualified RTX 5090 roles and explicit routes | `python/omp-knowledge/tests/test_inference_routes.py::test_valid_file_loads_and_pins_the_declared_identity`<br>`python/omp-knowledge/tests/test_inference_embedding.py::test_generation_id_is_stable_and_changes_with_every_field` | OMP-278-s06 |
| Native audit and delivery gates | `none` | OMP-278-s08 |
