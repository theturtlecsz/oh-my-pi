"""Label-blind intake support judge (OMP-604-s04).

Serves amended OMP-604 criterion 4 (a verdict is a receipt bound to the claim
text, source span, source hash, model id and version, prompt version and rule
version) and criterion 5 (measured token counts, or unknown with null counts,
never zero standing in for unknown). The caller relation is not an argument
and is not sent to the model.

The committed prompt is ``prompts/intake_support_judge.md`` with placeholders
``{{claim}}``, ``{{passage}}`` and ``{{context}}`` only. ``tuning-pin.json``
records the sealed corpus tree, the manifest digest, and the prompt bytes.
``judge_link`` checks that pin, via ``frozen_corpus.verify``, before it calls
the model.

Receipt bindings, all recomputed from the call:

- ``claim_text_sha256``, ``passage_sha256``, ``context_sha256``: UTF-8 hashes
- ``source_span``: ``[0, len(passage.encode()))``, the passage's own bytes
- ``source_revision``: UTF-8 hash of ``context`` (the source hash)
- ``model_version``: the ``model_id`` used for the call
- ``prompt_version`` and ``prompt_sha256``: SHA-256 of the prompt file bytes
- ``rule_version``: SHA-256 of the sealed ``rules.json``

No production path installs a judge hook. Tests opt in with ``use_judge_hook``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

from omp_work.frozen_corpus import FrozenCorpus, verify
from omp_work.intake_support import Measurement, SourceSpan, SupportReceipt, Verdict
from omp_work.v1.canonical import text_sha256

MODEL_GRANT_REQUIRED = "model_grant_required"
BUDGET_EXCEEDED = "budget_exceeded"
JUDGE_UNPARSABLE = "judge_unparsable"
MIXED_TOKEN_COUNTS = "mixed_token_counts"
PIN_MISMATCH = "pin_mismatch"
JUDGE_TRANSPORT = "judge_transport"
ECHO_MODEL_ID = "echo-caller-label"
_VERDICTS: frozenset[str] = frozenset(
    {"supports", "contradicts", "neither", "abstain", "timeout", "evidence-unavailable"}
)
_PLACEHOLDER = re.compile(r"\{\{(claim|passage|context)\}\}")
_FENCE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.DOTALL | re.IGNORECASE)
_RULES_PATH = "rules.json"


class IntakeJudgeRefusal(Exception):
    """The judge refused to issue a receipt. ``code`` is the stable reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Completion:
    """One model reply. Token counts are both ints, or both None when unknown."""

    text: str
    input_tokens: int | None
    output_tokens: int | None


Complete = Callable[[str, str], Completion]
Settle = Callable[[Completion], str | None]


@dataclass
class JudgeHook:
    """Optional test stand-in. Production leaves ``current_hook()`` as None."""

    complete: Complete | None = None
    echo_caller: bool = False


_installed_hook: JudgeHook | None = None


def current_hook() -> JudgeHook | None:
    """Return the installed hook, or None when src has not installed one."""
    return _installed_hook


@contextmanager
def use_judge_hook(hook: JudgeHook) -> Iterator[JudgeHook]:
    """Install ``hook`` on a module global and restore whatever was there."""
    global _installed_hook
    prior = _installed_hook
    _installed_hook = hook
    try:
        yield hook
    finally:
        _installed_hook = prior


def prompt_path() -> Path:
    return Path(__file__).resolve().parent / "prompts" / "intake_support_judge.md"


def corpus_dir() -> Path:
    root = _repo_root()
    return root / "evals" / "intake_support" / "v1"


def prompt_bytes() -> bytes:
    return prompt_path().read_bytes()


def prompt_sha256() -> str:
    return hashlib.sha256(prompt_bytes()).hexdigest()


def judge_link(
    *,
    claim_text: str,
    passage: str,
    context: str,
    model_id: str,
    remaining_usd: Decimal | int | float,
    complete: Complete,
    settle: Settle,
) -> SupportReceipt:
    """Judge one claim against one passage and return a bound receipt.

    Refuses, before ``complete``, when ``model_id`` is empty, ``remaining_usd``
    is not positive, or the tuning pin does not match the sealed corpus and the
    prompt bytes. Completion timeouts produce a timeout receipt with unknown
    usage. ``settle`` runs once after completion or timeout and before the reply
    is parsed, including when the reply is unparsable.
    """
    _refuse_unless_admitted(model_id, remaining_usd)
    sealed, prompt_hash = _sealed_pin()
    rendered = _render(claim_text, passage, context)
    try:
        completion = complete(rendered, model_id)
    except httpx.TimeoutException:
        completion = Completion('{"verdict": "timeout"}', None, None)
    usage_id = settle(completion)
    measurement, input_tokens, output_tokens = _measurement(completion)
    verdict = _parse_verdict(completion.text)
    return _receipt(
        verdict=verdict,
        claim_text=claim_text,
        passage=passage,
        context=context,
        model_id=model_id,
        measurement=measurement,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_id=usage_id,
        sealed=sealed,
        prompt_hash=prompt_hash,
    )


def echo_receipt(
    *,
    claim_text: str,
    passage: str,
    context: str,
    caller_relation: str,
) -> SupportReceipt:
    """Receipt whose verdict copies the caller relation. Usage stays unknown.

    Does not admit, call a model, or record usage. ``model_id`` is
    ``echo-caller-label``.
    """
    sealed, prompt_hash = _sealed_pin()
    return _receipt(
        verdict=caller_relation,
        claim_text=claim_text,
        passage=passage,
        context=context,
        model_id=ECHO_MODEL_ID,
        measurement="unknown",
        input_tokens=None,
        output_tokens=None,
        usage_id=None,
        sealed=sealed,
        prompt_hash=prompt_hash,
    )


