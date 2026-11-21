"""新增 Agent：市场状态、仓位管理、事件预期、复盘校准。"""

from datetime import date
from decimal import Decimal

import pytest

from gopher.agents import exposure, expectations, postmortem, regime
from gopher.agents.auditor import audit
from gopher.agents.rotation import compute
from gopher.clock import parse_dt
from gopher.ledger import replay
from gopher.market_data import MarketStore
from gopher.orchestrator import run_daily
from gopher.valuation import nav_series

from conftest import synth_market

AS_OF = "2026-11-20T09:00:00"


def _regime(label):
    return regime.Regime(label, 0, None, None, None, None, None, None, [])


# ---------- 市场状态 ----------

def test_regime_needs_enough_history(settings):
    m = MarketStore(settings.bars_path)
    assert regime.assess(m.view(parse_dt(AS_OF))).label == "unknown"


def test_regime_on_synthetic_downtrend(settings):
    m = synth_market(settings)
    r = regime.assess(m.view(parse_dt(AS_OF)))
    assert r.label in {"risk_off", "neutral"} and r.reasons


def test_regime_is_point_in_time(settings):
    """9/15 开盘前的判断，只能和“只有 9/14 及以前数据”时完全一样。"""
    m = synth_market(settings)
    early = regime.assess(m.view(parse_dt("2026-09-15T09:00:00")))
    truncated = MarketStore()
    truncated.add([b for b in m.bars.values() if b.session_date <= date(2026, 9, 14)])
    assert regime.assess(truncated.view(parse_dt(AS_OF))) == early


# ---------- 仓位管理 ----------

def test_too_much_cash_for_too_long_triggers_redeploy(settings):
    ex = exposure.assess(settings, _regime("neutral"), Decimal("100000"), Decimal("0.80"), [0.8] * 5, [])
    assert ex.status == "TOO_MUCH_CASH" and ex.mandate == "REDEPLOY"
    assert round(ex.gap_usd) == 15000


def test_brief_drift_does_not_force_trade(settings):
    ex = exposure.assess(settings, _regime("neutral"), Decimal("100000"), Decimal("0.80"), [0.5, 0.5, 0.8], [])
    assert ex.status == "TOO_MUCH_CASH" and ex.mandate is None and ex.days_out_of_band == 1


def test_in_band_no_mandate(settings):
    ex = exposure.assess(settings, _regime("risk_off"), Decimal("100000"), Decimal("0.686"), [0.686] * 10, [])
    assert ex.status == "IN_BAND" and ex.mandate is None


def test_inertia_counts_runs_since_last_fill(settings):
    decisions = [{"seq": i, "ticker": None, "action": "HOLD"} for i in range(10, 16)]
    ex = exposure.assess(settings, _regime("neutral"), Decimal("100000"), Decimal("0.70"), [0.70], decisions, 12)
    assert ex.consecutive_holds == 3 and ex.mandate is None
    ex = exposure.assess(settings, _regime("neutral"), Decimal("100000"), Decimal("0.70"), [0.70], decisions, 5)
    assert ex.consecutive_holds == 6 and ex.mandate == "REDEPLOY"


def test_de_risk_sells_highest_beta_holding(settings):
    m = synth_market(settings)
    feats = compute(m.view(parse_dt(AS_OF)), ["SPY", "NVDA", "XLV"])
    ex = exposure.assess(settings, _regime("risk_off"), Decimal("100000"), Decimal("0.30"), [0.3] * 5, [])
    c = exposure.propose(ex, settings, feats, {"NVDA": 0.3, "XLV": 0.4}, {"NVDA": 150.0, "XLV": 200.0}, ["ev"], 10)
    assert ex.mandate == "DE_RISK" and c.action == "SELL" and c.ticker == "NVDA"


def test_pipeline_adds_exposure_candidate_when_mandated(store, settings):
    synth_market(settings)
    settings.risk["exposure_policy"] = {"bands": {k: {"cash_min": "0.10", "cash_max": "0.30"}
                                                 for k in ("risk_on", "neutral", "risk_off", "unknown")},
                                        "max_days_out_of_band": 2, "rebalance_step_usd": "5000"}
    res = run_daily(settings, as_of=parse_dt(AS_OF), engine="offline")
    assert res.exposure.mandate == "REDEPLOY"
    buys = [g for g in res.gated if g["candidate"].action == "BUY"]
    assert buys, "必须至少有一个 BUY 候选"
    for g in buys:
        if g["origin"] == "exposure":
            assert g["gate"] in ("MEETING_REQUIRED", "BLOCKED")  # 仍然需要授权，不会自动成交
    assert not replay(store.load(), settings.initial_cash).orders


