from __future__ import annotations

import json
import stat
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from omp_work import contract_sha256

from .api_models import (
    CommandResponse,
    DecisionsPage,
    DomainEventsPage,
    EventSubscriptionsPage,
    MissionEventsPage,
    MissionView,
    StopStatusView,
    StoredOperationView,
    WorkflowView,
    WorkItemsPage,
    WorkItemView,
    WorkspaceTree,
)
from .models import (
    AnswerMissionDraftCommand,
    AnswerMissionDraftPayload,
    Command,
    CommandEnvelope,
    DraftMissionIntakeCommand,
    DraftMissionIntakePayload,
    EvidenceReceipt,
    FocusSlot,
    WorkRevision,
)
from .service import WorkError


def _envelope(workspace_id: UUID, command: Command) -> CommandEnvelope:
    """One command envelope with fresh operation, request, and correlation ids."""
    return CommandEnvelope(
        api_version="work.omp.dev/v1",
        workspace_id=workspace_id,
        operation_id=uuid4(),
        request_id=uuid4(),
        correlation_id=uuid4(),
        command=command,
    )


class WorkClient:
    def __init__(
        self,
        base_url: str,
        workspace_id: UUID,
        bearer_file: Path,
        *,
        timeout: float = 10,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if stat.S_IMODE(bearer_file.stat().st_mode) != 0o600:
            raise ValueError("unsafe bearer file permissions")
        self._workspace_id = workspace_id
        self._token = json.loads(bearer_file.read_text())["token"]
        # OMP-143: the digest this process loaded, captured once — a stale
        # process keeps its stale digest and gets the typed restart refusal.
        self._contract_sha256 = contract_sha256()
        self._client = httpx.Client(
            base_url=base_url, timeout=timeout, transport=transport
        )


    def execute(self, envelope: CommandEnvelope) -> CommandResponse:
        response = self._client.post(
            "/v1/commands",
            headers=self._headers(),
            json=envelope.model_dump(mode="json"),
        )
        if response.is_error:
            self._raise(response)
        return CommandResponse.model_validate(response.json())

    def work_item(self, key: str) -> WorkItemView:
        return WorkItemView.model_validate(self._get(f"/v1/work-items/{key}"))

    def workflow(self, key: str) -> WorkflowView:
        return WorkflowView.model_validate(self._get(f"/v1/work-items/{key}/workflow"))

    def tree(self) -> WorkspaceTree:
        return WorkspaceTree.model_validate(
            self._get(f"/v1/workspaces/{self._workspace_id}/tree")
        )

    def stop_status(self) -> StopStatusView:
        """OMP-405: the workspace's stop state, derived from the latest applied
        engage_stop/release_stop domain event."""
        return StopStatusView.model_validate(
            self._get(f"/v1/workspaces/{self._workspace_id}/stop")
        )

    def focus(self, owner_id: UUID) -> FocusSlot:
        return FocusSlot.model_validate(
            self._get(f"/v1/workspaces/{self._workspace_id}/focus/{owner_id}")
        )

    def operation(self, operation_id: UUID) -> StoredOperationView:
        return StoredOperationView.model_validate(
            self._get(f"/v1/operations/{operation_id}")
        )

    def revision(self, key: str, selector: str | int | UUID) -> WorkRevision:
        return WorkRevision.model_validate(
            self._get(f"/v1/work-items/{key}/revisions/{selector}")
        )

    def receipt(self, receipt_id: UUID | str) -> EvidenceReceipt:
        return EvidenceReceipt.model_validate(
            self._get(f"/v1/receipts/{receipt_id}")
        )

    def work_items(
        self,
        *,
        after_created_at: datetime | None = None,
        after_work_id: UUID | None = None,
        after: tuple[datetime, UUID] | None = None,
        limit: int | None = None,
    ) -> WorkItemsPage:
        params: dict[str, object] = {}
        if after is not None:
            after_created_at, after_work_id = after
        if after_created_at is not None:
            params["after_created_at"] = after_created_at.isoformat()
        if after_work_id is not None:
            params["after_work_id"] = str(after_work_id)
        if limit is not None:
            params["limit"] = limit
        return WorkItemsPage.model_validate(
            self._get(
                f"/v1/workspaces/{self._workspace_id}/work-items",
                params=params,
            )
        )

    def events(
        self,
        after_sequence: int = 0,
        limit: int = 500,
        *,
        after: int | None = None,
    ) -> DomainEventsPage:
        effective_after = after if after is not None else after_sequence
        params: dict[str, object] = {
            "after_sequence": effective_after,
            "limit": limit,
        }
        return DomainEventsPage.model_validate(
            self._get(
                f"/v1/workspaces/{self._workspace_id}/events",
                params=params,
            )
        )

    def mission_events(
        self,
        after_sequence: int = 0,
        limit: int = 500,
        mission_id: UUID | None = None,
    ) -> MissionEventsPage:
        params: dict[str, object] = {"after_sequence": after_sequence, "limit": limit}
        if mission_id is not None:
            params["mission_id"] = str(mission_id)
        return MissionEventsPage.model_validate(
            self._get(
                f"/v1/workspaces/{self._workspace_id}/mission-events",
                params=params,
            )
        )

    def event_subscriptions(
        self, client_id: UUID | None = None
    ) -> EventSubscriptionsPage:
        params: dict[str, object] = {}
        if client_id is not None:
            params["client_id"] = str(client_id)
        return EventSubscriptionsPage.model_validate(
            self._get(
                f"/v1/workspaces/{self._workspace_id}/event-subscriptions",
                params=params,
            )
        )

    def draft_mission_intake(
        self, payload: DraftMissionIntakePayload
    ) -> CommandResponse:
        """Draft one mission intake and route it to proceed, hold, or owner decision."""
        return self.execute(
            _envelope(
                self._workspace_id,
                DraftMissionIntakeCommand(type="draft_mission_intake", payload=payload),
            )
        )

    def answer_mission_draft(
        self, payload: AnswerMissionDraftPayload
    ) -> CommandResponse:
        """Apply the owner's structured answer to one pending mission draft."""
        return self.execute(
            _envelope(
                self._workspace_id,
                AnswerMissionDraftCommand(type="answer_mission_draft", payload=payload),
            )
        )

    def decisions(
        self,
        *,
        status: str | None = None,
        mission_id: str | None = None,
    ) -> DecisionsPage:
        params: dict[str, object] = {}
        if status is not None:
            params["status"] = status
        if mission_id is not None:
            params["mission_id"] = mission_id
        return DecisionsPage.model_validate(
            self._get(
                f"/v1/workspaces/{self._workspace_id}/decisions",
                params=params,
            )
        )

    def mission(self, mission_id: UUID) -> MissionView:
        return MissionView.model_validate(
            self._get(f"/v1/workspaces/{self._workspace_id}/missions/{mission_id}")
        )

    def _get(
        self, path: str, *, params: dict[str, object] | None = None
    ) -> dict[str, object]:
        response = self._client.get(path, headers=self._headers(), params=params)
        if response.is_error:
            self._raise(response)
        return dict(response.json())


    def close(self) -> None:
        self._client.close()

    def _headers(self) -> dict[str, str]:
        # OMP-143 fail-first handshake: the digest this process loaded; the
        # service refuses any mismatch with typed `contract_mismatch`.
        return {
            "Authorization": f"Bearer {self._token}",
            "X-OMP-Workspace-ID": str(self._workspace_id),
            "X-OMP-Contract-SHA256": self._contract_sha256,
        }

    @staticmethod
    def _raise(response: httpx.Response) -> None:
        error = response.json().get("error", {})
        raise WorkError(
            str(error.get("code", "invalid_request")),
            status=response.status_code,
            diagnostics=tuple(error.get("diagnostics", [])),
        )
