"""模拟执行引擎（§7）：只消费经授权且冻结的 OrderIntent；保守成交模型；禁止事后成交。

证据等级：A 券商/逐笔；B 分钟 OHLCV + 生效时间；C 仅日线（只能 ELIGIBLE_UNVERIFIED）；D 新闻/摘要（不用于成交）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .clock import ET, ensure_aware, is_trading_day, market_close, market_open, now_utc
from .config import Settings
from .event_store import EventStore, canonical, sha256, to_jsonable
from .evidence import EvidenceStore
from .ledger import ZERO, OPEN_ORDER_STATES, OrderState, q2, replay
from .market_data import Bar, MarketStore
from .schemas import OrderIntent

ELIGIBLE_GRACE = timedelta(days=3)  # 仅有日线触价的订单，等待补充分钟证据的宽限期


class OrderRejected(ValueError):
    pass


def create_order(store: EventStore, settings: Settings, market: MarketStore, *, ticker: str, side: str,
                 quantity: Decimal, order_type: str, effective_from: datetime, approval_id: str | None,
                 decision_id: str, limit_price: Decimal | None = None, expires_at: datetime | None = None,
                 now: datetime | None = None, competition_id: str | None = None) -> OrderIntent:
    now = ensure_aware(now) if now else now_utc()
    effective_from = ensure_aware(effective_from)
    ticker = ticker.upper()
    quantity = Decimal(str(quantity))
    limit_price = Decimal(str(limit_price)) if limit_price is not None else None
    events = store.load()
    st = replay(events, settings.initial_cash, as_of=now)

    if effective_from < now:
        raise OrderRejected("effective_from 早于当前时间：禁止回填订单")
    eff_day = effective_from.astimezone(ET).date()
    if not is_trading_day(eff_day):
        raise OrderRejected(f"{eff_day} 不是美股交易日")
    close = market_close(eff_day)
    expires_at = ensure_aware(expires_at) if expires_at else close
    if expires_at > close:
        raise OrderRejected("当日有效订单在收盘后到期，不自动滚动到下一日")
    if effective_from >= close:
        raise OrderRejected("生效时间不在常规交易时段内")

    if not approval_id:
        raise OrderRejected("没有授权 ID：所有模拟订单都必须引用有效授权")
    ap_state = st.approvals.get(approval_id)
    if not ap_state:
        raise OrderRejected(f"授权 {approval_id} 不存在（或未在当前时点前登记）")
    ap = ap_state.approval
    if ap.meeting_id and st.meetings.get(ap.meeting_id, {}).get("status") == "REJECTED":
        raise OrderRejected("该会议已被用户拒绝")
    if side not in ap.sides:
        raise OrderRejected(f"授权不包含 {side}")
    if ap.valid_until is None:
        raise OrderRejected("授权有效期未登记，不能用于新订单")
    if not (ap.valid_from <= effective_from and expires_at <= ap.valid_until):
        raise OrderRejected("订单时间窗超出授权有效期")
    caps_usd, caps_qty = ap.ticker_caps_usd, ap.ticker_qty_caps
    if (caps_usd or caps_qty) and ticker not in caps_usd and ticker not in caps_qty:
        raise OrderRejected(f"授权未覆盖 {ticker}")

    if settings.risk.get("whole_shares_for_new_orders", True) and quantity != quantity.to_integral_value():
        raise OrderRejected("新订单默认整股（risk_limits.whole_shares_for_new_orders）")

    ref = market.view(now).latest_close(ticker)
    ref_price = limit_price if order_type == "LIMIT" else (ref.price if ref else None)
    if ref_price is None:
        raise OrderRejected("市价单缺少参考价格，无法检查预算")
    notional = quantity * ref_price
    # 市价单按参考价 +2% 预留，保守占用预算
    reserve_px = limit_price if order_type == "LIMIT" else q2(ref_price * Decimal("1.02"))

    if side == "BUY":
        remaining = st.approval_remaining(approval_id)
        if remaining is not None and quantity * reserve_px > remaining:
            raise OrderRejected(f"超出授权剩余预算 ${q2(remaining)}")
        if quantity * reserve_px > st.cash - st.reserved_cash():
            raise OrderRejected("可用现金不足：禁止杠杆")
    else:
        pos = st.positions.get(ticker, ZERO)
        pending_sells = sum((o.intent.quantity for o in st.orders.values()
                             if o.intent.ticker == ticker and o.intent.side == "SELL" and o.status in OPEN_ORDER_STATES), ZERO)
        if quantity > pos - pending_sells:
            raise OrderRejected(f"卖出数量超过可卖持仓 {pos - pending_sells}：禁止做空")
    if ticker in caps_usd:
        used = ap_state.used_by_ticker_usd.get(ticker, ZERO) + sum(
            (o.intent.quantity * (o.intent.limit_price or ZERO) for o in st.orders.values()
             if o.intent.approval_id == approval_id and o.intent.ticker == ticker and o.status in OPEN_ORDER_STATES),
            ZERO)
        if quantity * reserve_px > caps_usd[ticker] - used:
            raise OrderRejected(f"超出 {ticker} 授权上限（剩余 ${q2(caps_usd[ticker] - used)}）")
    if ticker in caps_qty and quantity > caps_qty[ticker] - ap_state.used_qty.get(ticker, ZERO):
        raise OrderRejected(f"超出 {ticker} 授权数量上限")

    intent = OrderIntent(
        order_id=f"ord_{now:%Y%m%d}_{uuid.uuid4().hex[:8]}", decision_id=decision_id, created_at=now,
        effective_from=effective_from, expires_at=expires_at, ticker=ticker, side=side, quantity=quantity,
        order_type=order_type, limit_price=limit_price if order_type == "LIMIT" else reserve_px,
        approval_id=approval_id, status="PENDING",
    )
    if order_type == "MARKET":
        # 市价单的 limit_price 字段仅作为预算占用的保护价
        intent = intent.model_copy(update={"order_type": "MARKET"})
    body = to_jsonable(intent.model_dump())
    store.append("OrderProposed", {**body, "intent_hash": sha256(canonical(body)),
                                   "reference_price": ref_price, "notional_estimate": q2(notional)},
                 occurred_at=now, source="execution", competition_id=competition_id or settings.active_competition_id)
    return intent


def cancel_order(store: EventStore, settings: Settings, order_id: str, reason: str, now: datetime | None = None) -> None:
    st = replay(store.load(), settings.initial_cash)
    o = st.orders.get(order_id)
    if not o:
        raise OrderRejected(f"订单不存在: {order_id}")
    if o.status not in OPEN_ORDER_STATES:
        raise OrderRejected(f"订单已终结: {o.status}")
    store.append("OrderCancelled", {"order_id": order_id, "reason": reason}, occurred_at=now, source="user",
                 competition_id=settings.active_competition_id)


@dataclass
class FillDecision:
    bar: Bar
    price: Decimal
    grade: str
    model: str


def _minute_fill(o: OrderIntent, bars: list[Bar]) -> FillDecision | None:
    for b in bars:
        if b.start < o.effective_from or b.end > o.expires_at or b.volume <= 0:
            continue
        if o.order_type == "MARKET":
            return FillDecision(b, b.open, "B", "minute_bar_first_open_after_activation")
        L = o.limit_price
        if o.side == "BUY" and b.low <= L:
            return FillDecision(b, L, "B", "minute_bar_conservative_limit")
        if o.side == "SELL" and b.high >= L:
            return FillDecision(b, L, "B", "minute_bar_conservative_limit")
    return None


def reconcile(store: EventStore, settings: Settings, market: MarketStore, evidence: EvidenceStore,
              now: datetime | None = None) -> list[str]:
    """按时间顺序推进订单状态机；返回本次动作日志。"""
    now = ensure_aware(now) if now else now_utc()
    view = market.view(now)
    log: list[str] = []
    st = replay(store.load(), settings.initial_cash)
    for oid, o in list(st.orders.items()):
        it = o.intent
        if o.status not in OPEN_ORDER_STATES:
            continue
        if o.status == "PENDING":
            if now < it.effective_from:
                continue
            store.append("OrderActivated", {"order_id": oid}, occurred_at=it.effective_from, source="execution",
                         competition_id=settings.active_competition_id)
            log.append(f"{oid} 激活")
            o.status = "ACTIVE"

        window_end = min(now, it.expires_at)
        bars = view.minutes(it.ticker, it.effective_from, window_end)
        fd = _minute_fill(it, bars)
        if fd:
            cur = replay(store.load(), settings.initial_cash)
            notional = it.quantity * fd.price
            if it.side == "BUY" and notional > cur.cash:
                store.append("OrderCancelled", {"order_id": oid, "reason": "成交时现金不足"}, occurred_at=fd.bar.start,
                             source="execution", competition_id=settings.active_competition_id)
                log.append(f"{oid} 现金不足，撤销")
                continue
            ev = evidence.add(source_url=f"marketdata://{fd.bar.source}/{it.ticker}/1m/{fd.bar.start.isoformat()}",
                              retrieved_at=fd.bar.retrieved_at, available_at=fd.bar.available_at,
                              source_type=fd.bar.source_type, reliability="provisional",
                              content=fd.bar.to_row(), title=f"{it.ticker} 1m bar", tickers=[it.ticker])
            signed = it.quantity if it.side == "BUY" else -it.quantity
            store.append("FillRecorded", {
                "fill_id": f"fill_{uuid.uuid4().hex[:10]}", "order_id": oid, "ticker": it.ticker, "side": it.side,
                "quantity": it.quantity, "price": fd.price, "fee": "0", "registered_cash_delta": q2(-signed * fd.price),
                "approval_id": it.approval_id, "execution_model": fd.model, "fill_confidence": "simulated",
                "evidence_grade": fd.grade, "source_evidence_id": ev.evidence_id, "flags": [],
                "note": "仿真成交（非券商确认）",
            }, occurred_at=fd.bar.start, source="execution", competition_id=settings.active_competition_id)
            log.append(f"{oid} 成交 {it.side} {it.ticker} {it.quantity} @ {fd.price}（{fd.grade} 级，{fd.model}）")
            continue

        eff_day = it.effective_from.astimezone(ET).date()
        daily = view.daily(it.ticker, eff_day)
        touched = False
        if daily and it.order_type == "LIMIT":
            b = daily[0]
            touched = (b.low <= it.limit_price) if it.side == "BUY" else (b.high >= it.limit_price)
        if daily and it.order_type == "MARKET" and it.effective_from <= market_open(eff_day) and not bars:
            touched = True  # 仅有日线开盘价：只能标记待核实
        if touched and o.status == "ACTIVE":
            store.append("OrderEligibleUnverified", {
                "order_id": oid, "reason": "仅有日线区间触价证据（C 级），不能证明生效后成交",
                "daily_low": daily[0].low, "daily_high": daily[0].high, "source": daily[0].source,
            }, occurred_at=min(now, it.expires_at), source="execution", competition_id=settings.active_competition_id)
            log.append(f"{oid} 标记 ELIGIBLE_UNVERIFIED（仅日线触价）")
            o.status = "ELIGIBLE_UNVERIFIED"

        if now >= it.expires_at:
            if o.status == "ELIGIBLE_UNVERIFIED" and now < it.expires_at + ELIGIBLE_GRACE:
                continue  # 宽限期内等待补充分钟级证据
            reason = "无法证明成交" if o.status == "ELIGIBLE_UNVERIFIED" else "到期未成交"
            store.append("OrderExpired", {"order_id": oid, "reason": reason}, occurred_at=it.expires_at,
                         source="execution", competition_id=settings.active_competition_id)
            log.append(f"{oid} 到期（{reason}）")
    return log
