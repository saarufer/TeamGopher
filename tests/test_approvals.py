"""§12.4 风险与授权。"""

from decimal import Decimal

import pytest

from gopher.approvals import ApprovalError, approve, create_meeting, reject
from gopher.clock import parse_dt
from gopher.execution import OrderRejected, create_order
from gopher.ledger import replay
from gopher.market_data import MarketStore
from gopher.risk import assess
from gopher.valuation import value_portfolio

from conftest import synth_market
from helpers import approved

EFF = "2026-12-01T09:43:00"
NOW = "2026-12-01T09:00:00"


def _create(store, settings, ap, qty, ticker="XLV", px="180"):
    return create_order(store, settings, MarketStore(settings.bars_path), ticker=ticker, side="BUY",
                        quantity=Decimal(qty), order_type="LIMIT", limit_price=Decimal(px),
                        effective_from=parse_dt(EFF), approval_id=ap, decision_id="t", now=parse_dt(NOW))


def test_order_without_approval_rejected(store, settings):
    with pytest.raises(OrderRejected, match="授权"):
        _create(store, settings, None, "10")


def test_over_budget_order_rejected(store, settings):
    _, ap = approved(store, settings)  # XLV ≤ $3000
    with pytest.raises(OrderRejected, match="预算|上限"):
        _create(store, settings, ap, "17")  # 17 × 180 = 3060


def test_approved_budget_cannot_be_exceeded_cumulatively(store, settings):
    _, ap = approved(store, settings, caps={"XLI": Decimal("8000"), "XLV": Decimal("7000"), "XLF": Decimal("5000")},
                     budget=Decimal("20000"))
    _create(store, settings, ap, "38", "XLV", "180")    # 6840
    _create(store, settings, ap, "45", "XLI", "170")    # 7650
    with pytest.raises(OrderRejected):
        _create(store, settings, ap, "100", "XLF", "57")  # 5700 > XLF 上限
    _create(store, settings, ap, "87", "XLF", "57")     # 4959 → 合计 19449
    with pytest.raises(OrderRejected):
        _create(store, settings, ap, "1", "XLV", "180")  # 剩余预算 551，XLV 上限已用 6840 + 180 > 7000


def test_rejected_meeting_cannot_produce_active_order(store, settings):
    mid = create_meeting(store, candidates=[{"ticker": "XLV"}], triggers=["t"], summary="", at=parse_dt("2026-11-30T20:00:00"))
    reject(store, settings.initial_cash, mid, "不同意", at=parse_dt("2026-11-30T21:00:00"))
    with pytest.raises(ApprovalError):
        approve(store, settings.initial_cash, mid, tickers_caps_usd={"XLV": Decimal("1000")}, sides=["BUY"],
                valid_until=parse_dt("2026-12-04T16:00:00"), execution_rules="", at=parse_dt("2026-11-30T22:00:00"))
    st = replay(store.load(), settings.initial_cash)
    assert st.meetings[mid]["status"] == "REJECTED"
    assert not st.orders


def test_ambiguous_support_is_not_approval(store, settings):
    mid = create_meeting(store, candidates=[{"ticker": "XLV"}, {"ticker": "XLI"}], triggers=["t"], summary="",
                         at=parse_dt("2026-11-30T20:00:00"))
    with pytest.raises(ApprovalError, match="唯一"):
        approve(store, settings.initial_cash, mid, tickers_caps_usd={"XLV": Decimal("1000")}, sides=["BUY"],
                valid_until=parse_dt("2026-12-04T16:00:00"), execution_rules="", at=parse_dt("2026-11-30T21:00:00"))


def test_historical_approval_without_validity_cannot_be_reused(store, settings):
    with pytest.raises(OrderRejected, match="有效期未登记"):
        _create(store, settings, "appr_hist_20261029", "5")


def test_historical_20k_budget_usage(store, settings):
    st = replay(store.load(), settings.initial_cash)
    a = st.approvals["appr_hist_20261029"]
    assert a.used_usd == Decimal("13170.89")
    assert st.approval_remaining("appr_hist_20261029") == Decimal("6829.11")


def _val(store, settings):
    synth_market(settings)
    as_of = parse_dt("2026-11-19T17:00:00")
    st = replay(store.load(), settings.initial_cash, as_of=as_of)
    return st, value_portfolio(st, MarketStore(settings.bars_path).view(as_of), settings)


def test_single_stock_15pct_triggers_meeting(store, settings):
    st, v = _val(store, settings)
    rk = assess(st, v, "NVDA", "BUY", v.nav * Decimal("0.14"), settings, parse_dt("2026-11-20T10:00:00"))
    assert any("单一股票" in t for t in rk.meeting_triggers)


def test_cash_change_over_threshold_triggers_meeting(store, settings):
    st, v = _val(store, settings)
    small = assess(st, v, "XLV", "BUY", Decimal("3000"), settings, parse_dt("2026-11-20T10:00:00"))
    assert not any("现金比例" in t for t in small.meeting_triggers)
    big = assess(st, v, "XLV", "BUY", v.nav * Decimal("0.16"), settings, parse_dt("2026-11-20T10:00:00"))
    assert any("现金比例" in t for t in big.meeting_triggers)


def test_buy_without_approval_needs_meeting(store, settings):
    st, v = _val(store, settings)
    rk = assess(st, v, "XLU", "BUY", Decimal("2000"), settings, parse_dt("2026-11-20T10:00:00"))
    assert rk.meeting_required and rk.covering_approval_id is None


def test_sell_dollar_cap_enforced_across_orders(store, settings):
    _, ap = approved(store, settings, caps={"NVDA": Decimal("1100")}, sides=("SELL",))
    kw = dict(ticker="NVDA", side="SELL", quantity=Decimal("7"), order_type="LIMIT", limit_price=Decimal("140"),
              effective_from=parse_dt(EFF), approval_id=ap, decision_id="t", now=parse_dt(NOW))
    create_order(store, settings, MarketStore(settings.bars_path), **kw)
    with pytest.raises(OrderRejected, match="上限"):
        create_order(store, settings, MarketStore(settings.bars_path), **kw)


def test_approval_must_match_chosen_candidate(store, settings):
    mid = create_meeting(store, candidates=[{"ticker": "XLP", "action": "BUY"}, {"ticker": "NVDA", "action": "SELL"}],
                         triggers=["t"], summary="", at=parse_dt("2026-11-30T20:00:00"))
    kw = dict(valid_until=parse_dt("2026-12-04T16:00:00"), execution_rules="", at=parse_dt("2026-11-30T21:00:00"))
    with pytest.raises(ApprovalError, match="没有 XLP"):
        approve(store, settings.initial_cash, mid, tickers_caps_usd={"NVDA": Decimal("1100")}, sides=["SELL"],
                candidate_index=0, **kw)
    with pytest.raises(ApprovalError, match="--sides SELL"):
        approve(store, settings.initial_cash, mid, tickers_caps_usd={"NVDA": Decimal("1100")}, sides=["BUY"],
                candidate_index=1, **kw)
    approve(store, settings.initial_cash, mid, tickers_caps_usd={"NVDA": Decimal("1100")}, sides=["SELL"],
            candidate_index=1, **kw)
