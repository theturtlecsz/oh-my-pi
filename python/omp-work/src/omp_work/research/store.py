from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import psycopg

from omp_work.v1.canonical import canonical_json, sha256
from omp_work.v1.models import (
    CommandEnvelope,
    RecordResearchObservationPayload,
    ResearchCompatibilityManifest,
    ResearchComponentKind,
)
from omp_work.v1.semantics import (
    RESEARCH_CAMPAIGN_TRIAL_ACCEPTING,
    research_campaign_transition_error,
    research_compatibility_error,
)
from omp_work.v1.store_shared import WorkStoreError, row_json

_CAMPAIGN_FIELDS = "campaign_id,workspace_id,work_id,revision_id,domain,spec,spec_sha256,policy_sha256,state,cancel_reason,created_at,admitted_at,cancelled_at,outcome,outcome_reason,concluded_at,blocked_dependency,blocked_from_state,compatibility,compatibility_sha256"
_TRIAL_FIELDS = "trial_id,workspace_id,campaign_id,work_id,decision_id,candidate_digest,experiment_spec_sha256,evaluator_sha256,environment_sha256,input_manifest_sha256,seed,hardware_class,resource_request,policy_sha256,state,archived_reason,proposed_at,archived_at,action,reason"
_OBSERVATION_FIELDS = "observation_id,workspace_id,campaign_id,trial_id,issuer_kind,source_ref,execution_status,commit_sha,payload,payload_sha256,observed_at,recorded_at"
_DELIVERABLE_BINDING_FIELDS = "trial_id,workspace_id,campaign_id,work_id,revision_id,candidate_digest,native_candidate_id,binding_sha256,bound_at"
_COMPONENT_FIELDS = "workspace_id,component_sha256,descriptor,kind,registered_at"


