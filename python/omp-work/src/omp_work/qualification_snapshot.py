"""Read existing qualification sources without mutating or repairing authority."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import IsolationLevel, sql
from psycopg.rows import dict_row

from omp_work.operations.config import OperationsConfig
from omp_work.operations.database import _connect
from omp_work.qualification_records import Exclusion, Snapshot, decode_evidence
from omp_work.v1.canonical import canonical_json
from omp_work.v1.models import EvidenceReceipt
from omp_work.v1.store_shared import row_json


@dataclass(frozen=True)
class SourceRecord:
    """Complete stored row; source and id disambiguate overlapping identifiers.

    Chronology includes original timestamps and sequences in row_json, even
    when an item has since changed state or a job has retried.
    """
    source: str
    record_id: str
    recorded_at: datetime
    row_json: str


@dataclass(frozen=True)
class LedgerSnapshot(Snapshot):
    records: tuple[SourceRecord, ...] = ()


# All identifiers are static. No source payload controls a query or file read.
_SOURCES = (
    ("omp_work.work_items", "work_id", "created_at"),
    ("omp_work.work_revisions", "revision_id", "recorded_at"),
    ("omp_evidence.receipts", "receipt_id", "issued_at"),
    ("omp_audit.domain_events", "event_id", "occurred_at"),
    ("omp_research.campaigns", "campaign_id", "created_at"),
    ("omp_research.trials", "trial_id", "proposed_at"),
    ("omp_research.observations", "observation_id", "recorded_at"),
    ("omp_research.components", "component_sha256", "registered_at"),
    # Settlement payload and settled_at live on the job, not a separate table.
    ("omp_jobs.jobs", "job_id", "created_at"),
)


def load_snapshot(config: OperationsConfig) -> LedgerSnapshot:
    """One repeatable-read, read-only view with explicit workspace isolation.

    Retain unannotated and invalid evidence rows for exclusion reports. Database
    errors propagate: an unavailable database is not an empty qualification run.
    Stored campaign/component metadata does not establish an O9 freeze.
    """
    workspace_id = config.workspace_id()
    actor_id = config.actor_id()
    records: list[SourceRecord] = []
    receipts: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    with _connect(config, "omp_work_readonly") as conn:
        conn.read_only = True
        conn.isolation_level = IsolationLevel.REPEATABLE_READ
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SET LOCAL search_path = pg_catalog")
                cur.execute(
                    "SELECT set_config('omp.workspace_id', %s, true), set_config('omp.actor_id', %s, true)",
                    (str(workspace_id), str(actor_id)),
                )
                for source, id_field, time_field in _SOURCES:
                    cur.execute(
                        sql.SQL("SELECT * FROM {} WHERE workspace_id=%s ORDER BY {}, {}").format(
                            sql.Identifier(*source.split(".")),
                            sql.Identifier(time_field),
                            sql.Identifier("sequence" if source == "omp_audit.domain_events" else id_field),
                        ),
                        (workspace_id,),
                    )
                    for row in cur.fetchall():
                        projected = row_json(row)
                        assert projected is not None
                        records.append(SourceRecord(
                            source, str(row[id_field]), row[time_field], canonical_json(projected),
                        ))
                        if source == "omp_evidence.receipts":
                            # Receipt model excludes workspace_id and any storage-only fields.
                            receipts.append({
                                key: value for key, value in projected.items()
                                if key in EvidenceReceipt.model_fields
                            })
                        elif source == "omp_research.observations":
                            observations.append(projected)
                # omp_jobs has no RLS; events have no workspace column.
                cur.execute(
                    "SELECT e.* FROM omp_jobs.job_events e "
                    "JOIN omp_jobs.jobs j ON j.job_id=e.job_id "
                    "WHERE j.workspace_id=%s ORDER BY e.at, e.job_id, e.seq",
                    (workspace_id,),
                )
                for row in cur.fetchall():
                    records.append(SourceRecord(
                        "omp_jobs.job_events", f"{row['job_id']}:{row['seq']}",
                        row["at"], canonical_json(row_json(row)),
                    ))
    evidence = decode_evidence(receipts=receipts, observations=observations)
    reported = {
        record.record_id for record in (*evidence.receipts, *evidence.observations, *evidence.exclusions)
    }
    records.sort(key=lambda record: record.recorded_at)
    return LedgerSnapshot(
        receipts=evidence.receipts,
        observations=evidence.observations,
        acceptance_record_ids=evidence.acceptance_record_ids,
        exclusions=evidence.exclusions + tuple(
            Exclusion(record.record_id, "O9 freeze and release baseline unavailable")
            for record in records if record.record_id not in reported
        ),
        records=tuple(records),
    )
