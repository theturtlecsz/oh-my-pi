"""Stub Jev systemone server for the R2 claim-support classifier tests.

Mirrors ``python/robomp/tests/jev_stub.py``: an ``httpx.MockTransport`` that
answers the typed decision endpoint without reaching the real service. Modes
cover the success path and the refusals the classifier must map to
:class:`ClassifierAnswerError`: ``off-list`` (an option outside the requested
``{supports, contradicts, neither}`` set), ``malformed`` (a non-JSON body),
``missing-answer`` (a pair answer omitted), ``500`` and ``network``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

LABELS = ("supports", "contradicts", "neither")


def probabilities(label: str, *, confidence: float = 0.9) -> dict[str, float]:
    """A probability map over ``LABELS`` peaking at ``label``."""
    remainder = round((1.0 - confidence) / 2, 4)
    return {
        option: (confidence if option == label else remainder) for option in LABELS
    }


class JevStub:
    """Mock transport handler for the Jev systemone API."""

    def __init__(
        self,
        *,
        mode: str = "ok",
        labels: list[str] | None = None,
        server_id: str = "stub-jev-srv-1",
        answers: dict[str, Any] | None = None,
    ) -> None:
        self.mode = mode
        self.labels = labels
        self.server_id = server_id
        self.answers = answers

        self.payloads: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []
        self._label_offset = 0

    def _ok_body(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.answers is not None:
            return {"id": self.server_id, "answers": self.answers}
        names = list((body.get("questions") or {}).keys())
        answers: dict[str, Any] = {}
        for index, name in enumerate(names):
            label_idx = self._label_offset + index
            label = (
                self.labels[label_idx]
                if self.labels and label_idx < len(self.labels)
                else "neither"
            )
            answers[name] = {"probabilities": probabilities(label)}
        self._label_offset += len(names)
        return {"id": self.server_id, "answers": answers}

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        try:
            body = json.loads(request.content.decode("utf-8"))
        except Exception:
            body = {}
        self.payloads.append(body)

        if self.mode in ("network", "timeout"):
            raise httpx.ConnectError("connection refused", request=request)
        if self.mode == "500":
            return httpx.Response(500, text="Internal Server Error", request=request)
        if self.mode == "malformed":
            return httpx.Response(200, content=b"{malformed json", request=request)
        if self.mode == "missing-answer":
            names = list((body.get("questions") or {}).keys())
            answers = {
                name: {"probabilities": probabilities("neither")} for name in names[1:]
            }
            return httpx.Response(
                200, json={"id": self.server_id, "answers": answers}, request=request
            )
        if self.mode == "off-list":
            names = list((body.get("questions") or {}).keys())
            answers = {
                name: {"probabilities": {"off_list_label": 0.99}} for name in names
            }
            return httpx.Response(
                200, json={"id": self.server_id, "answers": answers}, request=request
            )

        return httpx.Response(200, json=self._ok_body(body), request=request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_request)
