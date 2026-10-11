"""Research reads use the native route, typed projection and shared refusals."""

from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from omp_work import contract_sha256
from omp_work.v1.api_models import ResearchView
from omp_work.v1.client import WorkClient
from omp_work.v1.service import WorkError

WORKSPACE = UUID(int=1)
WORK = UUID(int=2)


def client(
    tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]
) -> WorkClient:
    bearer = tmp_path / "bearer.json"
    bearer.write_text('{"token":"research-reader"}')
    bearer.chmod(0o600)
    return WorkClient(
        "https://work.test", WORKSPACE, bearer,
        transport=httpx.MockTransport(handler),
    )


def test_research_read_uses_authenticated_route_and_decodes_uuid(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/work-items/OMP-588/research"
        assert not request.url.query
        assert request.headers["Authorization"] == "Bearer research-reader"
        assert request.headers["X-OMP-Workspace-ID"] == str(WORKSPACE)
        assert request.headers["X-OMP-Contract-SHA256"] == contract_sha256()
        return httpx.Response(200, json={"work_id": str(WORK)})

    reader = client(tmp_path, handle)
    try:
        view = reader.research("OMP-588")
        assert isinstance(view, ResearchView)
        assert view.work_id == WORK
    finally:
        reader.close()


@pytest.mark.parametrize(
    "status,code,diagnostics",
    [(403, "forbidden", []), (409, "contract_mismatch", ["restart required"])],
)
def test_research_refusal_preserves_work_error(
    tmp_path: Path, status: int, code: str, diagnostics: list[str]
) -> None:
    reader = client(tmp_path, lambda _: httpx.Response(
        status, json={"error": {"code": code, "diagnostics": diagnostics}},
    ))
    try:
        with pytest.raises(WorkError) as caught:
            reader.research("OMP-588")
        assert (caught.value.code, caught.value.status, caught.value.diagnostics) == (
            code, status, tuple(diagnostics),
        )
    finally:
        reader.close()


def test_research_rejects_malformed_success_projection(tmp_path: Path) -> None:
    reader = client(tmp_path, lambda _: httpx.Response(200, json={"work_id": "bad"}))
    try:
        with pytest.raises(ValidationError):
            reader.research("OMP-588")
    finally:
        reader.close()
