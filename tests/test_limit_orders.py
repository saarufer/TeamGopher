"""§12.3 订单执行：禁止事后成交。"""

from datetime import date
from decimal import Decimal

import pytest

from gopher.clock import parse_dt
from gopher.event_store import EventStore
from gopher.evidence import EvidenceStore
from gopher.execution import OrderRejected, create_order, reconcile
from gopher.ledger import replay
from gopher.market_data import MarketStore, daily_bar, minute_bar

from helpers import approved

DAY = date(2026, 12, 1)


def _order(store, settings, market, **kw):
    _, ap = approved(store, settings)
    args = dict(ticker="XLV", side="BUY", quantity=Decimal("10"), order_type="LIMIT", limit_price=Decimal("180"),
                effective_from=parse_dt("2026-12-01T09:43:00"), approval_id=ap, decision_id="t",
                now=parse_dt("2026-12-01T09:00:00"))
    args.update(kw)
    return create_order(store, settings, market, **args)


def _state(store, settings, oid):
    return replay(store.load(), settings.initial_cash).orders[oid]


def test_bars_before_activation_cannot_fill(store, settings):
    m = MarketStore(settings.bars_path)
    o = _order(store, settings, m)
    m.add([minute_bar("v", "XLV", parse_dt(f"2026-12-01T09:{mm:02d}:00"), 181, 181, 179.5, 180.5, 500)
           for mm in range(30, 43)])  # 09:30–09:42 已触价
    m.add([minute_bar("v", "XLV", parse_dt(f"2026-12-01T09:{mm:02d}:00"), 181, 182, 180.5, 181.5, 500)
           for mm in range(43, 60)])
    reconcile(store, settings, m, EvidenceStore(settings.evidence_path), parse_dt("2026-12-01T10:00:00"))
    assert _state(store, settings, o.order_id).status == "ACTIVE"


def test_minute_bar_after_activation_fills_at_limit(store, settings):
    m = MarketStore(settings.bars_path)
    o = _order(store, settings, m)
    m.add([minute_bar("v", "XLV", parse_dt("2026-12-01T09:50:00"), 180.4, 180.5, 179.0, 179.2, 800)])
    reconcile(store, settings, m, EvidenceStore(settings.evidence_path), parse_dt("2026-12-01T10:00:00"))
    st = replay(store.load(), settings.initial_cash)
    assert st.orders[o.order_id].status == "FILLED"
    fill = st.fills[-1]
    assert Decimal(fill["price"]) == Decimal("180")  # 不因更低价格获得更好成交
    assert fill["evidence_grade"] == "B"


def test_daily_low_only_is_eligible_unverified_not_filled(store, settings):
    m = MarketStore(settings.bars_path)
    o = _order(store, settings, m)
    m.add([daily_bar("v", "XLV", DAY, 182, 183, 178, 181)])
    ev = EvidenceStore(settings.evidence_path)
    reconcile(store, settings, m, ev, parse_dt("2026-12-01T17:00:00"))
    assert _state(store, settings, o.order_id).status == "ELIGIBLE_UNVERIFIED"
    # 宽限期后仍无分钟证据 → 到期，不成交
    reconcile(store, settings, m, ev, parse_dt("2026-12-05T17:00:00"))
    assert _state(store, settings, o.order_id).status == "EXPIRED"
    assert not any(f.get("order_id") == o.order_id for f in replay(store.load(), settings.initial_cash).fills)


def test_eligible_can_be_upgraded_with_minute_evidence(store, settings):
    m = MarketStore(settings.bars_path)
    o = _order(store, settings, m)
    m.add([daily_bar("v", "XLV", DAY, 182, 183, 178, 181)])
    ev = EvidenceStore(settings.evidence_path)
    reconcile(store, settings, m, ev, parse_dt("2026-12-01T17:00:00"))
    m.add([minute_bar("v", "XLV", parse_dt("2026-12-01T14:10:00"), 180.2, 180.3, 178.0, 178.5, 900,
                      retrieved_at=parse_dt("2026-12-02T08:00:00"))])
    reconcile(store, settings, m, ev, parse_dt("2026-12-02T08:30:00"))
    assert _state(store, settings, o.order_id).status == "FILLED"


def test_expired_order_does_not_revive_next_day(store, settings):
    m = MarketStore(settings.bars_path)
    o = _order(store, settings, m)
    ev = EvidenceStore(settings.evidence_path)
    reconcile(store, settings, m, ev, parse_dt("2026-12-01T16:30:00"))
    assert _state(store, settings, o.order_id).status == "EXPIRED"
    m.add([minute_bar("v", "XLV", parse_dt("2026-12-02T10:00:00"), 179, 179, 170, 171, 5000)])
    reconcile(store, settings, m, ev, parse_dt("2026-12-02T11:00:00"))
    assert _state(store, settings, o.order_id).status == "EXPIRED"


def test_backfilled_order_rejected(store, settings):
    with pytest.raises(OrderRejected, match="回填"):
        _order(store, settings, MarketStore(settings.bars_path), now=parse_dt("2026-12-01T10:00:00"))


def test_day_order_cannot_roll_to_next_day(store, settings):
    with pytest.raises(OrderRejected, match="滚动"):
        _order(store, settings, MarketStore(settings.bars_path), expires_at=parse_dt("2026-12-02T16:00:00"))


def test_cannot_sell_more_than_held(store, settings):
    _, ap = approved(store, settings, caps={"XLE": Decimal("100000")}, sides=("SELL",))
    with pytest.raises(OrderRejected, match="做空"):
        create_order(store, settings, MarketStore(settings.bars_path), ticker="XLE", side="SELL",
                     quantity=Decimal("47"), order_type="LIMIT", limit_price=Decimal("60"),
                     effective_from=parse_dt("2026-12-01T09:43:00"), approval_id=ap, decision_id="t",
                     now=parse_dt("2026-12-01T09:00:00"))
