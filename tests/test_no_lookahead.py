"""§12.2 防未来信息泄漏。"""

from datetime import date

from gopher.agents.strategist import validate
from gopher.clock import parse_dt
from gopher.evidence import EvidenceStore
from gopher.market_data import MarketStore, daily_bar, minute_bar
from gopher.schemas import Candidate, DecisionProposal, HoldCounterfactual, Scenario


def test_intraday_low_not_visible_before_open(settings):
    m = MarketStore(settings.bars_path)
    m.add([daily_bar("v", "XLV", date(2026, 11, 2), 185, 186, 183.5, 184.5)])
    assert m.view(parse_dt("2026-11-02T09:00:00")).daily("XLV", date(2026, 11, 2)) == []
    assert m.view(parse_dt("2026-11-02T15:59:00")).daily("XLV", date(2026, 11, 2)) == []
    assert m.view(parse_dt("2026-11-02T16:20:00")).daily("XLV", date(2026, 11, 2))


def test_ipo_first_day_close_not_visible_pre_open(settings):
    m = MarketStore(settings.bars_path)
    m.add([daily_bar("v", "NEWCO", date(2026, 11, 19), 30, 45, 29, 42)])
    v = m.view(parse_dt("2026-11-19T09:29:00"))
    assert v.latest_close("NEWCO") is None


def test_minute_bar_only_visible_after_it_closes(settings):
    m = MarketStore(settings.bars_path)
    m.add([minute_bar("v", "XLF", parse_dt("2026-10-30T09:43:00"), 57, 57, 56.9, 56.95, 1000)])
    assert m.view(parse_dt("2026-10-30T09:43:30")).minutes("XLF", parse_dt("2026-10-30T09:30:00"), parse_dt("2026-10-30T16:00:00")) == []
    assert m.view(parse_dt("2026-10-30T09:44:00")).minutes("XLF", parse_dt("2026-10-30T09:30:00"), parse_dt("2026-10-30T16:00:00"))


def test_earnings_evidence_not_visible_before_release(settings):
    ev = EvidenceStore(settings.evidence_path)
    release = parse_dt("2026-11-17T16:20:00")
    ev.add(source_url="https://example.com/q3", retrieved_at=release, available_at=release, published_at=release,
           source_type="filing", reliability="verified", content={"eps": 1.23}, title="Q3 财报")
    assert ev.visible(parse_dt("2026-11-17T15:00:00")) == []
    assert len(ev.visible(parse_dt("2026-11-17T17:00:00"))) == 1


def test_query_uses_available_at_not_latest_db_value(settings):
    """同一交易日后补抓的数据，回测仍按 available_at 判断，而非数据库当前是否存在。"""
    m = MarketStore(settings.bars_path)
    m.add([daily_bar("v", "SPY", date(2026, 11, 18), 720, 725, 715, 722,
                     retrieved_at=parse_dt("2026-11-20T10:00:00"))])
    assert m.view(parse_dt("2026-11-18T12:00:00")).latest_close("SPY") is None
    assert m.view(parse_dt("2026-11-18T17:00:00")).latest_close("SPY").price == 722


def _cand(ids):
    sc = [Scenario(name="bull", probability=0.3, return_pct=4, rationale=""),
          Scenario(name="base", probability=0.45, return_pct=1, rationale=""),
          Scenario(name="bear", probability=0.25, return_pct=-5, rationale="")]
    return Candidate(action="BUY", ticker="XLV", budget_usd=3000, thesis="t", evidence_ids=ids, scenarios=sc,
                     invalidation_conditions=["x"], horizon_days=10, confidence="low")


def test_strategist_cannot_cite_unavailable_evidence():
    p = DecisionProposal(market_state="", candidates=[_cand(["ev_future"])],
                         hold_counterfactual=HoldCounterfactual(rationale="", cash_opportunity_cost_note=""),
                         no_trade_reason=None, key_risks=[])
    vc = validate(p, {"ev_ok"}, {"XLV"}, 10000, 3)[0]
    assert not vc.valid and vc.candidate.evidence_ids == []


def test_expected_return_is_computed_in_code():
    p = DecisionProposal(market_state="", candidates=[_cand(["ev_ok"])],
                         hold_counterfactual=HoldCounterfactual(rationale="", cash_opportunity_cost_note=""),
                         no_trade_reason=None, key_risks=[])
    vc = validate(p, {"ev_ok"}, {"XLV"}, 10000, 3)[0]
    assert vc.valid
    assert abs(vc.expected_return_pct - 0.40) < 1e-9  # 与文档 §14 示例一致
