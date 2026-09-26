"""Async client for Jev typed decisions API (POST /v1/systemone)."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import SecretStr

if TYPE_CHECKING:
    from robomp.db import Database

log = logging.getLogger(__name__)

_STATE_CHAR_CAP = 8000
_BUDGET_SECONDS = 3.0
_BREAKER_FAILURES = 2
_BREAKER_COOLDOWN_SECONDS = 60.0


@dataclass(slots=True, frozen=True)
class JevResponse:
    request_id: str
    server_id: str | None
    answers: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def truncate_state(state: str, cap: int = _STATE_CHAR_CAP) -> tuple[str, int, bool]:
    """Truncate state to `cap` chars with ellipsis marker if needed.

    Returns (payload_state, original_char_count, is_truncated).
    """
    state_chars = len(state)
    if state_chars > cap:
        truncated_count = state_chars - cap
        marker = f"…[truncated {truncated_count} chars]"
        return state[:cap] + marker, state_chars, True
    return state, state_chars, False


def extract_choice_probabilities(ans: Any) -> dict[str, float]:
    """Extract probability map from choice question answer."""
    if isinstance(ans, dict):
        if "probabilities" in ans and isinstance(ans["probabilities"], dict):
            return {str(k): float(v) for k, v in ans["probabilities"].items()}
        return {str(k): float(v) for k, v in ans.items() if isinstance(v, (int, float))}
    return {}


def extract_bool_probability(ans: Any) -> float:
    """Extract yes/true probability from bool/noul question answer."""
    if isinstance(ans, (int, float)):
        return float(ans)
    if isinstance(ans, dict):
        if "probability" in ans and isinstance(ans["probability"], (int, float)):
            return float(ans["probability"])
        if "probabilities" in ans and isinstance(ans["probabilities"], dict):
            probs = ans["probabilities"]
            for key in ("yes", "true", "True", "1"):
                if key in probs and isinstance(probs[key], (int, float)):
                    return float(probs[key])
        for key in ("yes", "true", "True"):
            if key in ans and isinstance(ans[key], (int, float)):
                return float(ans[key])
    return 0.0


class JevClient:
    """Client for Jev /v1/systemone typed classification endpoint."""

    def __init__(
        self,
        base_url: str = "https://api.typesafe.ai",
        api_key: str | SecretStr | None = None,
        *,
        db: Database | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        budget_seconds: float = _BUDGET_SECONDS,
        breaker_failure_threshold: int = _BREAKER_FAILURES,
        breaker_cooldown_seconds: float = _BREAKER_COOLDOWN_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if hasattr(api_key, "get_secret_value"):
            self.api_key: str | None = api_key.get_secret_value()  # type: ignore[union-attr]
        else:
            self.api_key = api_key
        self.db = db
        self.transport = transport
        self.budget_seconds = budget_seconds
        self.breaker_failure_threshold = breaker_failure_threshold
        self.breaker_cooldown_seconds = breaker_cooldown_seconds

        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def is_breaker_open(self) -> bool:
        if self._breaker_open_until > 0.0:
            if time.monotonic() < self._breaker_open_until:
                return True
            self._breaker_open_until = 0.0
            return False
        return False

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.breaker_failure_threshold:
            self._breaker_open_until = time.monotonic() + self.breaker_cooldown_seconds

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    async def decide(
        self,
        state: str,
        questions: Mapping[str, Any],
        *,
        feature: str = "robomp_prefilter",
        db: Database | None = None,
    ) -> JevResponse | None:
        """Call POST /v1/systemone with whole-call timeout budget and retry.

        Returns JevResponse on success, or None on any failure (after logging one warning).
        """
        if self.is_breaker_open:
            return None

        target_db = db or self.db
        state_payload, state_chars, truncated = truncate_state(state)

        payload = {
            "model": "jev-latest",
            "state": state_payload,
            "questions": questions,
        }

        url = f"{self.base_url}/v1/systemone"
        base_headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            base_headers["Authorization"] = f"Bearer {self.api_key}"

        start_time = time.monotonic()
        last_warning: str | None = None
        attempt = 1

        async with httpx.AsyncClient(transport=self.transport) as http_client:
            while attempt <= 2:
                elapsed = time.monotonic() - start_time
                remaining_budget = self.budget_seconds - elapsed
                if remaining_budget <= 0.0:
                    last_warning = f"Jev call exceeded budget of {self.budget_seconds}s"
                    break

                request_id = str(uuid.uuid4())
                headers = dict(base_headers)
                headers["X-Request-Id"] = request_id

                attempt_start = time.monotonic()
                try:
                    resp = await http_client.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=remaining_budget,
                    )
                    latency_ms = int(round((time.monotonic() - attempt_start) * 1000))
                except httpx.TimeoutException as exc:
                    latency_ms = int(round((time.monotonic() - attempt_start) * 1000))
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome="timeout",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev call timed out on attempt {attempt}: {exc}"
                    # No retry after timeout
                    break
                except (httpx.NetworkError, httpx.ConnectError) as exc:
                    latency_ms = int(round((time.monotonic() - attempt_start) * 1000))
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome="network_error",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev call network error on attempt {attempt}: {exc}"
                    if attempt == 1 and (self.budget_seconds - (time.monotonic() - start_time)) > 0:
                        attempt += 1
                        continue
                    break
                except Exception as exc:
                    latency_ms = int(round((time.monotonic() - attempt_start) * 1000))
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome="error",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev call error on attempt {attempt}: {exc}"
                    break

                # 5xx responses: retry once if budget permits
                if 500 <= resp.status_code < 600:
                    outcome = "500"
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome=outcome,
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev call received HTTP {resp.status_code} on attempt {attempt}"
                    if attempt == 1 and (self.budget_seconds - (time.monotonic() - start_time)) > 0:
                        attempt += 1
                        continue
                    break

                if resp.status_code != 200:
                    outcome = str(resp.status_code)
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome=outcome,
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev call received HTTP {resp.status_code}"
                    break

                # Parse JSON
                try:
                    data = resp.json()
                except Exception as exc:
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome="malformed",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev response malformed JSON: {exc}"
                    break

                if not isinstance(data, dict):
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=None,
                            feature=feature,
                            outcome="malformed",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = "Jev response is not a JSON object"
                    break

                server_id = data.get("id")
                answers = data.get("answers")
                if not isinstance(answers, dict):
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=str(server_id) if server_id is not None else None,
                            feature=feature,
                            outcome="malformed",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = "Jev response missing 'answers' object"
                    break

                # Check for off-list choice answers
                is_off_list = False
                for q_name, q_val in questions.items():
                    if isinstance(q_val, dict) and q_val.get("type") == "choice":
                        allowed = set(q_val.get("options") or [])
                        ans = answers.get(q_name)
                        probs = extract_choice_probabilities(ans)
                        for opt in probs:
                            if opt not in allowed:
                                is_off_list = True
                                break
                    if is_off_list:
                        break

                if is_off_list:
                    if target_db is not None:
                        target_db.record_jev_call(
                            request_id=request_id,
                            server_id=str(server_id) if server_id is not None else None,
                            feature=feature,
                            outcome="off_list",
                            latency_ms=latency_ms,
                            state_chars=state_chars,
                            truncated=truncated,
                            attempt=attempt,
                        )
                    last_warning = f"Jev response contained off-list answer for questions: {answers}"
                    break

                # Success
                if target_db is not None:
                    target_db.record_jev_call(
                        request_id=request_id,
                        server_id=str(server_id) if server_id is not None else None,
                        feature=feature,
                        outcome="ok",
                        latency_ms=latency_ms,
                        state_chars=state_chars,
                        truncated=truncated,
                        attempt=attempt,
                    )
                self.record_success()
                return JevResponse(
                    request_id=request_id,
                    server_id=str(server_id) if server_id is not None else None,
                    answers=answers,
                )

        # Call failed
        self.record_failure()
        log.warning(last_warning or "Jev call failed")
        return None

    async def choice(
        self,
        question: str,
        options: Sequence[str],
        state: str,
        *,
        feature: str = "robomp_prefilter",
        db: Database | None = None,
    ) -> dict[str, float] | None:
        """Convenience wrapper for a single choice question."""
        questions = {
            "choice": {
                "type": "choice",
                "instructions": question,
                "options": list(options),
            }
        }
        resp = await self.decide(state, questions, feature=feature, db=db)
        if resp is None:
            return None
        return extract_choice_probabilities(resp.answers.get("choice"))

    async def yesno(
        self,
        statement: str,
        state: str,
        *,
        feature: str = "robomp_prefilter",
        db: Database | None = None,
    ) -> float | None:
        """Convenience wrapper for a single yes/no (noul) question."""
        questions = {
            "statement": {
                "type": "noul",
                "instructions": statement,
            }
        }
        resp = await self.decide(state, questions, feature=feature, db=db)
        if resp is None:
            return None
        return extract_bool_probability(resp.answers.get("statement"))
