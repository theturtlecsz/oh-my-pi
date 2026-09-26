"""Token counter client and recorded reproduction counter (OMP-311 / FK-5).

- :class:`OmpTokenCounter` executes an external CLI counter adhering to the
  omp tokens count protocol (first stdout line is the profile JSON, followed
  by one token count per item id).
- :class:`RecordedCounter` replays token counts from an immutable dictionary
  keyed by the SHA-256 of the input text, raising :class:`ReproductionError`
  for unrecorded texts.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 - runs the operator-configured token counter CLI
from collections.abc import Sequence
from typing import Any

from omp_work.v1.canonical import canonical_json, sha256


class TokenCounterUnavailable(Exception):
    """Raised when an external token counter fails, times out, or produces invalid output."""


class ReproductionError(Exception):
    """Raised when bundle reproduction fails or an unrecorded text is queried."""


class OmpTokenCounter:
    """Subprocess token counter implementing the s01 TokenCounter protocol.

    Each call to :meth:`count` invokes ``argv + ["count", "--encoding", encoding]``
    with JSONL input on stdin (``{"id": str, "text": str}``). The first stdout line
    must be a profile JSON object with ``api == "countTokens"`` and the matching
    encoding. Each subsequent line must provide the token count for one input id.
    """

    def __init__(
        self,
        argv: Sequence[str] | list[str],
        encoding: str,
        timeout_s: float = 30.0,
    ) -> None:
        self.argv = list(argv)
        self.encoding = encoding
        self.timeout_s = timeout_s
        self._profile = canonical_json({"api": "countTokens", "encoding": encoding})

    @property
    def profile(self) -> str:
        return self._profile

    def count(self, texts: Sequence[str]) -> list[int]:
        if not texts:
            return []

        cmd = self.argv + ["count", "--encoding", self.encoding]
        stdin_lines = [
            canonical_json({"id": str(i), "text": text})
            for i, text in enumerate(texts)
        ]
        stdin_data = "\n".join(stdin_lines) + "\n"

        try:
            proc = subprocess.run(  # nosec B603 - cmd is fixed argv from --token-cmd, no shell
                cmd,
                input=stdin_data,
                text=True,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TokenCounterUnavailable(
                f"token counter timed out after {self.timeout_s}s"
            ) from exc
        except OSError as exc:
            raise TokenCounterUnavailable(
                f"failed to spawn token counter {cmd}: {exc}"
            ) from exc

        if proc.returncode != 0:
            raise TokenCounterUnavailable(
                f"token counter exited with code {proc.returncode}: {proc.stderr.strip()}"
            )

        raw_lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not raw_lines:
            raise TokenCounterUnavailable("token counter produced empty stdout")

        first_line = raw_lines[0]
        try:
            profile_data = json.loads(first_line)
        except Exception as exc:
            raise TokenCounterUnavailable(
                f"bad profile JSON from token counter: {first_line}"
            ) from exc

        if not isinstance(profile_data, dict):
            raise TokenCounterUnavailable(
                f"bad profile: expected JSON object, got {type(profile_data).__name__}"
            )
        if profile_data.get("api") != "countTokens":
            raise TokenCounterUnavailable(
                f"bad profile api: expected 'countTokens', got {profile_data.get('api')!r}"
            )
        if profile_data.get("encoding") != self.encoding:
            raise TokenCounterUnavailable(
                f"wrong-encoding profile: expected {self.encoding!r}, got {profile_data.get('encoding')!r}"
            )

        self._profile = canonical_json(profile_data)

        expected_ids = {str(i) for i in range(len(texts))}
        seen_ids: set[str] = set()
        results: dict[str, int] = {}

        for line_num, line in enumerate(raw_lines[1:], start=2):
            try:
                item_data = json.loads(line)
            except Exception as exc:
                raise TokenCounterUnavailable(
                    f"malformed JSON on line {line_num}: {line}"
                ) from exc

            if not isinstance(item_data, dict):
                raise TokenCounterUnavailable(
                    f"invalid count format on line {line_num}: expected JSON object"
                )
            if "id" not in item_data or not isinstance(item_data["id"], str):
                raise TokenCounterUnavailable(
                    f"missing or non-string 'id' on line {line_num}: {line}"
                )

            item_id = item_data["id"]
            if item_id in seen_ids:
                raise TokenCounterUnavailable(
                    f"duplicate id {item_id!r} on line {line_num}"
                )
            if item_id not in expected_ids:
                raise TokenCounterUnavailable(
                    f"extra id {item_id!r} on line {line_num}"
                )

            count_val: Any = None
            if "tokens" in item_data and isinstance(item_data["tokens"], int) and not isinstance(item_data["tokens"], bool):
                count_val = item_data["tokens"]
            elif "count" in item_data and isinstance(item_data["count"], int) and not isinstance(item_data["count"], bool):
                count_val = item_data["count"]
            else:
                raise TokenCounterUnavailable(
                    f"missing integer count on line {line_num}: {line}"
                )

            seen_ids.add(item_id)
            results[item_id] = count_val

        if seen_ids != expected_ids:
            missing = expected_ids - seen_ids
            raise TokenCounterUnavailable(f"missing ids from counter: {sorted(missing)}")

        return [results[str(i)] for i in range(len(texts))]


class RecordedCounter:
    """Token counter that replays recorded counts keyed by text SHA-256."""

    def __init__(self, profile: str, counts: dict[str, int]) -> None:
        self.profile = profile
        self.counts = dict(counts)

    def count(self, texts: Sequence[str]) -> list[int]:
        results: list[int] = []
        for text in texts:
            key = sha256(text)
            if key not in self.counts:
                raise ReproductionError(f"unrecorded text with sha256 {key}")
            results.append(self.counts[key])
        return results


__all__ = [
    "OmpTokenCounter",
    "RecordedCounter",
    "ReproductionError",
    "TokenCounterUnavailable",
]
