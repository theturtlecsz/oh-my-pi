from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from uuid import uuid4

import pytest

from omp_knowledge.learning.cli import EXIT_RUN_FAILED, main as cli_main
from omp_knowledge.learning.store import LearningStore


def test_promotion_answer_unknown_id_json() -> None:
    store = LearningStore(":memory:")
    decision_id = str(uuid4())
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "promotion-answer",
                "--state-dir",
                ":memory:",
                "--decision-id",
                decision_id,
                "--approve",
                "--answer-ref",
                "x",
                "--json",
            ],
            store=store,
        )
    assert rc == EXIT_RUN_FAILED
    out = json.loads(buf.getvalue().strip())
    assert "error" in out
    assert decision_id in out["error"]
    assert "not found" in out["error"]


def test_promotion_answer_unknown_id_no_json() -> None:
    store = LearningStore(":memory:")
    decision_id = str(uuid4())
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_main(
            [
                "promotion-answer",
                "--state-dir",
                ":memory:",
                "--decision-id",
                decision_id,
                "--approve",
                "--answer-ref",
                "x",
            ],
            store=store,
        )
    assert rc == EXIT_RUN_FAILED
    stdout = buf.getvalue().strip()
    assert decision_id in stdout
    assert "not found" in stdout
    assert "Traceback" not in stdout
    assert not stdout.startswith("'") and not stdout.startswith('"')