def _component_json(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    res = row_json(row)
    if res is not None and isinstance(res.get("descriptor"), str):
        res["descriptor"] = json.loads(res["descriptor"])
    return res


def _campaign_json(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    res = row_json(row)
    if res is not None:
        if isinstance(res.get("spec"), str):
            res["spec"] = json.loads(res["spec"])
        if isinstance(res.get("blocked_dependency"), str):
            res["blocked_dependency"] = json.loads(res["blocked_dependency"])
        if isinstance(res.get("compatibility"), str):
            res["compatibility"] = json.loads(res["compatibility"])
    return res


def _trial_json(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    res = row_json(row)
    if res is not None and isinstance(res.get("resource_request"), str):
        res["resource_request"] = json.loads(res["resource_request"])
    return res


def _observation_json(row: dict[str, object] | None) -> dict[str, object] | None:
    if row is None:
        return None
    res = row_json(row)
    if res is not None and isinstance(res.get("payload"), str):
        res["payload"] = json.loads(res["payload"])
    return res


def _is_exact_observation_match(
    existing: dict[str, object], payload: RecordResearchObservationPayload
) -> bool:
    if existing.get("observation_id") != payload.observation_id:
        return False
    if existing.get("campaign_id") != payload.campaign_id:
        return False
    if existing.get("trial_id") != payload.trial_id:
        return False
    existing_issuer = (
        existing["issuer_kind"].value
        if hasattr(existing.get("issuer_kind"), "value")
        else str(existing.get("issuer_kind"))
    )
    payload_issuer = (
        payload.issuer_kind.value
        if hasattr(payload.issuer_kind, "value")
        else str(payload.issuer_kind)
    )
    if existing_issuer != payload_issuer:
        return False
    if existing.get("source_ref") != payload.source_ref:
        return False
    existing_status = (
        existing["execution_status"].value
        if hasattr(existing.get("execution_status"), "value")
        else str(existing.get("execution_status"))
    )
    payload_status = (
        payload.execution_status.value
        if hasattr(payload.execution_status, "value")
        else str(payload.execution_status)
    )
    if existing_status != payload_status:
        return False
    if existing.get("commit_sha") != payload.commit_sha:
        return False
    if existing.get("payload_sha256") != payload.payload_sha256:
        return False
    existing_payload = (
        json.loads(existing["payload"])
        if isinstance(existing.get("payload"), str)
        else existing.get("payload")
    )
    if canonical_json(existing_payload) != canonical_json(payload.payload):
        return False
    existing_observed_at = existing.get("observed_at")
    if isinstance(existing_observed_at, str):
        existing_observed_at = datetime.fromisoformat(existing_observed_at)
    if isinstance(existing_observed_at, datetime):
        dt_existing = (
            existing_observed_at
            if existing_observed_at.tzinfo is not None
            else existing_observed_at.replace(tzinfo=UTC)
        ).astimezone(UTC)
        payload_dt = payload.observed_at
        if isinstance(payload_dt, str):
            payload_dt = datetime.fromisoformat(payload_dt)
        dt_payload = (
            payload_dt
            if payload_dt.tzinfo is not None
            else payload_dt.replace(tzinfo=UTC)
        ).astimezone(UTC)
        if dt_existing != dt_payload:
            return False
    else:
        return False
    return True


def _deliverable_binding_json(row: dict[str, object] | None) -> dict[str, object] | None:
    return row_json(row)


class ResearchStoreMixin:
    """Research persistence (omp_research.*) for PostgresWorkStore.

    Host supplies _lock_work_chain; the generic store owns transactions,
    idempotency, dispatch, and events.
    """

    if TYPE_CHECKING:
        def _lock_work_chain(
            self, cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, work_id: UUID
        ) -> dict[str, object]: ...

    def _create_research_campaign(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            self._lock_work_chain(cur, envelope.workspace_id, existing["work_id"])
            domain_val = (
                payload.domain.value
                if hasattr(payload.domain, "value")
                else str(payload.domain)
            )
            spec_data = payload.spec.model_dump(mode="json")
            existing_spec = (
                json.loads(existing["spec"])
                if isinstance(existing.get("spec"), str)
                else existing.get("spec")
            )
            if (
                existing["work_id"] == payload.work_id
                and existing["revision_id"] == payload.revision_id
                and existing["domain"] == domain_val
                and existing["spec_sha256"] == payload.spec_sha256
                and canonical_json(existing_spec) == canonical_json(spec_data)
            ):
                return {
                    "type": "create_research_campaign",
                    "status": "replayed",
                    "campaign": _campaign_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("research campaign differs from existing identity",),
            )

        self._lock_work_chain(cur, envelope.workspace_id, payload.work_id)
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            domain_val = (
                payload.domain.value
                if hasattr(payload.domain, "value")
                else str(payload.domain)
            )
            spec_data = payload.spec.model_dump(mode="json")
            existing_spec = (
                json.loads(existing["spec"])
                if isinstance(existing.get("spec"), str)
                else existing.get("spec")
            )
            if (
                existing["work_id"] == payload.work_id
                and existing["revision_id"] == payload.revision_id
                and existing["domain"] == domain_val
                and existing["spec_sha256"] == payload.spec_sha256
                and canonical_json(existing_spec) == canonical_json(spec_data)
            ):
                return {
                    "type": "create_research_campaign",
                    "status": "replayed",
                    "campaign": _campaign_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("research campaign differs from existing identity",),
            )

        cur.execute(
            "SELECT current_revision_id, archived FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (envelope.workspace_id, payload.work_id),
        )
        item = cur.fetchone()
        if item is None:
            raise WorkStoreError("invalid_request", ("work item not found",))
        if item["archived"]:
            raise WorkStoreError(
                "invalid_request",
                ("cannot create campaign for archived work item",),
            )
        if item["current_revision_id"] != payload.revision_id:
            raise WorkStoreError(
                "stale_evidence",
                ("campaign revision does not match current work revision",),
            )

        spec_data = payload.spec.model_dump(mode="json")
        computed_spec_sha256 = sha256(spec_data)
        if computed_spec_sha256 != payload.spec_sha256:
            raise WorkStoreError(
                "stale_evidence", ("campaign spec digest mismatch",)
            )

        domain_val = (
            payload.domain.value
            if hasattr(payload.domain, "value")
            else str(payload.domain)
        )
        cur.execute(
            f"""
            INSERT INTO omp_research.campaigns(
                campaign_id, workspace_id, work_id, revision_id, domain,
                spec, spec_sha256, policy_sha256, state, cancel_reason,
                created_at, admitted_at, cancelled_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, NULL, 'draft', NULL,
                clock_timestamp(), NULL, NULL
            ) RETURNING {_CAMPAIGN_FIELDS}
            """,
            (
                payload.campaign_id,
                envelope.workspace_id,
                payload.work_id,
                payload.revision_id,
                domain_val,
                canonical_json(spec_data),
                payload.spec_sha256,
            ),
        )
        return {
            "type": "create_research_campaign",
            "status": "applied",
            "campaign": _campaign_json(cur.fetchone()),
        }

    def _register_research_component(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        descriptor_data = payload.descriptor.model_dump(mode="json")
        computed_sha256 = sha256(descriptor_data)
        if computed_sha256 != payload.component_sha256:
            raise WorkStoreError(
                "stale_evidence", ("component digest mismatch",)
            )

        cur.execute(
            f"SELECT {_COMPONENT_FIELDS} FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
            (envelope.workspace_id, payload.component_sha256),
        )
        existing = cur.fetchone()
        if existing is not None:
            return {
                "type": "register_research_component",
                "status": "replayed",
                "component": _component_json(existing),
            }

        cur.execute(
            f"""
            INSERT INTO omp_research.components (
                workspace_id, component_sha256, descriptor
            ) VALUES (%s, %s, %s)
            ON CONFLICT (workspace_id, component_sha256) DO NOTHING
            RETURNING {_COMPONENT_FIELDS}
            """,
            (
                envelope.workspace_id,
                payload.component_sha256,
                canonical_json(descriptor_data),
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                f"SELECT {_COMPONENT_FIELDS} FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
                (envelope.workspace_id, payload.component_sha256),
            )
            row = cur.fetchone()
            return {
                "type": "register_research_component",
                "status": "replayed",
                "component": _component_json(row),
            }
        return {
            "type": "register_research_component",
            "status": "applied",
            "component": _component_json(row),
        }

    def _admit_research_campaign(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        self._lock_work_chain(cur, envelope.workspace_id, campaign["work_id"])
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        # Check if already admitted (either currently admitted or admitted previously before cancellation)
        if campaign["state"] == "admitted" or campaign["admitted_at"] is not None:
            if (
                campaign["work_id"] == payload.work_id
                and campaign["revision_id"] == payload.revision_id
                and campaign["spec_sha256"] == payload.spec_sha256
                and campaign["policy_sha256"] == payload.policy_sha256
                and campaign.get("compatibility_sha256") == payload.compatibility_sha256
            ):
                return {
                    "type": "admit_research_campaign",
                    "status": "replayed",
                    "campaign": _campaign_json(campaign),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("admitted campaign differs from existing admission",),
            )

        if campaign["state"] == "cancelled":
            raise WorkStoreError(
                "invalid_request", ("cannot admit cancelled campaign",)
            )

        if campaign["state"] != "draft":
            raise WorkStoreError(
                "invalid_request",
                (f"cannot admit campaign in state {campaign['state']}",),
            )

        if (
            campaign["work_id"] != payload.work_id
            or campaign["revision_id"] != payload.revision_id
        ):
            raise WorkStoreError(
                "stale_evidence", ("campaign work or revision mismatch",)
            )
        if campaign["spec_sha256"] != payload.spec_sha256:
            raise WorkStoreError(
                "stale_evidence", ("campaign spec digest mismatch",)
            )

        cur.execute(
            "SELECT current_revision_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (envelope.workspace_id, payload.work_id),
        )
        item = cur.fetchone()
        if item is None or item["current_revision_id"] != payload.revision_id:
            raise WorkStoreError(
                "stale_evidence",
                ("work revision has drifted since campaign creation",),
            )

        compat_data = payload.compatibility.model_dump(mode="json")
        computed_compat_sha = sha256(compat_data)
        if computed_compat_sha != payload.compatibility_sha256:
            raise WorkStoreError(
                "stale_evidence", ("compatibility manifest digest mismatch",)
            )

        cur.execute(
            "SELECT kind FROM omp_research.components WHERE workspace_id=%s AND component_sha256=%s",
            (envelope.workspace_id, payload.policy_sha256),
        )
        policy_row = cur.fetchone()
        if policy_row is None:
            raise WorkStoreError("invalid_request", ("unknown policy component",))
        if policy_row["kind"] != "policy":
            raise WorkStoreError(
                "invalid_request", ("policy fingerprint is not a policy component",)
            )

        manifest_entries: list[tuple[str, str]] = []
        for w in payload.compatibility.workers:
            manifest_entries.append(("worker", w))
        for e in payload.compatibility.evaluators:
            manifest_entries.append(("evaluator", e))
        for a in payload.compatibility.audits:
            manifest_entries.append(("audit", a))
        for r in payload.compatibility.releases:
            manifest_entries.append(("release", r))
        for env in payload.compatibility.environments:
            manifest_entries.append(("environment", env))

        if manifest_entries:
            all_hashes = list({sha for _, sha in manifest_entries})
            cur.execute(
                "SELECT component_sha256, kind FROM omp_research.components WHERE workspace_id=%s AND component_sha256 = ANY(%s)",
                (envelope.workspace_id, all_hashes),
            )
            found_components = {
                row["component_sha256"]: row["kind"] for row in cur.fetchall()
            }
            for axis, sha in manifest_entries:
                if sha not in found_components:
                    raise WorkStoreError(
                        "invalid_request", (f"unknown {axis} component",)
                    )
                if found_components[sha] != axis:
                    raise WorkStoreError(
                        "invalid_request",
                        (f"{axis} fingerprint is not a {axis} component",),
                    )

        cur.execute(
            f"""
            UPDATE omp_research.campaigns SET
                state = 'admitted',
                policy_sha256 = %s,
                compatibility = %s,
                compatibility_sha256 = %s,
                admitted_at = clock_timestamp()
            WHERE workspace_id = %s AND campaign_id = %s
            RETURNING {_CAMPAIGN_FIELDS}
            """,
            (
                payload.policy_sha256,
                canonical_json(compat_data),
                payload.compatibility_sha256,
                envelope.workspace_id,
                payload.campaign_id,
            ),
        )
        return {
            "type": "admit_research_campaign",
            "status": "applied",
            "campaign": _campaign_json(cur.fetchone()),
        }

    def _cancel_research_campaign(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        self._lock_work_chain(cur, envelope.workspace_id, campaign["work_id"])
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        if campaign["state"] == "cancelled":
            if (
                campaign["work_id"] == payload.work_id
                and campaign["cancel_reason"] == payload.reason
            ):
                return {
                    "type": "cancel_research_campaign",
                    "status": "replayed",
                    "campaign": _campaign_json(campaign),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("cancelled campaign differs from existing cancellation",),
            )

        if campaign["work_id"] != payload.work_id:
            raise WorkStoreError("invalid_request", ("campaign work mismatch",))

        err = research_campaign_transition_error(campaign["state"], "cancelled")
        if err:
            raise WorkStoreError("invalid_request", (err,))

        cur.execute(
            f"""
            UPDATE omp_research.campaigns SET
                state = 'cancelled',
                cancel_reason = %s,
                cancelled_at = clock_timestamp(),
                blocked_dependency = NULL,
                blocked_from_state = NULL
            WHERE workspace_id = %s AND campaign_id = %s
            RETURNING {_CAMPAIGN_FIELDS}
            """,
            (payload.reason, envelope.workspace_id, payload.campaign_id),
        )
        updated = cur.fetchone()

        cur.execute(
            """
            UPDATE omp_research.trials SET
                state = 'archived',
                archived_reason = 'campaign_cancelled',
                archived_at = clock_timestamp()
            WHERE workspace_id = %s AND campaign_id = %s AND state = 'proposed'
            """,
            (envelope.workspace_id, payload.campaign_id),
        )

        return {
            "type": "cancel_research_campaign",
            "status": "applied",
            "campaign": _campaign_json(updated),
        }

    def _propose_research_trial(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        action_val = (
            payload.action.value
            if hasattr(payload.action, "value")
            else str(payload.action)
        )
        cur.execute(
            f"SELECT {_TRIAL_FIELDS} FROM omp_research.trials WHERE workspace_id=%s AND trial_id=%s",
            (envelope.workspace_id, payload.trial_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            self._lock_work_chain(cur, envelope.workspace_id, existing["work_id"])
            if (
                existing["campaign_id"] == payload.campaign_id
                and existing["work_id"] == payload.work_id
                and existing["decision_id"] == payload.decision_id
                and existing["candidate_digest"] == payload.candidate_digest
                and existing["experiment_spec_sha256"] == payload.experiment_spec_sha256
                and existing["evaluator_sha256"] == payload.evaluator_sha256
                and existing["environment_sha256"] == payload.environment_sha256
                and existing["input_manifest_sha256"] == payload.input_manifest_sha256
                and existing["seed"] == payload.seed
                and existing["hardware_class"] == payload.hardware_class
                and existing["policy_sha256"] == payload.policy_sha256
                and (existing["resource_request"] or None) == (payload.resource_request or None)
                and existing.get("action") == action_val
                and existing.get("reason") == payload.reason
            ):
                return {
                    "type": "propose_research_trial",
                    "status": "replayed",
                    "trial": _trial_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("trial differs from existing identity",),
            )

        self._lock_work_chain(cur, envelope.workspace_id, payload.work_id)
        cur.execute(
            f"SELECT {_TRIAL_FIELDS} FROM omp_research.trials WHERE workspace_id=%s AND trial_id=%s",
            (envelope.workspace_id, payload.trial_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            if (
                existing["campaign_id"] == payload.campaign_id
                and existing["work_id"] == payload.work_id
                and existing["decision_id"] == payload.decision_id
                and existing["candidate_digest"] == payload.candidate_digest
                and existing["experiment_spec_sha256"] == payload.experiment_spec_sha256
                and existing["evaluator_sha256"] == payload.evaluator_sha256
                and existing["environment_sha256"] == payload.environment_sha256
                and existing["input_manifest_sha256"] == payload.input_manifest_sha256
                and existing["seed"] == payload.seed
                and existing["hardware_class"] == payload.hardware_class
                and existing["policy_sha256"] == payload.policy_sha256
                and (existing["resource_request"] or None) == (payload.resource_request or None)
                and existing.get("action") == action_val
                and existing.get("reason") == payload.reason
            ):
                return {
                    "type": "propose_research_trial",
                    "status": "replayed",
                    "trial": _trial_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("trial differs from existing identity",),
            )

        cur.execute(
            "SELECT trial_id FROM omp_research.trials WHERE workspace_id=%s AND decision_id=%s",
            (envelope.workspace_id, payload.decision_id),
        )
        decision_existing = cur.fetchone()
        if decision_existing is not None and decision_existing["trial_id"] != payload.trial_id:
            raise WorkStoreError(
                "idempotency_conflict",
                ("decision_id already used by another trial",),
            )

        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))
        if campaign["work_id"] != payload.work_id:
            raise WorkStoreError("invalid_request", ("campaign work mismatch",))
        if campaign["state"] not in RESEARCH_CAMPAIGN_TRIAL_ACCEPTING:
            raise WorkStoreError(
                "invalid_request",
                (f"cannot propose trial for campaign in state {campaign['state']}",),
            )
        if campaign["policy_sha256"] != payload.policy_sha256:
            raise WorkStoreError(
                "stale_evidence",
                ("trial policy digest does not match campaign policy",),
            )

        if not campaign.get("compatibility_sha256") or not campaign.get("compatibility"):
            raise WorkStoreError(
                "stale_evidence",
                ("legacy campaign has no compatibility manifest; new trials refused",),
            )

        compat_val = campaign["compatibility"]
        if isinstance(compat_val, str):
            compat_val = json.loads(compat_val)
        manifest = (
            ResearchCompatibilityManifest.model_validate(compat_val)
            if not isinstance(compat_val, ResearchCompatibilityManifest)
            else compat_val
        )
        eval_err = research_compatibility_error(
            manifest, ResearchComponentKind.EVALUATOR, payload.evaluator_sha256
        )
        if eval_err:
            raise WorkStoreError("stale_evidence", (eval_err,))

        env_err = research_compatibility_error(
            manifest, ResearchComponentKind.ENVIRONMENT, payload.environment_sha256
        )
        if env_err:
            raise WorkStoreError("stale_evidence", (env_err,))

        resource_request_json = (
            canonical_json(payload.resource_request)
            if payload.resource_request is not None
            else None
        )

        cur.execute(
            f"""
            INSERT INTO omp_research.trials(
                trial_id, workspace_id, campaign_id, work_id, decision_id,
                candidate_digest, experiment_spec_sha256, evaluator_sha256,
                environment_sha256, input_manifest_sha256, seed,
                hardware_class, resource_request, policy_sha256,
                state, archived_reason, proposed_at, archived_at,
                action, reason
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                %s, %s, %s,
                'proposed', NULL, clock_timestamp(), NULL,
                %s, %s
            ) RETURNING {_TRIAL_FIELDS}
            """,
            (
                payload.trial_id,
                envelope.workspace_id,
                payload.campaign_id,
                payload.work_id,
                payload.decision_id,
                payload.candidate_digest,
                payload.experiment_spec_sha256,
                payload.evaluator_sha256,
                payload.environment_sha256,
                payload.input_manifest_sha256,
                payload.seed,
                payload.hardware_class,
                resource_request_json,
                payload.policy_sha256,
                action_val,
                payload.reason,
            ),
        )
        return {
            "type": "propose_research_trial",
            "status": "applied",
            "trial": _trial_json(cur.fetchone()),
        }

    def _record_research_observation(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload: RecordResearchObservationPayload = envelope.command.payload
        cur.execute(
            f"SELECT {_OBSERVATION_FIELDS} FROM omp_research.observations WHERE workspace_id=%s AND observation_id=%s",
            (envelope.workspace_id, payload.observation_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            if _is_exact_observation_match(existing, payload):
                return {
                    "type": "record_research_observation",
                    "status": "replayed",
                    "observation": _observation_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("observation differs from existing identity",),
            )

        issuer_kind_val = (
            payload.issuer_kind.value
            if hasattr(payload.issuer_kind, "value")
            else str(payload.issuer_kind)
        )
        cur.execute(
            f"SELECT {_OBSERVATION_FIELDS} FROM omp_research.observations WHERE workspace_id=%s AND issuer_kind=%s AND source_ref=%s",
            (envelope.workspace_id, issuer_kind_val, payload.source_ref),
        )
        ref_existing = cur.fetchone()
        if ref_existing is not None:
            if ref_existing["observation_id"] == payload.observation_id and _is_exact_observation_match(ref_existing, payload):
                return {
                    "type": "record_research_observation",
                    "status": "replayed",
                    "observation": _observation_json(ref_existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("source_ref already exists with different observation",),
            )

        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))
        if campaign["state"] == "cancelled":
            raise WorkStoreError(
                "invalid_request",
                ("cannot record observation for cancelled campaign",),
            )

        self._lock_work_chain(cur, envelope.workspace_id, campaign["work_id"])

        cur.execute(
            f"SELECT {_OBSERVATION_FIELDS} FROM omp_research.observations WHERE workspace_id=%s AND observation_id=%s",
            (envelope.workspace_id, payload.observation_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            if _is_exact_observation_match(existing, payload):
                return {
                    "type": "record_research_observation",
                    "status": "replayed",
                    "observation": _observation_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("observation differs from existing identity",),
            )

        cur.execute(
            f"SELECT {_OBSERVATION_FIELDS} FROM omp_research.observations WHERE workspace_id=%s AND issuer_kind=%s AND source_ref=%s",
            (envelope.workspace_id, issuer_kind_val, payload.source_ref),
        )
        ref_existing = cur.fetchone()
        if ref_existing is not None:
            if ref_existing["observation_id"] == payload.observation_id and _is_exact_observation_match(ref_existing, payload):
                return {
                    "type": "record_research_observation",
                    "status": "replayed",
                    "observation": _observation_json(ref_existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("source_ref already exists with different observation",),
            )

        if payload.trial_id is not None:
            cur.execute(
                f"SELECT {_TRIAL_FIELDS} FROM omp_research.trials WHERE workspace_id=%s AND trial_id=%s",
                (envelope.workspace_id, payload.trial_id),
            )
            trial = cur.fetchone()
            if trial is None or trial["campaign_id"] != payload.campaign_id:
                raise WorkStoreError(
                    "invalid_request", ("trial does not belong to campaign",)
                )

        computed_sha256 = sha256(payload.payload)
        if computed_sha256 != payload.payload_sha256:
            raise WorkStoreError(
                "stale_evidence", ("observation payload digest mismatch",)
            )

        cur.execute(
            f"""
            INSERT INTO omp_research.observations(
                observation_id, workspace_id, campaign_id, trial_id,
                issuer_kind, source_ref, execution_status, commit_sha,
                payload, payload_sha256, observed_at, recorded_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s, %s,
                %s, %s, %s, clock_timestamp()
            ) RETURNING {_OBSERVATION_FIELDS}
            """,
            (
                payload.observation_id,
                envelope.workspace_id,
                payload.campaign_id,
                payload.trial_id,
                issuer_kind_val,
                payload.source_ref,
                payload.execution_status.value
                if hasattr(payload.execution_status, "value")
                else str(payload.execution_status),
                payload.commit_sha,
                canonical_json(payload.payload),
                payload.payload_sha256,
                payload.observed_at,
            ),
        )
        return {
            "type": "record_research_observation",
            "status": "applied",
            "observation": _observation_json(cur.fetchone()),
        }

    def _bind_research_deliverable(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_DELIVERABLE_BINDING_FIELDS} FROM omp_research.deliverable_bindings WHERE workspace_id=%s AND trial_id=%s",
            (envelope.workspace_id, payload.trial_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            self._lock_work_chain(cur, envelope.workspace_id, existing["work_id"])
            if (
                existing["campaign_id"] == payload.campaign_id
                and existing["work_id"] == payload.work_id
                and existing["revision_id"] == payload.revision_id
                and existing["candidate_digest"] == payload.candidate_digest
                and existing["native_candidate_id"] == payload.native_candidate_id
                and existing["binding_sha256"] == payload.binding_sha256
            ):
                return {
                    "type": "bind_research_deliverable",
                    "status": "replayed",
                    "deliverable_binding": _deliverable_binding_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("trial already bound with different deliverable attributes",),
            )

        self._lock_work_chain(cur, envelope.workspace_id, payload.work_id)
        cur.execute(
            f"SELECT {_DELIVERABLE_BINDING_FIELDS} FROM omp_research.deliverable_bindings WHERE workspace_id=%s AND trial_id=%s",
            (envelope.workspace_id, payload.trial_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            if (
                existing["campaign_id"] == payload.campaign_id
                and existing["work_id"] == payload.work_id
                and existing["revision_id"] == payload.revision_id
                and existing["candidate_digest"] == payload.candidate_digest
                and existing["native_candidate_id"] == payload.native_candidate_id
                and existing["binding_sha256"] == payload.binding_sha256
            ):
                return {
                    "type": "bind_research_deliverable",
                    "status": "replayed",
                    "deliverable_binding": _deliverable_binding_json(existing),
                }
            raise WorkStoreError(
                "idempotency_conflict",
                ("trial already bound with different deliverable attributes",),
            )

        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("stale_evidence", ("campaign not found",))
        if campaign["work_id"] != payload.work_id or campaign["revision_id"] != payload.revision_id:
            raise WorkStoreError("stale_evidence", ("campaign work or revision mismatch",))

        cur.execute(
            f"SELECT {_TRIAL_FIELDS} FROM omp_research.trials WHERE workspace_id=%s AND trial_id=%s",
            (envelope.workspace_id, payload.trial_id),
        )
        trial = cur.fetchone()
        if trial is None or trial["campaign_id"] != payload.campaign_id or trial["work_id"] != payload.work_id:
            raise WorkStoreError("stale_evidence", ("trial does not match campaign or work",))
        if trial["state"] != "proposed":
            raise WorkStoreError("invalid_request", ("trial is not in proposed state",))
        if trial["candidate_digest"] != payload.candidate_digest:
            raise WorkStoreError("stale_evidence", ("trial candidate digest mismatch",))

        cur.execute(
            "SELECT work_id, revision_id, kind FROM omp_work.candidates WHERE workspace_id=%s AND candidate_id=%s",
            (envelope.workspace_id, payload.native_candidate_id),
        )
        candidate = cur.fetchone()
        if candidate is None or candidate["work_id"] != payload.work_id or candidate["revision_id"] != payload.revision_id:
            raise WorkStoreError("stale_evidence", ("native candidate work or revision mismatch",))
        if candidate["kind"] != "final":
            raise WorkStoreError("stale_evidence", ("native candidate must be finalized",))

        cur.execute(
            "SELECT current_candidate_id FROM omp_work.work_items WHERE workspace_id=%s AND work_id=%s",
            (envelope.workspace_id, payload.work_id),
        )
        item = cur.fetchone()
        if item is None or item["current_candidate_id"] != payload.native_candidate_id:
            raise WorkStoreError("stale_evidence", ("native candidate is not the current candidate on work item",))

        cur.execute(
            "SELECT candidate_id FROM omp_work.candidate_source_versions WHERE workspace_id=%s AND candidate_id=%s",
            (envelope.workspace_id, payload.native_candidate_id),
        )
        if cur.fetchone() is None:
            raise WorkStoreError("stale_evidence", ("native candidate has no source version association",))

        binding_identity = {
            "candidate_digest": payload.candidate_digest,
            "campaign_id": str(payload.campaign_id),
            "native_candidate_id": str(payload.native_candidate_id),
            "revision_id": str(payload.revision_id),
            "trial_id": str(payload.trial_id),
            "work_id": str(payload.work_id),
            "workspace_id": str(envelope.workspace_id),
        }
        if sha256(binding_identity) != payload.binding_sha256:
            raise WorkStoreError("stale_evidence", ("binding digest does not match identity fields",))

        cur.execute(
            f"""
            INSERT INTO omp_research.deliverable_bindings(
                trial_id, workspace_id, campaign_id, work_id, revision_id,
                candidate_digest, native_candidate_id, binding_sha256, bound_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, clock_timestamp()
            ) RETURNING {_DELIVERABLE_BINDING_FIELDS}
            """,
            (
                payload.trial_id,
                envelope.workspace_id,
                payload.campaign_id,
                payload.work_id,
                payload.revision_id,
                payload.candidate_digest,
                payload.native_candidate_id,
                payload.binding_sha256,
            ),
        )
        return {
            "type": "bind_research_deliverable",
            "status": "applied",
            "deliverable_binding": _deliverable_binding_json(cur.fetchone()),
        }

    def _set_research_campaign_state(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        self._lock_work_chain(cur, envelope.workspace_id, campaign["work_id"])
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        if campaign["work_id"] != payload.work_id:
            raise WorkStoreError("invalid_request", ("campaign work mismatch",))

        if campaign["state"] != payload.expected_state:
            raise WorkStoreError(
                "revision_conflict",
                (f"campaign state is {campaign['state']}, expected {payload.expected_state}",),
            )

        err = research_campaign_transition_error(campaign["state"], payload.target_state)
        if err:
            raise WorkStoreError("invalid_request", (err,))

        if payload.policy_sha256 != campaign["policy_sha256"]:
            raise WorkStoreError(
                "stale_evidence",
                ("policy fingerprint incompatible with admitted campaign",),
            )

        if campaign["state"] == "blocked" and payload.target_state != campaign["blocked_from_state"]:
            raise WorkStoreError(
                "invalid_request",
                (f"blocked campaign resumes only to {campaign['blocked_from_state']}",),
            )

        if payload.target_state == "blocked":
            blocked_dependency_json = canonical_json(
                payload.blocked_dependency.model_dump(mode="json")
            )
            cur.execute(
                f"""
                UPDATE omp_research.campaigns SET
                    state = %s,
                    blocked_dependency = %s,
                    blocked_from_state = %s
                WHERE workspace_id = %s AND campaign_id = %s
                RETURNING {_CAMPAIGN_FIELDS}
                """,
                (
                    payload.target_state,
                    blocked_dependency_json,
                    campaign["state"],
                    envelope.workspace_id,
                    payload.campaign_id,
                ),
            )
        else:
            cur.execute(
                f"""
                UPDATE omp_research.campaigns SET
                    state = %s,
                    blocked_dependency = NULL,
                    blocked_from_state = NULL
                WHERE workspace_id = %s AND campaign_id = %s
                RETURNING {_CAMPAIGN_FIELDS}
                """,
                (payload.target_state, envelope.workspace_id, payload.campaign_id),
            )
        updated = cur.fetchone()
        return {
            "type": "set_research_campaign_state",
            "status": "applied",
            "campaign": _campaign_json(updated),
        }

    def _conclude_research_campaign(
        self, cur: psycopg.Cursor[dict[str, object]], envelope: CommandEnvelope
    ) -> dict[str, object]:
        payload = envelope.command.payload
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        self._lock_work_chain(cur, envelope.workspace_id, campaign["work_id"])
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND campaign_id=%s",
            (envelope.workspace_id, payload.campaign_id),
        )
        campaign = cur.fetchone()
        if campaign is None:
            raise WorkStoreError("invalid_request", ("campaign not found",))

        if campaign["work_id"] != payload.work_id:
            raise WorkStoreError("invalid_request", ("campaign work mismatch",))

        if campaign["state"] not in {"evaluating", "blocked"}:
            err = research_campaign_transition_error(campaign["state"], "concluded")
            raise WorkStoreError(
                "invalid_request",
                (err or f"cannot conclude campaign in state {campaign['state']}",),
            )

        if campaign["state"] == "blocked" and payload.outcome not in {
            "resource_exhausted",
            "externally_blocked",
        }:
            raise WorkStoreError(
                "invalid_request",
                ("blocked campaign conclusion permitted only for resource_exhausted or externally_blocked",),
            )

        if payload.policy_sha256 != campaign["policy_sha256"]:
            raise WorkStoreError(
                "stale_evidence",
                ("policy fingerprint incompatible with admitted campaign",),
            )

        cur.execute(
            f"""
            UPDATE omp_research.campaigns SET
                state = 'concluded',
                outcome = %s,
                outcome_reason = %s,
                concluded_at = clock_timestamp(),
                blocked_dependency = NULL,
                blocked_from_state = NULL
            WHERE workspace_id = %s AND campaign_id = %s
            RETURNING {_CAMPAIGN_FIELDS}
            """,
            (
                payload.outcome,
                payload.reason,
                envelope.workspace_id,
                payload.campaign_id,
            ),
        )
        updated = cur.fetchone()
        return {
            "type": "conclude_research_campaign",
            "status": "applied",
            "campaign": _campaign_json(updated),
        }

    def _research_view(
        self, cur: psycopg.Cursor[dict[str, object]], workspace_id: UUID, work_id: UUID
    ) -> dict[str, object]:
        cur.execute(
            f"SELECT {_CAMPAIGN_FIELDS} FROM omp_research.campaigns WHERE workspace_id=%s AND work_id=%s ORDER BY created_at, campaign_id LIMIT 20",
            (workspace_id, work_id),
        )
        campaigns = [_campaign_json(dict(r)) for r in cur.fetchall()]
        cur.execute(
            f"SELECT {_TRIAL_FIELDS} FROM omp_research.trials WHERE workspace_id=%s AND work_id=%s ORDER BY proposed_at, trial_id LIMIT 200",
            (workspace_id, work_id),
        )
        trials = [_trial_json(dict(r)) for r in cur.fetchall()]
        cur.execute(
            f"SELECT {_OBSERVATION_FIELDS} FROM omp_research.observations WHERE workspace_id=%s AND campaign_id IN (SELECT campaign_id FROM omp_research.campaigns WHERE workspace_id=%s AND work_id=%s) ORDER BY observed_at DESC, observation_id LIMIT 200",
            (workspace_id, workspace_id, work_id),
        )
        observations = [_observation_json(dict(r)) for r in cur.fetchall()]
        cur.execute(
            f"SELECT {_DELIVERABLE_BINDING_FIELDS} FROM omp_research.deliverable_bindings WHERE workspace_id=%s AND work_id=%s ORDER BY bound_at, trial_id LIMIT 200",
            (workspace_id, work_id),
        )
        deliverable_bindings = [
            _deliverable_binding_json(dict(r)) for r in cur.fetchall()
        ]
        comp_hashes: set[str] = set()
        for c in campaigns:
            if c.get("policy_sha256"):
                comp_hashes.add(c["policy_sha256"])
            compat = c.get("compatibility")
            if isinstance(compat, dict):
                for k in ("workers", "evaluators", "audits", "releases", "environments"):
                    for h in compat.get(k, ()):
                        comp_hashes.add(h)
        if comp_hashes:
            cur.execute(
                f"SELECT {_COMPONENT_FIELDS} FROM omp_research.components WHERE workspace_id=%s AND component_sha256 = ANY(%s) ORDER BY component_sha256",
                (workspace_id, list(comp_hashes)),
            )
            components = [_component_json(dict(r)) for r in cur.fetchall()]
        else:
            components = []

        return {
            "work_id": work_id,
            "campaigns": campaigns,
            "trials": trials,
            "observations": observations,
            "deliverable_bindings": deliverable_bindings,
            "components": components,
        }
