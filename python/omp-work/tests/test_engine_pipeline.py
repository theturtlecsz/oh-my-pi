"""engine_pipeline acceptance."""
import sys, json
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omp_work.engine_pipeline import run_retrieve_compile_campaign, estimate_tokens
from omp_work.enola_store import EnolaStore
from omp_work.cognee_store import CogneeStore


@pytest.fixture
def caps_file(tmp_path: Path) -> Path:
    caps = tmp_path / "BUDGET-CAPS.json"
    caps.write_text(json.dumps({
        "soft_warn_fraction": 0.7,
        "hard_refuse_fraction": 1.0,
        "post_program_milestone_soft_tokens": 2000000,
        "post_program_milestone_hard_tokens": 5000000,
    }))
    return caps


def test_pipeline_happy(caps_file):
    r = run_retrieve_compile_campaign(
        job_id="omp-pipe-test",
        query="write-first effort",
        objective="Seal a write-first packet under E1",
        max_context_tokens=1200,
        caps_path=caps_file,
        enola_enabled=False,
        ledger_attach=True,
    )
    assert r.job_id == "omp-pipe-test"
    assert r.findings
    assert r.campaign.stopped is True
    assert r.ledger_event is not None
    assert r.ledger_event["kind"] == "research_campaign"


def test_pipeline_requires_job_id():
    try:
        run_retrieve_compile_campaign(job_id=" ", query="x", objective="y", enola_enabled=False, ledger_attach=False)
        assert False
    except ValueError:
        pass


def test_truncate_under_tiny_bar(caps_file):
    store = {"a": {"claim": "X" * 5000, "source": "t"}, "b": {"claim": "Y" * 5000, "source": "t"}}
    r = run_retrieve_compile_campaign(
        job_id="tiny", query="a b", objective="fit", max_context_tokens=80,
        store=store, caps_path=caps_file, enola_enabled=False, ledger_attach=False,
    )
    assert r.truncated is True
    assert r.context_tokens_est <= 80


def test_estimate_tokens():
    assert estimate_tokens("abcd") == 1


def test_pipeline_uses_active_cognee_store(tmp_path, caps_file):
    active_store = tmp_path / "cognee-store.json"
    CogneeStore.open(active_store)
    assert active_store.is_file()
    r = run_retrieve_compile_campaign(
        job_id="pipe-store", query="post-program autonomy", objective="Respect BUDGET-CAPS",
        store_path=active_store, caps_path=caps_file, enola_enabled=False, ledger_attach=False,
    )
    assert r.store_path == str(active_store)


def test_pipeline_enola_wire(tmp_path, caps_file):
    enola = tmp_path / "enola.json"
    r = run_retrieve_compile_campaign(
        job_id="pipe-enola", query="write-first", objective="continuous admit",
        store={"write-first": {"claim": "Prefer write-first", "source": "t"}},
        caps_path=caps_file, enola_store_path=enola, enola_enabled=True, ledger_attach=False,
    )
    assert r.enola_trace_key and r.enola_finding


def test_pipeline_ledger_append(tmp_path, caps_file):
    led = tmp_path / "camp.json"
    r = run_retrieve_compile_campaign(
        job_id="pipe-led", query="write-first", objective="ledger",
        store={"write-first": {"claim": "Prefer write-first", "source": "t"}},
        caps_path=caps_file, enola_enabled=False, ledger_attach=True, ledger_path=led,
    )
    assert led.is_file()
    data = json.loads(led.read_text())
    assert data["events"][0]["conversation_id"] == "pipe-led-camp"
