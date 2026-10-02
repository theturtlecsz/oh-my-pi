"""GitHub pull requests for one repository (OMP-518-s02).

Every call reads its token file and performs HTTP only through
``push_http.request_json``. ``merge`` uses the token path given to that call.
The other methods use the path given to ``GitHubPulls``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from omp_work.push_http import PushDeliveryError, request_json

__all__ = [
    "GitHubPulls",
    "PullRequest",
    "PullRequestError",
]


class PullRequestError(RuntimeError):
    """A pull-request call failed. ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PullRequest:
    """One pull request as the orchestrator needs to see it."""

    number: int
    state: str
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    merge_commit_sha: str | None


def _quote_query(
    value: str,
    safe: str = "",
    encoding: str | None = None,
    errors: str | None = None,
) -> str:
    """Encode a query value and leave the owner:head colon readable."""
    del encoding, errors
    return quote(str(value), safe=f"{safe}:")


def _status_code(status: int | None) -> str:
    """Map an HTTP failure to a pull-request error code."""
    if status is None or status >= 500:
        return "outcome_unknown"
    if status == 404:
        return "not_found"
    if status in (405, 409):
        return "merge_refused"
    if status == 422:
        return "unprocessable"
    if 400 <= status < 500:
        return "github_refused"
    return "outcome_unknown"


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PullRequestError("outcome_unknown")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise PullRequestError("outcome_unknown")
    return value


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PullRequestError("outcome_unknown")
    return value


def _pull(data: Any) -> PullRequest:
    """Parse one GitHub pull object. A shape we cannot use is outcome_unknown."""
    body = _mapping(data)
    head = _mapping(body.get("head"))
    base = _mapping(body.get("base"))
    merged = body.get("merged")
    if not isinstance(merged, bool):
        raise PullRequestError("outcome_unknown")
    merge_commit_sha = body.get("merge_commit_sha")
    if merge_commit_sha is not None:
        merge_commit_sha = _text(merge_commit_sha)
    return PullRequest(
        number=_integer(body.get("number")),
        state=_text(body.get("state")),
        merged=merged,
        head_ref=_text(head.get("ref")),
        head_sha=_text(head.get("sha")),
        base_ref=_text(base.get("ref")),
        merge_commit_sha=merge_commit_sha,
    )


class GitHubPulls:
    """Pull requests, check conclusions, and merges for ``repository``."""

    def __init__(
        self,
        api_url: str,
        repository: str,
        token_path: str | Path,
        timeout: float = 10,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.repository = repository
        self.token_path = Path(token_path)
        self.timeout = timeout

    def find(self, head: str) -> int | None:
        """Highest pull number for ``head`` on any base, or None when there is none."""
        owner, _sep, _name = self.repository.partition("/")
        query = urlencode(
            (("state", "all"), ("head", f"{owner}:{head}")),
            quote_via=_quote_query,
        )
        payload = self._request("GET", f"{self._root()}/pulls?{query}", self.token_path)
        if not isinstance(payload, list):
            raise PullRequestError("outcome_unknown")
        best: int | None = None
        for item in payload:
            number = _integer(_mapping(item).get("number"))
            if best is None or number > best:
                best = number
        return best

    def create(self, head: str, base: str, title: str, body: str) -> PullRequest:
        """Open a pull and return it."""
        payload = self._request(
            "POST",
            f"{self._root()}/pulls",
            self.token_path,
            {"title": title, "head": head, "base": base, "body": body},
        )
        return _pull(payload)

    def read(self, number: int) -> PullRequest:
        """Return pull ``number``."""
        payload = self._request(
            "GET", f"{self._root()}/pulls/{number}", self.token_path
        )
        return _pull(payload)

    def check_conclusions(self, sha: str) -> dict[str, str | None]:
        """Per check name, the conclusion of the run with the highest id.

        A run whose status is not ``completed`` contributes None.
        """
        quoted = quote(sha, safe="")
        payload = self._request(
            "GET",
            f"{self._root()}/commits/{quoted}/check-runs?per_page=100",
            self.token_path,
        )
        runs = _mapping(payload).get("check_runs")
        if not isinstance(runs, list):
            raise PullRequestError("outcome_unknown")
        chosen: dict[str, tuple[int, str | None]] = {}
        for run in runs:
            record = _mapping(run)
            name = _text(record.get("name"))
            run_id = _integer(record.get("id"))
            status = _text(record.get("status"))
            conclusion = record.get("conclusion")
            if conclusion is not None:
                conclusion = _text(conclusion)
            value = None if status != "completed" else conclusion
            previous = chosen.get(name)
            if previous is None or run_id > previous[0]:
                chosen[name] = (run_id, value)
        return {name: value for name, (_run_id, value) in chosen.items()}

    def merge(self, number: int, sha: str, token_path: str | Path) -> str:
        """Merge pull ``number`` when its head is ``sha``. Return the merge commit."""
        payload = self._request(
            "PUT",
            f"{self._root()}/pulls/{number}/merge",
            Path(token_path),
            {"sha": sha, "merge_method": "merge"},
        )
        merged = _mapping(payload).get("sha")
        if not isinstance(merged, str) or not merged:
            raise PullRequestError("outcome_unknown")
        return merged

    def _root(self) -> str:
        quoted = "/".join(quote(part, safe="") for part in self.repository.split("/"))
        return f"{self.api_url}/repos/{quoted}"

    def _token(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        token = text.strip()
        if not token:
            raise PullRequestError("credential_missing")
        return token

    def _request(
        self, method: str, url: str, token_path: Path, body: Any = None
    ) -> Any:
        token = self._token(token_path)
        try:
            _status, payload = request_json(
                method,
                url,
                token,
                body,
                timeout=self.timeout,
            )
        except PushDeliveryError as exc:
            raise PullRequestError(_status_code(exc.status_code)) from None
        return payload
