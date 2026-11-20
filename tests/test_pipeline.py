"""端到端：离线引擎跑完整多 Agent 流程。"""

from gopher.clock import parse_dt
from gopher.ledger import replay
from gopher.orchestrator import run_daily

from conftest import synth_market


def test_offline_pipeline_records_decisions_and_report(store, settings):
    synth_market(settings)
    n0 = len(store.load())
    res = run_daily(settings, as_of=parse_dt("2026-11-20T09:00:00"), engine="offline")
    assert res.valuation.nav is not None
    assert res.audit.passed
    st = replay(store.load(), settings.initial_cash)
    holds = [d for d in st.decisions if d["action"] in ("HOLD", "NO_TRADE")]
    assert holds, "HOLD/NO_TRADE 也必须被记录（方向 B）"
    assert len(store.load()) > n0
    assert not store.verify()
    for g in res.gated:
        if g["gate"] == "MEETING_REQUIRED":
            assert res.meeting_id
    assert not st.orders, "shadow 模式不得自动下单"
    assert res.report_path.exists()
    text = res.report_path.read_text(encoding="utf-8")
    assert "提案，尚未成交" in text and "report_hash" in text


def test_dry_run_writes_nothing(store, settings):
    synth_market(settings)
    n0 = len(store.load())
    run_daily(settings, as_of=parse_dt("2026-11-20T09:00:00"), engine="offline", record=False)
    assert len(store.load()) == n0


def test_dry_run_writes_no_evidence(store, settings):
    synth_market(settings)
    run_daily(settings, as_of=parse_dt("2026-11-20T09:00:00"), engine="offline", record=False)
    assert not settings.evidence_path.exists()