# ---------- 事件预期 ----------

def test_forecast_frozen_before_event_and_not_duplicated(store, settings):
    synth_market(settings)
    key = expectations.schedule(store, name="US CPI", scheduled_at=parse_dt("2026-11-24T08:30:00"),
                                tickers=["SPY", "XLF"], consensus="同比 2.9%", at=parse_dt("2026-11-19T20:00:00"))
    run_daily(settings, as_of=parse_dt(AS_OF), engine="offline")
    st = replay(store.load(), settings.initial_cash)
    fcs = [f for f in st.forecasts if f["event_key"] == key]
    assert {f["ticker"] for f in fcs} == {"SPY", "XLF"}
    for f in fcs:
        assert parse_dt(f["data_cutoff"]) < parse_dt(f["scheduled_at"])
        assert abs(sum(s["probability"] for s in f["scenarios"]) - 1) < 1e-9
    run_daily(settings, as_of=parse_dt("2026-11-20T10:00:00"), engine="offline")
    assert len([f for f in replay(store.load(), settings.initial_cash).forecasts if f["event_key"] == key]) == 2


def test_no_forecast_after_event(store, settings):
    expectations.schedule(store, name="FOMC", scheduled_at=parse_dt("2026-11-18T14:00:00"), tickers=["SPY"],
                          at=parse_dt("2026-11-10T20:00:00"))
    st = replay(store.load(), settings.initial_cash)
    assert expectations.pending(st, parse_dt(AS_OF)) == []
    ev = next(iter(st.scheduled_events.values()))
    fc = expectations.offline_forecast(ev, "SPY", None)
    with pytest.raises(expectations.ExpectationError):
        expectations.to_payload(ev, fc, made_at=parse_dt(AS_OF), cutoff=parse_dt(AS_OF), engine="t")


def test_actual_cannot_be_recorded_before_release(store, settings):
    key = expectations.schedule(store, name="NFP", scheduled_at=parse_dt("2026-12-04T08:30:00"), tickers=["SPY"],
                                at=parse_dt("2026-11-20T09:00:00"))
    st = replay(store.load(), settings.initial_cash)
    with pytest.raises(expectations.ExpectationError, match="公布时间"):
        expectations.record_actual(store, st, key, "+150k", parse_dt("2026-12-04T08:00:00"))
    # 绕过接口直接写入，也会被审计发现
    store.append("EventActualRecorded", {"event_key": key, "actual_after_release": "+150k"},
                 occurred_at=parse_dt("2026-12-04T08:00:00"))
    events = store.load()
    rep = audit(store, events, replay(events, settings.initial_cash))
    assert any("未来信息" in x for x in rep.blocking)


# ---------- 复盘校准 ----------

def test_decomposition_adds_up_and_counterfactuals(store, settings):
    m = synth_market(settings)
    as_of = parse_dt(AS_OF)
    events = store.load()
    series = nav_series(events, m, settings, as_of)
    pm = postmortem.run(settings, events, m.view(as_of), as_of, series)
    d = pm.decomposition
    assert abs(d["exposure_effect"] + d["beyond_exposure"] - d["excess_vs_spy"]) < 1e-6
    names = {c["name"]: c for c in pm.counterfactuals}
    assert abs(names["满仓 SPY（基准）"]["nav"] - float(series[-1].benchmark_nav)) < 1e-6
    no_1019 = names["10/19 不交易"]
    assert "SELL QQQ" in no_1019["note"] and "SELL NVDA" in no_1019["note"]
    no_1009 = names["10/09 不交易"]
    assert "（连带）" in no_1009["note"]  # 没买 BNO，10/12 的卖出也不能发生


def test_decisions_get_scored_after_horizon(store, settings):
    synth_market(settings)
    run_daily(settings, as_of=parse_dt("2026-10-20T09:00:00"), engine="offline")
    as_of = parse_dt(AS_OF)
    events = store.load()
    m = MarketStore(settings.bars_path)
    pm = postmortem.run(settings, events, m.view(as_of), as_of, nav_series(events, m, settings, as_of))
    holds = [s for s in pm.scored if s.kind == "hold"]
    assert holds and holds[0].realized_pct is not None and "SPY" in holds[0].note
    for s in pm.scored:
        if s.kind == "decision" and s.brier is not None:
            assert 0 <= s.brier <= 1
