-- HOME-147: `python -m omp_work serve` gates startup on collect_health() running
-- as omp_work_app (__main__.py serve branch). 0005_work_service_hardening.sql
-- revoked every omp_control grant 0002/0003 had given the app role and restored
-- only workspaces + idempotent_commands, so the readiness gate's control-table
-- reads and probe write raised permission denied and the service could never
-- start. Restore exactly the grants the gate needs — nothing else. (SELECT on
-- readiness_probe is required by the ON CONFLICT (singleton) arbiter.)
GRANT SELECT ON omp_control.schema_migrations, omp_control.runtime_compatibility, omp_control.operations_evidence TO omp_work_app;
GRANT SELECT, INSERT, UPDATE ON omp_control.readiness_probe TO omp_work_app;
