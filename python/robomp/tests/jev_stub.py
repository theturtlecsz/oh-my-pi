"""Stub Jev systemone server for tests."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx

_DEFAULT_PRIMARY_PROBS = {
    "bug": 0.80,
    "enhancement": 0.05,
    "question": 0.05,
    "proposal": 0.02,
    "documentation": 0.03,
    "wontfix": 0.01,
    "invalid": 0.02,
    "duplicate": 0.02,
}


class JevStub:
    """Mock transport handler for Jev systemone API."""

    def __init__(
        self,
        mode: str = "ok",
        *,
        sequence: Sequence[str] | None = None,
        answers: dict[str, Any] | None = None,
        primary_probs: dict[str, float] | None = None,
        batch_audit_prob: float = 0.0,
        server_id: str = "stub-jev-srv-1",
    ) -> None:
        self.mode = mode
        self.sequence = list(sequence) if sequence is not None else None
        self.answers = answers
        self.primary_probs = primary_probs or _DEFAULT_PRIMARY_PROBS
        self.batch_audit_prob = batch_audit_prob
        self.server_id = server_id

        self.requests: list[httpx.Request] = []
        self.payloads: list[dict[str, Any]] = []

    def _get_current_mode(self) -> str:
        if self.sequence:
            return self.sequence.pop(0)
        return self.mode

    def _build_ok_response(self) -> dict[str, Any]:
        if self.answers is not None:
            return {"id": self.server_id, "answers": self.answers}

        return {
            "id": self.server_id,
            "answers": {
                "primary_type": {
                    "probabilities": self.primary_probs,
                },
                "batch_audit": {
                    "probability": self.batch_audit_prob,
                },
                "first_person_failure": {
                    "probability": 0.0,
                },
                "wanted_different_behavior": {
                    "probability": 0.0,
                },
                "upstream_cause": {
                    "probability": 0.0,
                },
                "nondefault_exotic_env": {
                    "probability": 0.0,
                },
            },
        }

    async def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            body = json.loads(request.content.decode("utf-8"))
            self.payloads.append(body)
        except Exception:
            self.payloads.append({})

        mode = self._get_current_mode()

        if mode in ("delay", "timeout"):
            raise httpx.ReadTimeout("Read timed out", request=request)

        if mode == "500":
            return httpx.Response(500, text="Internal Server Error", request=request)

        if mode == "malformed":
            return httpx.Response(200, content=b"{malformed json", request=request)

        if mode == "missing-answers":
            return httpx.Response(200, json={"id": self.server_id}, request=request)

        if mode == "off-list":
            return httpx.Response(
                200,
                json={
                    "id": self.server_id,
                    "answers": {
                        "primary_type": {
                            "probabilities": {
                                "off_list_unknown_type": 0.99,
                            }
                        }
                    },
                },
                request=request,
            )

        data = self._build_ok_response()
        return httpx.Response(200, json=data, request=request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_request)