def complete_from_env(
    prompt: str,
    model_id: str,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Completion:
    """POST ``{"prompt", "model"}`` to ``OMP_INTAKE_JUDGE_URL``.

    An unset or blank URL raises ``model_grant_required`` before a client or
    transport is used. The JSON body supplies ``text`` and optional
    ``input_tokens`` and ``output_tokens``. Timeouts propagate to ``judge_link``;
    other transport errors raise ``judge_transport``.
    """
    url = os.environ.get("OMP_INTAKE_JUDGE_URL", "").strip()
    if not url:
        raise IntakeJudgeRefusal(MODEL_GRANT_REQUIRED)
    try:
        with httpx.Client(transport=transport, timeout=30.0, trust_env=False) as client:
            response = client.post(url, json={"prompt": prompt, "model": model_id})
            response.raise_for_status()
            payload = response.json()
    except httpx.TimeoutException:
        raise
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        raise IntakeJudgeRefusal(JUDGE_TRANSPORT) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
        raise IntakeJudgeRefusal(JUDGE_TRANSPORT)
    return Completion(
        text=payload["text"],
        input_tokens=_optional_count(payload, "input_tokens"),
        output_tokens=_optional_count(payload, "output_tokens"),
    )


def _refuse_unless_admitted(
    model_id: str, remaining_usd: Decimal | int | float | None,
) -> None:
    if not isinstance(model_id, str) or model_id == "":
        raise IntakeJudgeRefusal(MODEL_GRANT_REQUIRED)
    if remaining_usd is None or remaining_usd <= 0:
        raise IntakeJudgeRefusal(BUDGET_EXCEEDED)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "evals" / "intake_support" / "v1" / "manifest.json").is_file():
            return candidate
    raise IntakeJudgeRefusal(PIN_MISMATCH)


def _sealed_pin() -> tuple[FrozenCorpus, str]:
    """Verify the corpus seal and require the tuning pin to match it."""
    base = corpus_dir()
    try:
        sealed = verify(base / "inputs", base / "manifest.json")
    except ValueError as exc:
        raise IntakeJudgeRefusal(PIN_MISMATCH) from exc
    try:
        pin = json.loads((base / "tuning-pin.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeJudgeRefusal(PIN_MISMATCH) from exc
    prompt_hash = prompt_sha256()
    expected = {
        "tree_sha256": sealed.tree_sha256,
        "manifest_sha256": sealed.manifest_sha256,
        "prompt_sha256": prompt_hash,
    }
    if not isinstance(pin, dict) or any(pin.get(key) != value for key, value in expected.items()):
        raise IntakeJudgeRefusal(PIN_MISMATCH)
    return sealed, prompt_hash


def _rule_version(sealed: FrozenCorpus) -> str:
    for item in sealed.files:
        if item.path == _RULES_PATH:
            return item.sha256
    raise IntakeJudgeRefusal(PIN_MISMATCH)


def _render(claim_text: str, passage: str, context: str) -> str:
    values = {"claim": claim_text, "passage": passage, "context": context}
    seen: set[str] = set()

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        seen.add(key)
        return values[key]

    rendered = _PLACEHOLDER.sub(sub, prompt_bytes().decode("utf-8"))
    if seen != set(values):
        raise IntakeJudgeRefusal(PIN_MISMATCH)
    return rendered


def _parse_verdict(text: str) -> Verdict:
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise IntakeJudgeRefusal(JUDGE_UNPARSABLE) from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"verdict"}
        or not isinstance(value["verdict"], str)
        or value["verdict"] not in _VERDICTS
    ):
        raise IntakeJudgeRefusal(JUDGE_UNPARSABLE)
    return value["verdict"]  # type: ignore[return-value]


def _measurement(
    completion: Completion,
) -> tuple[Measurement, int | None, int | None]:
    counts = (completion.input_tokens, completion.output_tokens)
    if all(count is None for count in counts):
        return "unknown", None, None
    if all(_nonnegative_int(count) for count in counts):
        return "measured", counts[0], counts[1]
    raise IntakeJudgeRefusal(MIXED_TOKEN_COUNTS)


def _nonnegative_int(count: int | None) -> bool:
    return isinstance(count, int) and not isinstance(count, bool) and count >= 0


def _optional_count(payload: dict[str, object], key: str) -> int | None:
    if key not in payload or payload[key] is None:
        return None
    value = payload[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntakeJudgeRefusal(JUDGE_TRANSPORT)
    return value


def _receipt(
    *,
    verdict: str,
    claim_text: str,
    passage: str,
    context: str,
    model_id: str,
    measurement: Measurement,
    input_tokens: int | None,
    output_tokens: int | None,
    usage_id: str | None,
    sealed: FrozenCorpus,
    prompt_hash: str,
) -> SupportReceipt:
    return SupportReceipt(
        verdict=verdict,  # type: ignore[arg-type]
        source_span=SourceSpan(start=0, end=len(passage.encode())),
        source_revision=text_sha256(context),
        model_version=model_id,
        prompt_version=prompt_hash,
        rule_version=_rule_version(sealed),
        passage_sha256=text_sha256(passage),
        claim_text_sha256=text_sha256(claim_text),
        context_sha256=text_sha256(context),
        prompt_sha256=prompt_hash,
        model_id=model_id,
        measurement=measurement,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        usage_id=usage_id,
    )
