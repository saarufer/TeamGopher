"""§8.2 基准与 §12.5 数据质量。"""

from datetime import date
from decimal import Decimal

from gopher.clock import ET, market_session, parse_dt, last_completed_session
from gopher.ledger import replay
from gopher.market_data import MarketStore, daily_bar
from gopher.valuation import performance, value_portfolio

from conftest import synth_market


def test_spy_benchmark_formula(store, settings):
    synth_market(settings)
    as_of = parse_dt("2026-11-19T17:00:00")
    st = replay(store.load(), settings.initial_cash, as_of=as_of)
    v = value_portfolio(st, MarketStore(settings.bars_path).view(as_of), settings)
    p = performance(v, settings)
    spy = v.benchmark_close
    assert p.benchmark_nav == Decimal("100000") / Decimal("769.64") * spy
    assert p.benchmark_return == spy / Decimal("769.64") - 1
    assert p.active_pnl_usd == p.nav - p.benchmark_nav
    assert v.official


def test_price_conflict_suspends_official_nav(store, settings):
    m = synth_market(settings)
    d = date(2026, 11, 19)
    q = m.view(parse_dt("2026-11-19T17:00:00")).close("XLV", d)
    m.add([daily_bar("other_vendor", "XLV", d, q.price, q.price, q.price, q.price * Decimal("1.02"))])
    st = replay(store.load(), settings.initial_cash)
    v = value_portfolio(st, m.view(parse_dt("2026-11-19T17:00:00")), settings)
    assert not v.official and any("冲突" in r for r in v.reasons)


def test_mixed_adjustment_types_flagged(store, settings):
    m = synth_market(settings)
    m.add([daily_bar("adj_vendor", "XLV", date(2026, 11, 18), 1, 1, 1, 177, adjustment_type="total_return_adjusted")])
    v = value_portfolio(replay(store.load(), settings.initial_cash), m.view(parse_dt("2026-11-19T17:00:00")), settings)
    assert any("口径混用" in r for r in v.reasons)


def test_holidays_and_dst():
    assert market_session(parse_dt("2026-11-26T11:00:00")) == "closed"           # 感恩节
    assert market_session(parse_dt("2026-11-27T13:30:00")) == "afterhours"       # 提前收盘
    # 美国 11/01 结束夏令时、德国 10/25 结束：10/26–10/30 美股开盘为柏林 14:30
    assert parse_dt("2026-10-27T09:30:00").astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).hour == 14
    assert parse_dt("2026-11-03T09:30:00").astimezone(__import__("zoneinfo").ZoneInfo("Europe/Berlin")).hour == 15
    assert last_completed_session(parse_dt("2026-11-23T08:00:00")) == date(2026, 11, 20)
