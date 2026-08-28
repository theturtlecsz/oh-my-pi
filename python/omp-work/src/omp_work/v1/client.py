from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import UUID

import httpx

from omp_work import contract_sha256

from .api_models import (
    CommandResponse,
    StoredOperationView,
    WorkflowView,
    WorkItemView,
    WorkspaceTree,
)
from .models import CommandEnvelope, FocusSlot
from .service import WorkError


class WorkClient:
    def __init__(
        self,
        base_url: str,
        workspace_id: UUID,
        bearer_file: Path,
        *,
        timeout: float = 10,
    ) -> None:
        if stat.S_IMODE(bearer_file.stat().st_mode) != 0o600:
            raise ValueError("unsafe bearer file permissions")
        self._workspace_id = workspace_id
        self._token = json.loads(bearer_file.read_text())["token"]
        # OMP-143: the digest this process loaded, captured once — a stale
        # process keeps its stale digest and gets the typed restart refusal.
        self._contract_sha256 = contract_sha256()
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

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

    def focus(self, owner_id: UUID) -> FocusSlot:
        return FocusSlot.model_validate(
            self._get(f"/v1/workspaces/{self._workspace_id}/focus/{owner_id}")
        )

    def operation(self, operation_id: UUID) -> StoredOperationView:
        return StoredOperationView.model_validate(
            self._get(f"/v1/operations/{operation_id}")
        )

    def _get(self, path: str) -> dict[str, object]:
        response = self._client.get(path, headers=self._headers())
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
